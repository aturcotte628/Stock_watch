"""Local analytics over the SQLite bar cache: per-stock risk/return stats and
correlation-based similarity. Pure pandas/numpy, no network.

The daily-returns matrix for every stored symbol is built once and memoised;
adding a new symbol busts the cache so it gets picked up on the next call.
"""

import sqlite3
import threading
import time

import numpy as np
import pandas as pd

import store

# trading-day lookbacks
WINDOWS = {"6mo": 126, "1y": 252, "2y": 504, "3y": 756, "5y": 1260, "max": 10_000_000}
_BENCH = "SPY"

_cache = {}
_cache_lock = threading.Lock()
_TTL = 1800
_RET_KEY = "returns"


def _returns():
    """Daily-returns matrix (rows = calendar day, columns = symbol) from the bar cache, memoised for _TTL."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(_RET_KEY)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    con = sqlite3.connect(store.DB_PATH)
    try:
        px = pd.read_sql_query(
            "SELECT symbol, ts, COALESCE(adjclose, close) AS px FROM bars ORDER BY ts", con
        )
    finally:
        con.close()
    # Index on the calendar day, not the raw epoch: crypto stamps daily bars at
    # 00:00 UTC and DST shifts equities between 13:30/14:30 UTC, which would
    # otherwise fragment the grid and null out pct_change everywhere.
    px["day"] = px["ts"] // 86400
    px = px.drop_duplicates(["symbol", "day"], keep="last")
    wide = px.pivot(index="day", columns="symbol", values="px").sort_index()
    # keep only real market days (drop crypto-only weekend/holiday rows)
    ref = "SPY" if "SPY" in wide.columns else wide.notna().sum().idxmax()
    wide = wide[wide[ref].notna()]
    rets = wide.pct_change(fill_method=None)  # NaN across gaps, don't forward-fill
    with _cache_lock:
        _cache[_RET_KEY] = (now, rets)
    return rets


def _ensure(*symbols):
    """Return the returns matrix, backfilling any missing symbol first."""
    rets = _returns()
    missing = [s for s in symbols if s and s not in rets.columns]
    if not missing:
        return rets
    for s in missing:
        try:
            store.sync(s)
        except Exception:  # noqa: BLE001 - benchmark / bad ticker is non-fatal
            pass
    with _cache_lock:
        _cache.pop(_RET_KEY, None)
    return _returns()


def _f(x, ndigits=6):
    """Round to a JSON-safe float; None for non-numbers and NaN."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if x != x else round(x, ndigits)


def coverage():
    """Size of the returns matrix: how many symbols and trading days are available."""
    rets = _returns()
    return {"symbols": int(rets.shape[1]), "rows": int(rets.shape[0])}


# max plausible single-period move before we treat it as bad data (thin
# preferreds / bad ticks) rather than a real gainer/loser
_MOVER_CAP = {"1d": 1.5, "5d": 3.0}


def movers(period="1d", n=12):
    """Top gainers/losers across every locally-stored symbol, from SQLite only."""
    if period not in _MOVER_CAP:
        raise ValueError("period must be '1d' or '5d'")
    rets = _returns()
    if period == "5d":
        s = (1 + rets.tail(5)).prod(min_count=4) - 1
    else:
        s = rets.iloc[-1]
    s = s.dropna()
    s = s[s.abs() < _MOVER_CAP[period]]
    gainers = s.sort_values(ascending=False).head(n)
    losers = s.sort_values(ascending=True).head(n)
    return {
        "period": period,
        "as_of_day": int(rets.index[-1]) * 86400,
        "universe": int(len(s)),
        "gainers": [{"symbol": k, "change": _f(v, 4)} for k, v in gainers.items()],
        "losers": [{"symbol": k, "change": _f(v, 4)} for k, v in losers.items()],
    }


_RANK_COL = {
    "overall": "correlation",
    "up": "corr_up",
    "down": "corr_down",
    "symmetric": "sym_track",   # min(corr_up, corr_down) - tracks NVDA both ways
}


