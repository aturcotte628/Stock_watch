"""Bulk-load daily history into the local SQLite cache (data/history.db).

Examples
--------
    python load_history.py                     # built-in ~100 largest US stocks, 10y
    python load_history.py --top 1000          # 1000 largest US stocks by market cap
    python load_history.py --top 500 --years 20
    python load_history.py --years 0           # all available history instead of 10y
    python load_history.py --symbols AAPL,MSFT,BRK-B
    python load_history.py --list-file my_tickers.txt
    python load_history.py --refresh                    # top up every stored daily symbol (scheduled runs)
    python load_history.py --refresh --interval 1h      # re-pull hourly for every stored 1h symbol
    python load_history.py --refresh --interval 5m

`--top N` pulls the current N-largest US-listed companies from the Nasdaq stock
screener (one request), sorted by market cap. Falls back to the built-in list if
that request fails.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import re
import time

import requests

import store

# ~100 of the largest US-listed companies - offline fallback / default.
TOP_100 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "BRK-B", "AVGO", "LLY",
    "TSLA", "JPM", "WMT", "V", "UNH", "XOM", "MA", "JNJ", "PG", "HD",
    "COST", "ORCL", "BAC", "ABBV", "CVX", "KO", "MRK", "CRM", "AMD", "PEP",
    "NFLX", "ADBE", "TMO", "LIN", "ACN", "MCD", "CSCO", "ABT", "WFC", "GE",
    "DHR", "INTC", "VZ", "TXN", "QCOM", "PM", "CAT", "IBM", "AMGN", "NOW",
    "UNP", "GS", "INTU", "SPGI", "GILD", "MS", "AXP", "BLK", "DE", "BKNG",
    "SYK", "LOW", "T", "PLD", "TJX", "REGN", "VRTX", "C", "SCHW", "CB",
    "MRSH", "ISRG", "SO", "ZTS", "BSX", "PGR", "ETN", "SLB", "DUK", "BMY",
    "CME", "EOG", "APD", "MU", "EQIX", "AON", "ITW", "CL", "NOC", "WM",
    "CSX", "FCX", "EMR", "MCO", "PNC", "GD", "MAR", "MSI", "HCA", "NSC",
]

_NASDAQ_SCREENER = "https://api.nasdaq.com/api/screener/stocks"
_NASDAQ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
_TICKER_RE = re.compile(r"^[A-Z]{1,6}(-[A-Z])?$")


def fetch_top_us(n):
    """Return the ~n largest US-listed tickers by market cap (Nasdaq screener)."""
    r = requests.get(
        _NASDAQ_SCREENER,
        params={"tableonly": "true", "limit": "10000", "offset": "0", "download": "true"},
        headers=_NASDAQ_HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    rows = (r.json().get("data") or {}).get("rows") or []

    def cap(row):
        try:
            return float(row.get("marketCap") or 0)
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted((row for row in rows if cap(row) > 0), key=cap, reverse=True)
    out, seen = [], set()
    for row in ranked:
        sym = (row.get("symbol") or "").strip().upper().replace("/", "-")
        if sym and sym not in seen and _TICKER_RE.match(sym):
            seen.add(sym)
            out.append(sym)
        if len(out) >= n:
            break
    return out


def resolve_universe(args):
    """Pick the symbols to load and a label for where they came from.

    Priority: --refresh (what is already stored), --symbols, --list-file, --top, then TOP_100.
    """
    if args.refresh:
        if args.interval == "1d":
            return store.all_symbols(), "refresh: every stored daily symbol"
        return store.intraday_symbols(args.interval), f"refresh: every stored {args.interval} symbol"
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()], "--symbols"
    if args.list_file:
        with open(args.list_file, encoding="utf-8") as fh:
            toks = re.split(r"[\s,]+", fh.read())
        return [t.strip().upper() for t in toks if t.strip()], args.list_file
    if args.top:
        try:
            syms = fetch_top_us(args.top)
            if syms:
                return syms, f"Nasdaq screener top {len(syms)}"
            print("  (screener returned nothing - using built-in list)")
        except Exception as exc:  # noqa: BLE001
            print(f"  (screener fetch failed: {exc} - using built-in list)")
    return TOP_100, "built-in top 100"


def _fmt_date(ts):
    """Epoch seconds -> YYYY-MM-DD (UTC), or '?' when missing."""
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat() if ts else "?"


def load_one(symbol, years, interval="1d", force=True):
    """Load one symbol. Returns (symbol, state, error, seconds); errors are returned, not raised."""
    t0 = time.time()
    try:
        if interval in ("1h", "5m"):
            r = store.sync_intraday(symbol, interval, force=force)
            st = {"bars": r["bars"], "first_ts": None, "last_ts": r["last_ts"], "error": None}
        else:
            st = store.sync(symbol, years=years, force=force)
        return symbol, st, None, time.time() - t0
    except Exception as exc:  # noqa: BLE001
        return symbol, None, str(exc), time.time() - t0


def main():
    """CLI entry point: parse arguments, load the universe in parallel and print a summary."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=0, help="fetch the N largest US stocks by market cap")
    ap.add_argument("--refresh", action="store_true",
                    help="re-sync every symbol already in the DB (incremental for 1d); for scheduled runs")
    ap.add_argument("--interval", choices=["1d", "1h", "5m"], default="1d",
                    help="1d = daily history (default); 1h = ~1y hourly; 5m = ~57d of 5-minute bars")
    ap.add_argument("--years", type=int, default=10, help="history depth; 0 = all (default: 10)")
    ap.add_argument("--workers", type=int, default=6, help="parallel downloads (default: 6)")
    ap.add_argument("--symbols", default="", help="comma-separated tickers")
    ap.add_argument("--list-file", default="", help="file of tickers (whitespace/comma separated)")
    ap.add_argument("--verbose", action="store_true", help="print a line per symbol (default: only failures + progress)")
    args = ap.parse_args()

    store.init_db()
    symbols, source = resolve_universe(args)
    total = len(symbols)
    if not total:
        print(f"Nothing to do ({source}). Run without --refresh first to populate the DB.")
        return
    if args.interval == "1h":
        depth = "~1y hourly"
    elif args.interval == "5m":
        depth = "~57d 5-minute"
    elif args.refresh:
        depth = "incremental top-up"
    else:
        depth = "all history" if args.years == 0 else f"{args.years} years"
    print(f"Loading {total} symbols from {source}  ({depth}, {args.workers} workers)")
    print(f"-> {store.DB_PATH}\n")

    # --refresh on daily uses the cheap incremental path; everything else re-pulls the window.
    force = not (args.refresh and args.interval == "1d")

    ok = failed = 0
    started = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(load_one, s, args.years, args.interval, force): s for s in symbols}
        for i, fut in enumerate(cf.as_completed(futures), 1):
            symbol, st, err, secs = fut.result()
            if err:
                failed += 1
                print(f"[{i:4d}/{total}] {symbol:<7} FAILED  {err[:64]}")
            else:
                ok += 1
                if args.verbose or args.interval != "1d":
                    span = "" if args.interval != "1d" else f"  {_fmt_date(st['first_ts'])} -> {_fmt_date(st['last_ts'])}"
                    note = f"  !{st['error'][:40]}" if st.get("error") else ""
                    print(f"[{i:4d}/{total}] {symbol:<7} {st['bars']:>6,} bars{span}  ({secs:4.1f}s){note}")
                elif i % 50 == 0 or i == total:
                    rate = i / max(time.time() - started, 1e-9)
                    print(f"[{i:4d}/{total}]  {ok} ok  {failed} failed  ({rate:.0f}/s)")

    t = store.totals()
    print(f"\nDone in {time.time() - started:.0f}s.  {ok} ok, {failed} failed.")
    print(f"DB: {t['bars']:,} daily / {t['symbols']} sym  ·  "
          f"{t['hourly_bars']:,} 1h / {t['hourly_symbols']} sym  ·  "
          f"{t['min5_bars']:,} 5m / {t['min5_symbols']} sym  ({t['db_bytes'] / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
