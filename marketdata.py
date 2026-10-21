"""Stateless Yahoo Finance fetch helpers - no Flask, no storage.

Shared by app.py (live serving) and load_history.py (bulk backfill).
"""

import time

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125 Safari/537.36")
HEADERS = {"User-Agent": _UA, "Accept": "application/json"}

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"

# Far enough back that Yahoo clamps to the earliest bar it actually has
# (1927 for ^GSPC, 1962 for old blue chips, IPO date otherwise).
FULL_START = -2208988800  # 1900-01-01 UTC


def chart_result(symbol, params, retries=3):
    """GET the chart endpoint and return result[0]. Retries transient errors / 429."""
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(
                _CHART_URL.format(symbol),
                params={"includePrePost": "false", **params},
                headers=HEADERS,
                timeout=20,
            )
            if r.status_code == 429:
                last_err = "rate limited (HTTP 429)"
                time.sleep(2 * (attempt + 1) + 1)
                continue
            r.raise_for_status()
            payload = r.json()["chart"]
            if payload.get("error"):
                raise ValueError(payload["error"].get("description") or "unknown symbol")
            return payload["result"][0]
        except ValueError:
            raise  # bad symbol - no point retrying
        except (requests.RequestException, KeyError, IndexError) as exc:
            last_err = str(exc)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Yahoo chart failed for {symbol}: {last_err}")


def daily_bars(symbol, period1, period2):
    """Return a list of {ts, open, high, low, close, adjclose, volume} daily bars."""
    res = chart_result(symbol, {
        "period1": int(period1),
        "period2": int(period2),
        "interval": "1d",
        "events": "div,splits",
    })
    ts = res.get("timestamp") or []
    ind = res.get("indicators", {})
    q = (ind.get("quote") or [{}])[0]
    adj = ((ind.get("adjclose") or [{}])[0] or {}).get("adjclose") or []
    o, h, l, c = (q.get(k) or [] for k in ("open", "high", "low", "close"))
    v = q.get("volume") or []
    out = []
    for i, t in enumerate(ts):
        if i >= len(c) or c[i] is None:
            continue
        out.append({
            "ts": int(t),
            "open": o[i] if i < len(o) else None,
            "high": h[i] if i < len(h) else None,
            "low": l[i] if i < len(l) else None,
            "close": c[i],
            "adjclose": adj[i] if i < len(adj) else None,
            "volume": v[i] if i < len(v) else None,
        })
    return out


def intraday_bars(symbol, interval="1h", lookback_days=365):
    """Return a list of {ts, open, high, low, close, volume} intraday bars.

    Yahoo caps intraday lookback by interval (1h ~= 730 days, 5m ~= 58 days).
    """
    now = int(time.time())
    res = chart_result(symbol, {
        "period1": now - int(lookback_days) * 86400,
        "period2": now,
        "interval": interval,
    })
    ts = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    o, h, l, c = (q.get(k) or [] for k in ("open", "high", "low", "close"))
    v = q.get("volume") or []
    out = []
    for i, t in enumerate(ts):
        if i >= len(c) or c[i] is None:
            continue
        out.append({
            "ts": int(t),
            "open": o[i] if i < len(o) else None,
            "high": h[i] if i < len(h) else None,
            "low": l[i] if i < len(l) else None,
            "close": c[i],
            "volume": v[i] if i < len(v) else None,
        })
    return out


def quote_meta(symbol):
    """Small live snapshot (regularMarketPrice, previous close, exchange, names...)."""
    return chart_result(symbol, {"range": "1d", "interval": "1d"}).get("meta", {})


def search_quotes(query):
    """Yahoo symbol search -> [{symbol, name, exchange, type}] (up to 14 matches)."""
    r = requests.get(
        _SEARCH_URL,
        params={"q": query, "quotesCount": 14, "newsCount": 0, "listsCount": 0},
        headers=HEADERS,
        timeout=8,
    )
    r.raise_for_status()
    out = []
    for it in r.json().get("quotes", []):
        if not it.get("symbol"):
            continue
        out.append({
            "symbol": it["symbol"],
            "name": it.get("shortname") or it.get("longname") or "",
            "exchange": it.get("exchDisp") or it.get("exchange") or "",
            "type": it.get("typeDisp") or it.get("quoteType") or "",
        })
    return out
