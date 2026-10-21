"""Local SQLite cache of daily OHLCV bars.

One table of bars keyed by (symbol, ts) plus a sync_state bookkeeping row per
symbol. `sync()` is the entry point: it backfills a symbol on first use and
does cheap incremental top-ups afterwards. The Flask app reads through
get_bars(); the bulk loader calls sync(force=True).
"""

import os
import sqlite3
import threading
import time

import marketdata

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "history.db")

# How long a symbol's daily data is trusted before we re-check for new bars.
REFRESH_TTL = 1800  # seconds

_locks = {}
_locks_guard = threading.Lock()


def _lock_for(symbol):
    """Per-key lock so concurrent requests don't sync the same symbol twice."""
    with _locks_guard:
        return _locks.setdefault(symbol, threading.Lock())


def _connect():
    """Open the SQLite DB (WAL mode, 30 s busy timeout), creating data/ if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db():
    """Create the bars, sync_state and intraday tables if they don't exist."""
    with _connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS bars (
            symbol   TEXT NOT NULL,
            ts       INTEGER NOT NULL,
            open     REAL,
            high     REAL,
            low      REAL,
            close    REAL,
            adjclose REAL,
            volume   INTEGER,
            PRIMARY KEY (symbol, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_bars_symbol_ts ON bars(symbol, ts);

        CREATE TABLE IF NOT EXISTS sync_state (
            symbol     TEXT PRIMARY KEY,
            first_ts   INTEGER,
            last_ts    INTEGER,
            bars       INTEGER,
            updated_at INTEGER,
            error      TEXT
        );

        CREATE TABLE IF NOT EXISTS bars_1h (
            symbol TEXT NOT NULL, ts INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY (symbol, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_bars1h_symbol_ts ON bars_1h(symbol, ts);

        CREATE TABLE IF NOT EXISTS bars_5m (
            symbol TEXT NOT NULL, ts INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY (symbol, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_bars5m_symbol_ts ON bars_5m(symbol, ts);
        """)


_STATE_KEYS = ["symbol", "first_ts", "last_ts", "bars", "updated_at", "error"]


def all_symbols():
    """Every symbol that has daily bars stored."""
    with _connect() as con:
        rows = con.execute(
            "SELECT symbol FROM sync_state WHERE bars > 0 ORDER BY symbol"
        ).fetchall()
    return [r[0] for r in rows]


def get_state(symbol):
    """Sync bookkeeping for a symbol (first/last bar, bar count, last error), or None."""
    with _connect() as con:
        row = con.execute(
            "SELECT symbol, first_ts, last_ts, bars, updated_at, error "
            "FROM sync_state WHERE symbol=?", (symbol,)
        ).fetchone()
    return dict(zip(_STATE_KEYS, row)) if row else None


def _refresh_state(con, symbol, error=None):
    """Recompute a symbol's sync_state row from its stored bars."""
    first_ts, last_ts, n = con.execute(
        "SELECT MIN(ts), MAX(ts), COUNT(*) FROM bars WHERE symbol=?", (symbol,)
    ).fetchone()
    con.execute("""
        INSERT INTO sync_state(symbol, first_ts, last_ts, bars, updated_at, error)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(symbol) DO UPDATE SET
            first_ts=excluded.first_ts, last_ts=excluded.last_ts,
            bars=excluded.bars, updated_at=excluded.updated_at, error=excluded.error
    """, (symbol, first_ts, last_ts, n, int(time.time()), error))


def upsert_bars(symbol, bars):
    """Insert/replace bars; refresh the sync_state row. Returns rows added (net)."""
    with _connect() as con:
        before = con.execute("SELECT COUNT(*) FROM bars WHERE symbol=?", (symbol,)).fetchone()[0]
        if bars:
            con.executemany("""
                INSERT INTO bars(symbol, ts, open, high, low, close, adjclose, volume)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, ts) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, adjclose=excluded.adjclose, volume=excluded.volume
            """, [
                (symbol, b["ts"], b.get("open"), b.get("high"), b.get("low"),
                 b.get("close"), b.get("adjclose"), b.get("volume"))
                for b in bars
            ])
        after = con.execute("SELECT COUNT(*) FROM bars WHERE symbol=?", (symbol,)).fetchone()[0]
        _refresh_state(con, symbol)
    return after - before


def get_bars(symbol, start_ts=None, end_ts=None):
    """Daily bars for a symbol in [start_ts, end_ts], oldest first, as dicts."""
    q = "SELECT ts, open, high, low, close, adjclose, volume FROM bars WHERE symbol=?"
    args = [symbol]
    if start_ts is not None:
        q += " AND ts>=?"
        args.append(int(start_ts))
    if end_ts is not None:
        q += " AND ts<=?"
        args.append(int(end_ts))
    q += " ORDER BY ts"
    with _connect() as con:
        rows = con.execute(q, args).fetchall()
    keys = ["time", "open", "high", "low", "close", "adjclose", "volume"]
    return [dict(zip(keys, r)) for r in rows]


def totals():
    """Row/symbol counts for daily, hourly and 5-minute data, plus DB size in bytes."""
    with _connect() as con:
        rows = con.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
        syms = con.execute("SELECT COUNT(*) FROM sync_state WHERE bars>0").fetchone()[0]
        h_rows, h_syms = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM bars_1h"
        ).fetchone()
        m5_rows, m5_syms = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM bars_5m"
        ).fetchone()
    size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    return {
        "bars": rows, "symbols": syms, "db_bytes": size,
        "hourly_bars": h_rows, "hourly_symbols": h_syms,
        "min5_bars": m5_rows, "min5_symbols": m5_syms,
    }


def sync(symbol, years=0, force=False):
    """Ensure `symbol` has daily bars in the DB and is reasonably fresh.

    years: on a first/full backfill, how far back to fetch (0 = all history Yahoo has).
    force: fetch even if the local copy is still within REFRESH_TTL.
    Returns the sync_state dict. Raises only if there is still no data afterwards.
    """
    symbol = symbol.upper()
    with _lock_for(symbol):
        st = get_state(symbol)
        now = int(time.time())
        have_data = bool(st and (st.get("bars") or 0) > 0)
        stale = not st or (now - (st.get("updated_at") or 0) > REFRESH_TTL)
        if have_data and not force and not stale:
            return st

        try:
            if have_data and st.get("last_ts") and not (force and years == 0):
                # incremental top-up from a week before the last stored bar
                period1 = st["last_ts"] - 7 * 86400
            elif years:
                period1 = now - int(years * 366 * 86400)
            else:
                period1 = marketdata.FULL_START
            upsert_bars(symbol, marketdata.daily_bars(symbol, period1, now))
        except Exception as exc:  # noqa: BLE001
            if not have_data:
                raise
            with _connect() as con:  # keep serving stale data, just record the error
                _refresh_state(con, symbol, error=str(exc)[:200])
        return get_state(symbol)


# interval -> (table, lookback days to fetch, seconds before a re-pull)
_INTRADAY = {
    "1h": ("bars_1h", 365, 6 * 3600),
    "5m": ("bars_5m", 57, 1800),
}


def intraday_table(interval):
    """SQLite table name for an intraday interval ('1h' or '5m')."""
    return _INTRADAY[interval][0]


def intraday_symbols(interval):
    """Symbols that have stored bars at this intraday interval."""
    with _connect() as con:
        rows = con.execute(f"SELECT DISTINCT symbol FROM {intraday_table(interval)}").fetchall()
    return [r[0] for r in rows]


def get_intraday(symbol, interval, start_ts=None):
    """Intraday bars for a symbol from start_ts onward, oldest first, as dicts."""
    tbl = intraday_table(interval)
    q = f"SELECT ts, open, high, low, close, volume FROM {tbl} WHERE symbol=?"
    args = [symbol.upper()]
    if start_ts is not None:
        q += " AND ts>=?"
        args.append(int(start_ts))
    q += " ORDER BY ts"
    with _connect() as con:
        rows = con.execute(q, args).fetchall()
    keys = ["time", "open", "high", "low", "close", "volume"]
    return [dict(zip(keys, r)) for r in rows]


def sync_intraday(symbol, interval, force=False):
    """Fetch/refresh the rolling intraday window for `symbol` into bars_<interval>.

    Yahoo only serves intraday by rolling window, so this re-pulls the whole
    thing (cheap) rather than doing incremental fetches.
    """
    tbl, lookback, fresh = _INTRADAY[interval]
    symbol = symbol.upper()
    with _lock_for(f"{interval}:{symbol}"):
        with _connect() as con:
            cnt, last = con.execute(
                f"SELECT COUNT(*), MAX(ts) FROM {tbl} WHERE symbol=?", (symbol,)
            ).fetchone()
        now = int(time.time())
        if cnt and not force and last and now - last < fresh:
            return {"symbol": symbol, "bars": cnt, "last_ts": last}

        bars = marketdata.intraday_bars(symbol, interval=interval, lookback_days=lookback)
        if bars:
            with _connect() as con:
                con.executemany(f"""
                    INSERT INTO {tbl}(symbol, ts, open, high, low, close, volume)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(symbol, ts) DO UPDATE SET
                        open=excluded.open, high=excluded.high, low=excluded.low,
                        close=excluded.close, volume=excluded.volume
                """, [
                    (symbol, b["ts"], b.get("open"), b.get("high"), b.get("low"),
                     b.get("close"), b.get("volume"))
                    for b in bars
                ])
        with _connect() as con:
            cnt, last = con.execute(
                f"SELECT COUNT(*), MAX(ts) FROM {tbl} WHERE symbol=?", (symbol,)
            ).fetchone()
        return {"symbol": symbol, "bars": cnt, "last_ts": last}


def sync_hourly(symbol, force=False):
    """Shortcut for sync_intraday(symbol, '1h')."""
    return sync_intraday(symbol, "1h", force=force)


def get_hourly(symbol, start_ts=None):
    """Shortcut for get_intraday(symbol, '1h', start_ts)."""
    return get_intraday(symbol, "1h", start_ts)


def resample(daily_rows, interval):
    """Roll daily rows up into '1wk' or '1mo' OHLCV bars (bucket = first bar's ts)."""
    import datetime as dt

    buckets = {}
    order = []
    for r in daily_rows:
        d = dt.datetime.fromtimestamp(r["time"], dt.timezone.utc)
        key = (d.isocalendar()[0], d.isocalendar()[1]) if interval == "1wk" else (d.year, d.month)
        b = buckets.get(key)
        if b is None:
            buckets[key] = {
                "time": r["time"], "open": r["open"], "high": r["high"],
                "low": r["low"], "close": r["close"], "volume": r["volume"] or 0,
            }
            order.append(key)
            continue
        if r["high"] is not None:
            b["high"] = r["high"] if b["high"] is None else max(b["high"], r["high"])
        if r["low"] is not None:
            b["low"] = r["low"] if b["low"] is None else min(b["low"], r["low"])
        b["close"] = r["close"]
        b["volume"] += r["volume"] or 0
    return [buckets[k] for k in order]
