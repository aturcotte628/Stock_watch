"""Marketscope - a small local website for browsing US publicly traded markets.

Daily price history is served from a local SQLite cache (see store.py); it is
backfilled on first use and topped up incrementally. Intraday bars, 5Y/MAX
long ranges, live quotes, search and fundamentals come straight from Yahoo
(yfinance for fundamentals). Short-lived in-memory caching sits in front of
the upstream calls.

Run:  python app.py   ->  http://127.0.0.1:5000
Bulk-load the top 100:  python load_history.py
"""

import concurrent.futures as cf
import datetime as dt
import threading
import time
import webbrowser

import yfinance as yf
from flask import Flask, jsonify, render_template, request

import analysis
import marketdata
import store

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # always re-serve edited css/js in local dev

store.init_db()

# Symbol, display label - shown on the overview dashboard.
MARKET_ROWS = [
    ("NVDA", "NVIDIA"),
    ("INTC", "Intel"),
    ("AAPL", "Apple"),
    ("AMD", "AMD"),
    ("TSM", "TSMC"),
    ("^GSPC", "S&P 500"),
    ("^DJI", "Dow Jones"),
    ("^IXIC", "Nasdaq Composite"),
    ("^RUT", "Russell 2000"),
    ("^VIX", "Volatility Index"),
    ("^TNX", "10Y Treasury Yield"),
    ("CL=F", "Crude Oil (WTI)"),
    ("GC=F", "Gold"),
    ("DX-Y.NYB", "US Dollar Index"),
    ("BTC-USD", "Bitcoin"),
]

_cache = {}
_cache_lock = threading.Lock()


def cached(key, ttl, producer):
    """Return producer() memoised under key for ttl seconds. Errors are not cached."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _sym(raw):
    """Normalise a ticker from a query string: trimmed, upper-case, max 20 chars."""
    return (raw or "").strip().upper()[:20]


def _quote(symbol):
    """Light quote (price + day change) built from the chart endpoint's meta block."""
    try:
        m = cached(f"q:{symbol}", 20, lambda: marketdata.quote_meta(symbol))
        price = m.get("regularMarketPrice")
        prev = m.get("chartPreviousClose") or m.get("previousClose")
        change = (price - prev) if (price is not None and prev is not None) else None
        pct = (change / prev * 100) if (change is not None and prev) else None
        return {
            "symbol": symbol,
            "name": m.get("shortName") or m.get("longName") or symbol,
            "price": price,
            "change": change,
            "changePercent": pct,
            "currency": m.get("currency"),
            "exchange": m.get("exchangeName"),
        }
    except Exception as exc:  # noqa: BLE001 - surface per-row, don't fail the batch
        return {"symbol": symbol, "error": str(exc)}


def _quote_many(symbols):
    """Quote up to 30 unique symbols in parallel, preserving order. Empty input -> []."""
    symbols = [s for s in dict.fromkeys(symbols) if s][:30]
    if not symbols:
        return []
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(_quote, symbols))


PROFILE_KEYS = [
    "longName", "shortName", "symbol", "quoteType", "exchange", "currency",
    "sector", "industry", "website", "country", "city", "state", "fullTimeEmployees",
    "longBusinessSummary", "marketCap", "trailingPE", "forwardPE", "priceToBook",
    "trailingEps", "dividendYield", "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
    "averageVolume", "regularMarketVolume", "regularMarketOpen", "regularMarketPreviousClose",
    "regularMarketDayHigh", "regularMarketDayLow", "sharesOutstanding", "profitMargins",
    "revenueGrowth", "totalRevenue", "regularMarketPrice",
]


@app.route("/")
def index():
    """Single-page app shell; asset_v (newest static file mtime) busts the browser cache."""
    import os

    static_dir = os.path.join(app.root_path, "static")
    latest = 0
    for root, _dirs, files in os.walk(static_dir):
        for f in files:
            latest = max(latest, os.path.getmtime(os.path.join(root, f)))
    return render_template("index.html", asset_v=int(latest))