def similar(symbol, window="2y", n=25, rank="overall", min_overlap_frac=0.8):
    """Rank the local universe by daily-return co-movement with `symbol`.

    rank: 'overall' | 'up' (NVDA-up days only) | 'down' | 'symmetric'.
    Also returns per-name up/down capture ratios and beta vs the target.
    """
    symbol = symbol.upper()
    rets = _ensure(symbol)
    if symbol not in rets.columns:
        raise ValueError(f"{symbol}: no local history (run load_history.py or open its page once)")

    win = rets.tail(WINDOWS.get(window, 504))
    target = win[symbol].dropna()
    min_overlap = max(30, int(len(target) * min_overlap_frac))
    up_idx = target.index[target > 0]
    dn_idx = target.index[target < 0]
    tgt_up_mean = target.loc[up_idx].mean()
    tgt_dn_mean = target.loc[dn_idx].mean()

    out = []
    for col in win.columns:
        if col == symbol:
            continue
        x, t = win[col].align(target, join="inner")
        mask = x.notna() & t.notna()
        x, t = x[mask], t[mask]
        if len(x) < min_overlap:
            continue
        u = t > 0
        d = t < 0
        var_t = t.var()
        out.append({
            "symbol": col,
            "correlation": x.corr(t),
            "corr_up": x[u].corr(t[u]),
            "corr_down": x[d].corr(t[d]),
            "beta": (np.cov(x, t)[0, 1] / var_t) if var_t else np.nan,
            "up_capture": (x[u].mean() / tgt_up_mean) if tgt_up_mean else np.nan,
            "down_capture": (x[d].mean() / tgt_dn_mean) if tgt_dn_mean else np.nan,
            "overlap": int(len(x)),
        })

    table = pd.DataFrame(out)
    if table.empty:
        return {"symbol": symbol, "window": window, "rank": rank, "results": []}
    table["sym_track"] = table[["corr_up", "corr_down"]].min(axis=1)
    sort_col = _RANK_COL.get(rank, "correlation")
    table = table[table[sort_col].notna()].sort_values(sort_col, ascending=False).head(n)

    return {
        "symbol": symbol,
        "window": window,
        "rank": rank,
        "observations": int(len(target)),
        "up_days": int(len(up_idx)),
        "down_days": int(len(dn_idx)),
        "universe": int(rets.shape[1]),
        "results": [
            {
                "symbol": r.symbol,
                "correlation": _f(r.correlation, 3),
                "corr_up": _f(r.corr_up, 3),
                "corr_down": _f(r.corr_down, 3),
                "beta": _f(r.beta, 3),
                "up_capture": _f(r.up_capture, 3),
                "down_capture": _f(r.down_capture, 3),
                "overlap": int(r.overlap),
            }
            for r in table.itertuples()
        ],
    }


# interval -> (seconds per bar, gap threshold to null a return, default +/- lag span)
_INTRADAY_META = {"1h": (3600, 7200, 3), "5m": (300, 1200, 6)}


def _intraday_returns(symbols, interval):
    """Wide frame of within-session returns at `interval` (session gaps -> NaN)."""
    bar_s, gap_s, _ = _INTRADAY_META[interval]
    symbols = tuple(sorted({s.upper() for s in symbols}))
    key = ("intraday", interval, symbols)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < 600:
            return hit[1]
    tbl = store.intraday_table(interval)
    con = sqlite3.connect(store.DB_PATH)
    try:
        marks = ",".join("?" * len(symbols))
        px = pd.read_sql_query(
            f"SELECT symbol, ts, close FROM {tbl} WHERE symbol IN ({marks}) ORDER BY ts",
            con, params=list(symbols),
        )
    finally:
        con.close()
    wide = px.pivot(index="ts", columns="symbol", values="close").sort_index()
    rets = wide.pct_change(fill_method=None)
    session_gap = pd.Series(wide.index, index=wide.index).diff() > gap_s
    rets[session_gap.values] = np.nan
    with _cache_lock:
        _cache[key] = (now, rets)
    return rets


def _hourly_returns(symbols):
    """Shortcut for _intraday_returns(symbols, '1h')."""
    return _intraday_returns(symbols, "1h")


def leadlag(symbol, interval="1h", peers=None, max_lag=None, min_overlap=200):
    """Intraday lead/lag of each peer vs `symbol` at `interval` ('1h' or '5m').

    corr(peer_t, target_{t+k}):  k>0 => peer leads target by k bars,
    k<0 => peer follows target. Classifies by which |correlation| is largest.
    """
    if interval not in _INTRADAY_META:
        raise ValueError(f"unsupported interval {interval!r}")
    bar_s, _, default_lag = _INTRADAY_META[interval]
    max_lag = default_lag if max_lag is None else int(max_lag)
    symbol = symbol.upper()
    stored = set(store.intraday_symbols(interval))
    if symbol not in stored:
        raise ValueError(f"{symbol}: no {interval} data (run load_history.py --interval {interval})")
    peers = [p.upper() for p in (peers or sorted(stored - {symbol}))]
    peers = [p for p in peers if p in stored]

    rets = _intraday_returns([symbol] + peers, interval)
    tgt = rets[symbol]
    lags = list(range(-max_lag, max_lag + 1))
    results = []
    for p in peers:
        if p not in rets.columns:
            continue
        pair = pd.concat([rets[p].rename("p"), tgt.rename("t")], axis=1).dropna()
        if len(pair) < min_overlap:
            continue
        cc = {k: pair["p"].corr(pair["t"].shift(-k)) for k in lags}
        cc = {k: (v if v == v else 0.0) for k, v in cc.items()}
        contemp = cc[0]
        best_k = max(lags, key=lambda k: abs(cc[k]))
        lead = max((cc[k] for k in lags if k > 0), default=0.0)
        follow = max((cc[k] for k in lags if k < 0), default=0.0)
        unit = "h" if interval == "1h" else "×5m"
        if best_k == 0 or abs(cc[best_k]) - abs(contemp) < 0.02:
            verdict = "coincident"
        elif best_k > 0:
            verdict = f"leads {symbol} by {best_k}{unit}"
        else:
            verdict = f"follows {symbol} by {-best_k}{unit}"
        results.append({
            "symbol": p,
            "contemp": _f(contemp, 3),
            "lead_best": _f(lead, 3),
            "follow_best": _f(follow, 3),
            "best_lag_h": int(best_k),
            "best_lag_min": int(best_k * bar_s / 60),
            "best_corr": _f(cc[best_k], 3),
            "direction": "inverse" if contemp < 0 else "same",
            "verdict": verdict,
            "overlap": int(len(pair)),
        })
    results.sort(key=lambda r: abs(r["contemp"] or 0), reverse=True)
    return {
        "symbol": symbol,
        "interval": interval,
        "bars": int(tgt.notna().sum()),
        "max_lag": max_lag,
        "peers": len(results),
        "results": results,
    }


