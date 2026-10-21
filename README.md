# Marketscope

A small **local** website for browsing US publicly traded markets — indices,
stocks, ETFs, futures, crypto. Search any ticker, view an interactive price
chart with selectable ranges (1D → MAX), key fundamentals, and a company
profile. Keep a personal watchlist (stored in your browser).

Daily price history is kept in a **local SQLite cache** so charts load instantly
and work offline. Live quotes, intraday bars, search and fundamentals come from
public Yahoo Finance endpoints (`yfinance` for fundamentals). No API key.

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

Starts on <http://127.0.0.1:5000> and opens your browser automatically. The
SQLite file (`data/history.db`) is created on first run.

## Pre-load history (optional but recommended)

```bash
python load_history.py            # built-in ~100 largest US stocks, 10y  (~12s, ~26 MB)
python load_history.py --top 1000 # 1000 largest by market cap, 10y       (~35s, ~235 MB)
```

`--top N` pulls the current N-largest US-listed companies from the Nasdaq stock
screener (one request, ranked by market cap), so there's no hard-coded list to
maintain. More options:

```bash
python load_history.py --top 500 --years 20
python load_history.py --years 0                 # all available history instead of 10y
python load_history.py --symbols AAPL,MSFT,BRK-B
python load_history.py --list-file my_tickers.txt
python load_history.py --top 1000 --verbose      # one line per symbol
```

Any symbol you open that isn't pre-loaded is backfilled on demand (full history)
and then kept current with small incremental fetches.

### Keep it fresh automatically

```bash
python load_history.py --refresh                 # incremental top-up of every stored daily symbol (~30s / 1000)
python load_history.py --refresh --interval 1h   # re-pull hourly for every stored 1h symbol
python load_history.py --refresh --interval 5m
```

Schedule the three `--refresh` commands (daily, then `1h`, then `5m`) with cron or Task Scheduler.
`scripts/refresh.sh` (Linux/macOS) and `scripts/refresh.cmd` (Windows) run all three, logging to `data/refresh.log`.

## How data is served

| Chart range | Source |
|-------------|--------|
| 1M / 6M / YTD / 1Y (daily) | **local SQLite** |
| 5Y (weekly) | **local SQLite**, resampled from daily (falls back to live if not covered) |
| MAX (monthly) | live Yahoo (authoritative for true earliest bar) |
| 1D / 5D (intraday) | live Yahoo |

A badge under each chart shows which source was used. `GET /api/history/status`
reports what the cache holds.

## Top movers

The overview page has a **Top Movers** section (Daily / Weekly tabs, gainers +
losers) computed entirely from the local SQLite cache — no live calls, so it
covers the full stored universe (~1000+ symbols) instantly.

```
GET /api/movers?period=1d&n=12   # period: 1d | 5d
```

Moves bigger than 150% (1d) / 300% (5d) are dropped as bad data (thin
preferreds, bad ticks) rather than real gainers/losers.

## Local analytics

Computed on the fly from the SQLite cache (pandas/numpy, no network), shown on
every ticker page:

- **Analytics** — annualised return / volatility, Sharpe, Sortino, max drawdown,
  beta and correlation vs SPY, up-day rate, best/worst day (3-year daily window).
- **Co-movement** — the local universe ranked by daily-return correlation with
  the stock, with per-name up-day corr, down-day corr, beta, and up/down capture
  ratios. Toggle: overall · on up days · on down days · symmetric (min of the two).
- **Lead / lag** — intraday cross-correlation `corr(peer_t, target_{t+k})`;
  classifies each name as leading, following or coincident. Toggle **1h** (k = ±3h,
  ~1y of bars) or **5m** (k = ±30min, ~57d). Needs intraday bars:
  `python load_history.py --interval 1h --symbols NVDA,SMH,…` and/or `--interval 5m`
  (stored in `bars_1h` / `bars_5m`).

- **Tech-cycle exposure** — `partial_tech` = corr(symbol, XLK | SPY): how much a
  name moves with the tech sector *after* removing the broad-market move, plus a
  tech-residual beta and its NVDA correlation for contrast. Surfaces indirect
  tech plays (uranium, Taiwan/Korea, copper) and rotation counterweights
  (staples, financials, REITs).

```
GET /api/stats?symbol=NVDA&window=3y
GET /api/similar?symbol=NVDA&rank=symmetric&window=2y&n=25   # rank: overall|up|down|symmetric
GET /api/leadlag?symbol=NVDA&interval=5m                    # lead/lag vs stored peers (interval: 1h|5m)
GET /api/factor?symbol=URA&window=2y                        # tech-cycle exposure vs XLK, controlling for SPY
```

The bigger the pre-loaded universe (`load_history.py --top 1000`), the more
meaningful the rankings.

## Notes

- Data is delayed and best-effort. **For personal use only — not investment advice.**
- Yahoo's floor for daily history: ~1962 for old blue chips, 1927 for `^GSPC`,
  IPO date otherwise. Intraday history is short (1-min ≈ 7 days, 1-hour ≈ 2 years).
- Fundamentals (`/api/profile`) come from `yfinance` and are occasionally sparse
  or slow on the first hit for a symbol.
- Live upstream calls are cached in memory 20–900s.

## Layout

| Path                   | Purpose                                       |
|------------------------|-----------------------------------------------|
| `app.py`               | Flask server + JSON API                       |
| `marketdata.py`        | Stateless Yahoo fetch helpers                 |
| `store.py`             | SQLite cache (schema, CRUD, `sync`, resample) |
| `analysis.py`          | Returns matrix, per-stock stats, co-movement  |
| `load_history.py`      | Bulk backfill CLI (`--top N` via Nasdaq screener) |
| `templates/index.html` | Single-page shell                             |
| `static/js/app.js`     | Router, views, chart, search, watchlist       |
| `static/css/style.css` | Dark/light finance theme                      |
| `data/history.db`      | Local SQLite cache (created at runtime)       |