@app.route("/api/overview")
def api_overview():
    """Quotes for the MARKET_ROWS dashboard, each tagged with its display label."""
    data = cached("overview", 20, lambda: _quote_many([s for s, _ in MARKET_ROWS]))
    labels = {s: lbl for s, lbl in MARKET_ROWS}
    return jsonify([{**row, "label": labels.get(row["symbol"], row["symbol"])} for row in data])


@app.route("/api/quotes")
def api_quotes():
    """Light quotes for ?symbols=A,B,C (comma-separated, max 30)."""
    symbols = [_sym(s) for s in (request.args.get("symbols") or "").split(",")]
    return jsonify(_quote_many(symbols))


@app.route("/api/search")
def api_search():
    """Ticker/company search for ?q=; 502 with an error body if Yahoo fails."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify([])

    try:
        return jsonify(cached(f"s:{q.lower()}", 120, lambda: marketdata.search_quotes(q)))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


# Approx seconds of lookback per range preset (used to slice the SQLite table).
_RANGE_DAYS = {"1d": 2, "5d": 7, "1mo": 34, "6mo": 190, "1y": 372, "5y": 1850}


def _range_start_ts(rng):
    """Epoch start for a range preset (1d..5y, ytd), or None for 'max' / unknown presets."""
    if rng == "max":
        return None
    if rng == "ytd":
        year = dt.datetime.now(dt.timezone.utc).year
        return int(dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    days = _RANGE_DAYS.get(rng)
    return None if days is None else int(time.time()) - days * 86400


def _candles_from_yahoo(symbol, rng, interval):
    """Fetch candles live from Yahoo and shape them like the SQLite-backed response."""
    if rng == "max":
        params = {"period1": marketdata.FULL_START, "period2": int(time.time()), "interval": interval}
    else:
        params = {"range": rng, "interval": interval}
    result = marketdata.chart_result(symbol, params)
    ts = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    o, h, l, c = (quote.get(k) or [] for k in ("open", "high", "low", "close"))
    v = quote.get("volume") or []
    candles = []
    for i, t in enumerate(ts):
        if i >= len(c) or c[i] is None:
            continue
        candles.append({
            "time": int(t),
            "open": o[i] if i < len(o) else None,
            "high": h[i] if i < len(h) else None,
            "low": l[i] if i < len(l) else None,
            "close": c[i],
            "volume": v[i] if i < len(v) else None,
        })
    return {"symbol": symbol, "meta": result.get("meta", {}), "candles": candles, "source": "yahoo"}


def _strip_ohlcv(rows):
    """Keep only the time + OHLCV fields the chart needs (drops adjclose)."""
    return [{"time": r["time"], "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"], "volume": r["volume"]} for r in rows]


@app.route("/api/chart")
def api_chart():
    """Candles for ?symbol, ?range, ?interval. Daily comes from SQLite, 5Y weekly is
    resampled from it, and MAX/intraday go live to Yahoo. Cached for 30 s."""
    symbol = _sym(request.args.get("symbol"))
    rng = (request.args.get("range") or "1y").strip()
    interval = (request.args.get("interval") or "1d").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400

    def producer():
        start_ts = _range_start_ts(rng)

        # Daily ranges (1M / 6M / YTD / 1Y): serve straight from the local DB.
        if interval == "1d":
            st = store.sync(symbol)  # lazy backfill + incremental top-up
            rows = store.get_bars(symbol, start_ts)
            if rows:
                return {
                    "symbol": symbol,
                    "meta": marketdata.quote_meta(symbol),
                    "candles": _strip_ohlcv(rows),
                    "source": "sqlite",
                    "stored_bars": (st or {}).get("bars"),
                }
            return _candles_from_yahoo(symbol, rng, interval)  # nothing stored -> live

        # 5Y weekly: roll up from the local DB when it covers the window.
        if interval == "1wk":
            st = store.sync(symbol)
            if st and st.get("first_ts") is not None and start_ts and st["first_ts"] <= start_ts + 10 * 86400:
                rows = store.get_bars(symbol, start_ts)
                if len(rows) > 200:
                    return {
                        "symbol": symbol,
                        "meta": marketdata.quote_meta(symbol),
                        "candles": store.resample(rows, "1wk"),
                        "source": "sqlite-resampled",
                        "stored_bars": st.get("bars"),
                    }

        # MAX (monthly) and intraday (5m / 30m): always live from Yahoo.
        return _candles_from_yahoo(symbol, rng, interval)

    try:
        return jsonify(cached(f"c:{symbol}:{rng}:{interval}", 30, producer))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.route("/api/profile")
def api_profile():
    """Company profile and fundamentals (PROFILE_KEYS subset); best-effort, cached 15 min."""
    symbol = _sym(request.args.get("symbol"))
    if not symbol:
        return jsonify({"error": "symbol required"}), 400

    def producer():
        try:
            info = yf.Ticker(symbol).get_info() or {}
        except Exception:  # noqa: BLE001 - fundamentals are best-effort
            info = {}
        return {k: info.get(k) for k in PROFILE_KEYS}

    try:
        return jsonify(cached(f"p:{symbol}", 900, producer))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.route("/api/stats")
def api_stats():
    """Risk/return stats for ?symbol over ?window (default 3y); see analysis.stats."""
    symbol = _sym(request.args.get("symbol"))
    window = (request.args.get("window") or "3y").strip().lower()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        return jsonify(cached(f"stats:{symbol}:{window}", 300, lambda: analysis.stats(symbol, window)))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.route("/api/factor")
def api_factor():
    """Tech-sector exposure beyond the market for ?symbol; see analysis.factor."""
    symbol = _sym(request.args.get("symbol"))
    window = (request.args.get("window") or "2y").strip().lower()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        return jsonify(cached(f"factor:{symbol}:{window}", 300, lambda: analysis.factor(symbol, window)))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.route("/api/similar")
def api_similar():
    """Stocks that move most like ?symbol, ranked by ?rank; ?n is clamped to 5-50."""
    symbol = _sym(request.args.get("symbol"))
    window = (request.args.get("window") or "2y").strip().lower()
    rank = (request.args.get("rank") or "overall").strip().lower()
    try:
        n = min(50, max(5, int(request.args.get("n", 25))))
    except ValueError:
        n = 25
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        return jsonify(cached(
            f"sim:{symbol}:{window}:{rank}:{n}", 300,
            lambda: analysis.similar(symbol, window=window, rank=rank, n=n),
        ))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.route("/api/movers")
def api_movers():
    """Top gainers/losers over ?period (1d or 5d) from the local cache; ?n clamped to 3-30."""
    period = (request.args.get("period") or "1d").strip().lower()
    try:
        n = min(30, max(3, int(request.args.get("n", 12))))
    except ValueError:
        n = 12
    try:
        return jsonify(cached(f"movers:{period}:{n}", 300, lambda: analysis.movers(period, n)))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.route("/api/leadlag")
def api_leadlag():
    """Which peers lead or follow ?symbol intraday (?interval 1h/5m, optional ?peers list)."""
    symbol = _sym(request.args.get("symbol"))
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    interval = (request.args.get("interval") or "1h").strip().lower()
    peers = [_sym(s) for s in (request.args.get("peers") or "").split(",") if s.strip()] or None
    try:
        n = min(60, max(5, int(request.args.get("n", 30))))
    except ValueError:
        n = 30
    try:
        out = cached(
            f"ll:{symbol}:{interval}:{','.join(peers) if peers else 'all'}", 300,
            lambda: analysis.leadlag(symbol, interval=interval, peers=peers),
        )
        return jsonify({**out, "results": out["results"][:n]})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.route("/api/history/status")
def api_history_status():
    """What the local SQLite cache currently holds (totals + optional per-symbol)."""
    out = {"totals": store.totals()}
    syms = [_sym(s) for s in (request.args.get("symbols") or "").split(",") if s.strip()]
    if syms:
        out["symbols"] = {s: store.get_state(s) for s in syms}
    return jsonify(out)


if __name__ == "__main__":
    import os

    url = "http://127.0.0.1:5000"
    if not os.environ.get("MARKETSCOPE_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f" * Marketscope running at {url}  (Ctrl+C to stop)")
    # threaded: a slow upstream (Yahoo) call must not block fast SQLite reads,
    # and out-of-order responses are guarded on the client (chartGen / simGen).
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