def _pair_corr(a, b):
    """Correlation of two return series on their overlap; NaN if under 60 shared points."""
    p = pd.concat([a, b], axis=1).dropna()
    return p.iloc[:, 0].corr(p.iloc[:, 1]) if len(p) > 60 else float("nan")


def factor(symbol, window="2y", tech="XLK", market="SPY"):
    """Exposure of `symbol` to the tech sector beyond the broad market.

    partial_tech = corr(symbol, XLK | SPY): co-movement with tech after removing
    the part explained by SPY. tech_resid_beta scales that. corr_nvda is shown so
    a name can be seen to be tech-driven WITHOUT being an NVDA/semis play.
    """
    symbol = symbol.upper()
    rets = _ensure(symbol, market, tech, "NVDA")
    for need in (symbol, market, tech):
        if need not in rets.columns:
            raise ValueError(f"{need}: no local history")
    win = rets.tail(WINDOWS.get(window, 504))
    x, m, t = win[symbol], win[market], win[tech]
    nv = win["NVDA"] if "NVDA" in win.columns else None

    r_xm, r_xt, r_tm = _pair_corr(x, m), _pair_corr(x, t), _pair_corr(t, m)
    denom = np.sqrt(max(1e-9, (1 - r_xm ** 2) * (1 - r_tm ** 2)))
    partial = (r_xt - r_xm * r_tm) / denom if np.isfinite([r_xm, r_xt, r_tm]).all() else float("nan")

    pair = pd.concat([x, t, m], axis=1).dropna()
    pair.columns = ["x", "t", "m"]
    tbeta = float("nan")
    beta_spy = float("nan")
    if len(pair) > 60:
        vm = pair.m.var()
        beta_spy = np.cov(pair.x, pair.m)[0, 1] / vm
        ex = pair.x - beta_spy * pair.m
        et = pair.t - (np.cov(pair.t, pair.m)[0, 1] / vm) * pair.m
        tbeta = np.cov(ex, et)[0, 1] / et.var()

    return {
        "symbol": symbol, "window": window, "tech": tech, "market": market,
        "observations": int(len(pair)),
        "corr_market": _f(r_xm, 3),
        "corr_tech": _f(r_xt, 3),
        "corr_nvda": _f(_pair_corr(x, nv), 3) if nv is not None else None,
        "beta_market": _f(beta_spy, 3),
        "partial_tech": _f(partial, 3),
        "tech_resid_beta": _f(tbeta, 3),
    }


def stats(symbol, window="3y"):
    """Annualised return/volatility, Sharpe, Sortino, max drawdown and beta vs SPY.

    Raises ValueError if the symbol has no local history or under 30 observations.
    Sharpe and Sortino use a 0% risk-free rate.
    """
    symbol = symbol.upper()
    rets = _ensure(symbol, _BENCH)
    if symbol not in rets.columns:
        raise ValueError(f"{symbol}: no local history")

    win = rets.tail(WINDOWS.get(window, 756))
    s = win[symbol].dropna()
    if len(s) < 30:
        raise ValueError("not enough local history for stats")

    ann_return = (1 + s).prod() ** (252 / len(s)) - 1
    ann_vol = s.std() * np.sqrt(252)
    downside = s[s < 0].std() * np.sqrt(252)
    curve = (1 + s).cumprod()
    max_dd = float((curve / curve.cummax() - 1).min())

    beta = corr_b = None
    if _BENCH in win.columns:
        pair = win[[symbol, _BENCH]].dropna()
        if len(pair) > 30:
            cov = pair.cov()
            var_b = cov.loc[_BENCH, _BENCH]
            beta = cov.loc[symbol, _BENCH] / var_b if var_b else None
            corr_b = pair[symbol].corr(pair[_BENCH])

    return {
        "symbol": symbol,
        "window": window,
        "observations": int(len(s)),
        "ann_return": _f(ann_return),
        "ann_vol": _f(ann_vol),
        "sharpe": _f(ann_return / ann_vol if ann_vol else None, 3),
        "sortino": _f(ann_return / downside if downside else None, 3),
        "max_drawdown": _f(max_dd),
        "beta_spy": _f(beta, 3),
        "corr_spy": _f(corr_b, 3),
        "best_day": _f(s.max()),
        "worst_day": _f(s.min()),
        "positive_days": _f((s > 0).mean()),
    }
