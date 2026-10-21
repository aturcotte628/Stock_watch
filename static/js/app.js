"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const app = $("#app");

/* ------------------------------------------------------------------ helpers */
const WATCH_KEY = "marketscope.watchlist";
const THEME_KEY = "marketscope.theme";
const DEFAULT_WATCH = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "TSLA", "SPY"];

function loadWatch() {
  try {
    const v = JSON.parse(localStorage.getItem(WATCH_KEY));
    if (Array.isArray(v) && v.length) return v;
  } catch (_) {}
  return DEFAULT_WATCH.slice();
}
function saveWatch(list) {
  try { localStorage.setItem(WATCH_KEY, JSON.stringify(list)); } catch (_) {}
}

const num = (n) => (n === null || n === undefined || Number.isNaN(n));
function price(n) {
  if (num(n)) return "–";
  const abs = Math.abs(n);
  const dp = abs >= 1000 ? 2 : abs >= 1 ? 2 : 4;
  return n.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
}
function signed(n, dp = 2) {
  if (num(n)) return "–";
  return (n >= 0 ? "+" : "") + n.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
}
function pct(n) { return num(n) ? "–" : signed(n, 2) + "%"; }
function big(n) {
  if (num(n)) return "–";
  const a = Math.abs(n);
  if (a >= 1e12) return (n / 1e12).toFixed(2) + "T";
  if (a >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return (n / 1e3).toFixed(2) + "K";
  return String(n);
}
function intc(n) { return num(n) ? "–" : Math.round(n).toLocaleString(); }
function dirClass(n) { return num(n) ? "flat" : n > 0 ? "up" : n < 0 ? "down" : "flat"; }
function divYield(p) {
  // yfinance returns `dividendYield` already as a percentage (e.g. 2.41 = 2.41%).
  const y = p.dividendYield;
  if (num(y) || y <= 0 || y > 60) return "–";
  return y.toFixed(2) + "%";
}
function arrow(n) { return num(n) || n === 0 ? "" : n > 0 ? "▲ " : "▼ "; }

async function getJSON(url) {
  const r = await fetch(url);
  const data = await r.json().catch(() => ({ error: "bad response" }));
  if (!r.ok || (data && data.error)) throw new Error((data && data.error) || r.statusText);
  return data;
}

/* ------------------------------------------------------------------ router */
function currentRoute() {
  const h = location.hash.replace(/^#\/?/, "").trim();
  return h ? decodeURIComponent(h).toUpperCase() : "";
}
window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", () => {
  initSearch();
  initTheme();
  route();
});

function route() {
  clearPicks();
  const sym = currentRoute();
  if (sym) renderDetail(sym);
  else renderOverview();
}

/* ------------------------------------------------------------------ overview */
async function renderOverview() {
  document.title = "Marketscope";
  app.innerHTML = `
    <div class="section-title">Markets</div>
    <div class="grid" id="ov-grid"><div class="loading">Loading markets…</div></div>

    <div class="section-title" id="mv-title">Top Movers</div>
    <div class="sim-toolbar" id="mv-tabs">
      <button class="type-btn active" data-period="1d">Daily</button>
      <button class="type-btn" data-period="5d">Weekly</button>
    </div>
    <div class="movers-grid" id="mv-grid"><div class="loading">Computing…</div></div>

    <div class="section-title">Watchlist</div>
    <div class="wl-wrap" id="wl-wrap"><div class="loading">Loading watchlist…</div></div>`;

  getJSON("/api/overview").then((rows) => {
    $("#ov-grid").innerHTML = rows.map(quoteCard).join("") || `<div class="chart-msg">No data.</div>`;
    wireCards($("#ov-grid"));
  }).catch((e) => { $("#ov-grid").innerHTML = `<div class="error-box">${e.message}</div>`; });

  $("#mv-tabs").querySelectorAll("[data-period]").forEach((b) => {
    b.addEventListener("click", () => {
      $("#mv-tabs").querySelectorAll("[data-period]").forEach((x) => x.classList.toggle("active", x === b));
      loadMovers(b.dataset.period);
    });
  });
  loadMovers("1d");

  renderWatchlist();
}

async function loadMovers(period) {
  const grid = $("#mv-grid");
  grid.innerHTML = `<div class="loading">Computing…</div>`;
  let data;
  try { data = await getJSON(`/api/movers?period=${period}&n=12`); }
  catch (e) { grid.innerHTML = `<div class="error-box">${e.message}</div>`; return; }

  const rows = [...data.gainers, ...data.losers];
  let quotes = {};
  if (rows.length) {
    try {
      const qs = await getJSON("/api/quotes?symbols=" + encodeURIComponent(rows.map((r) => r.symbol).join(",")));
      qs.forEach((q) => { quotes[q.symbol] = q; });
    } catch (_) {}
  }

  const label = period === "5d" ? "Weekly" : "Daily";
  const when = data.as_of_day ? new Date(data.as_of_day * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "";
  $("#mv-title").textContent = `Top Movers · ${label}${when ? " (as of " + when + ")" : ""} · ${data.universe} symbols`;

  const side = (title, list, cls) => `
    <div class="wl-wrap mv-side">
      <div class="mv-head">${title}</div>
      ${list.length ? `<table class="wl mv-table"><tbody>
        ${list.map((r) => {
          const q = quotes[r.symbol] || {};
          return `<tr data-sym="${r.symbol}">
            <td><span class="sym">${r.symbol}</span>${q.name && q.name !== r.symbol ? `<span class="nm">${q.name}</span>` : ""}</td>
            <td class="${cls}">${signed(r.change * 100)}%</td>
          </tr>`;
        }).join("")}
      </tbody></table>` : `<div class="wl-empty">No data.</div>`}
    </div>`;

  grid.innerHTML = side("Gainers", data.gainers, "up") + side("Losers", data.losers, "down");
  grid.querySelectorAll("tbody tr").forEach((tr) => {
    tr.addEventListener("click", () => { location.hash = "#/" + tr.dataset.sym; });
  });
}

function quoteCard(q) {
  if (q.error) return `<div class="qcard" data-sym="${q.symbol}">
      <div class="label">${q.symbol}</div><div class="price">–</div>
      <div class="chg flat">unavailable</div></div>`;
  const c = dirClass(q.change);
  return `<div class="qcard" data-sym="${q.symbol}">
      <div class="label">${q.label || q.name || ""}</div>
      <div class="sym">${q.symbol}</div>
      <div class="price">${price(q.price)}</div>
      <div class="chg ${c}">${arrow(q.change)}${signed(q.change)} (${pct(q.changePercent)})</div>
    </div>`;
}

function wireCards(scope) {
  scope.querySelectorAll(".qcard").forEach((el) => {
    el.addEventListener("click", () => { location.hash = "#/" + el.dataset.sym; });
  });
}

async function renderWatchlist() {
  const wrap = $("#wl-wrap");
  const list = loadWatch();
  if (!list.length) { wrap.innerHTML = `<div class="wl-empty">Your watchlist is empty. Search for a ticker and add it.</div>`; return; }
  let rows;
  try {
    rows = await getJSON("/api/quotes?symbols=" + encodeURIComponent(list.join(",")));
  } catch (e) {
    wrap.innerHTML = `<div class="error-box">${e.message}</div>`; return;
  }
  wrap.innerHTML = `
    <table class="wl">
      <thead><tr><th>Symbol</th><th>Last</th><th>Change</th><th>% Change</th><th></th></tr></thead>
      <tbody>
        ${rows.map((q) => {
          const c = dirClass(q.change);
          return `<tr data-sym="${q.symbol}">
            <td><span class="sym">${q.symbol}</span>${q.name && q.name !== q.symbol ? `<span class="nm">${q.name}</span>` : ""}</td>
            <td>${price(q.price)}</td>
            <td class="${c}">${signed(q.change)}</td>
            <td class="${c}">${pct(q.changePercent)}</td>
            <td><button class="wl-remove" data-rm="${q.symbol}" title="Remove">✕</button></td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>`;
  wrap.querySelectorAll("tbody tr").forEach((tr) => {
    tr.addEventListener("click", (ev) => {
      if (ev.target.dataset.rm) return;
      location.hash = "#/" + tr.dataset.sym;
    });
  });
  wrap.querySelectorAll("[data-rm]").forEach((b) => {
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      saveWatch(loadWatch().filter((s) => s !== b.dataset.rm));
      renderWatchlist();
    });
  });
}

/* ------------------------------------------------------------------ detail */
const RANGES = [
  ["1D", { range: "1d", interval: "5m" }],
  ["5D", { range: "5d", interval: "30m" }],
  ["1M", { range: "1mo", interval: "1d" }],
  ["6M", { range: "6mo", interval: "1d" }],
  ["YTD", { range: "ytd", interval: "1d" }],
  ["1Y", { range: "1y", interval: "1d" }],
  ["5Y", { range: "5y", interval: "1wk" }],
  ["MAX", { range: "max", interval: "1mo" }],
];

let chart, priceSeries, volSeries, chartState, hoverBar;

async function renderDetail(symbol) {
  document.title = symbol + " · Marketscope";
  app.innerHTML = `
    <div class="detail-head">
      <div class="name" id="d-name">${symbol}</div>
      <div class="meta" id="d-meta"></div>
      <button class="btn-watch" id="d-watch"></button>
    </div>
    <div class="detail-price-row">
      <div class="detail-price" id="d-price">–</div>
      <div class="detail-change" id="d-change"></div>
    </div>
    <div class="detail-sub" id="d-sub"></div>

    <div class="chart-card">
      <div class="chart-toolbar">
        ${RANGES.map(([lbl], i) => `<button class="range-btn${i === 5 ? " active" : ""}" data-r="${i}">${lbl}</button>`).join("")}
        <button class="type-btn active" data-type="area">Line</button>
        <button class="type-btn" data-type="candle">Candles</button>
      </div>
      <div class="chart-holder">
        <div id="chart"></div>
        <div class="chart-legend" id="chart-legend" hidden></div>
      </div>
      <div class="chart-msg" id="chart-msg" hidden></div>
      <div class="chart-foot"><span class="src-badge" id="chart-src" hidden></span><span class="measure-hint">Ctrl+click two points or values to measure the % between them</span></div>
    </div>

    <div class="stats-grid" id="d-stats"></div>
    <div class="about" id="d-about" hidden></div>

    <div class="section-title">Analytics · 3Y daily</div>
    <div class="stats-grid" id="d-analytics"><div class="loading">Computing…</div></div>

    <div class="section-title">Tech-cycle exposure · 2Y</div>
    <div class="stats-grid" id="d-factor"><div class="loading">Computing…</div></div>
    <div class="sub-hint" id="d-factor-hint"></div>

    <div class="section-title" id="d-sim-title">Co-movement</div>
    <div class="sim-toolbar" id="d-sim-tabs">
      <button class="type-btn active" data-rank="overall">Overall</button>
      <button class="type-btn" data-rank="up">On up days</button>
      <button class="type-btn" data-rank="down">On down days</button>
      <button class="type-btn" data-rank="symmetric">Symmetric</button>
      <button class="type-btn" data-rank="leadlag">Lead / lag · 1h</button>
    </div>
    <div class="wl-wrap" id="d-similar"><div class="loading">Computing…</div></div>`;

  chartState = { symbol, rangeIdx: 5, type: "area" };
  setupChart();

  $("#d-watch").addEventListener("click", () => {
    let list = loadWatch();
    if (list.includes(symbol)) list = list.filter((s) => s !== symbol);
    else list = [symbol, ...list];
    saveWatch(list);
    paintWatchBtn(symbol);
  });
  paintWatchBtn(symbol);

  app.querySelectorAll(".range-btn").forEach((b) => {
    b.addEventListener("click", () => {
      app.querySelectorAll(".range-btn").forEach((x) => x.classList.toggle("active", x === b));
      chartState.rangeIdx = +b.dataset.r;
      loadChart();
    });
  });
  app.querySelectorAll(".chart-toolbar .type-btn").forEach((b) => {
    b.addEventListener("click", () => {
      app.querySelectorAll(".chart-toolbar .type-btn").forEach((x) => x.classList.toggle("active", x === b));
      chartState.type = b.dataset.type;
      setupChart();
      loadChart();
    });
  });

  $("#d-sim-title").textContent = `Moves most like ${symbol}`;
  $("#d-sim-tabs").querySelectorAll("[data-rank]").forEach((b) => {
    b.addEventListener("click", () => {
      $("#d-sim-tabs").querySelectorAll("[data-rank]").forEach((x) => x.classList.toggle("active", x === b));
      loadSimilar(symbol, b.dataset.rank);
    });
  });

  loadChart();
  loadProfile(symbol);
  loadAnalytics(symbol);
  loadFactor(symbol);
  loadSimilar(symbol, simRank);
}

async function loadFactor(symbol) {
  const el = $("#d-factor");
  let f;
  try { f = await getJSON("/api/factor?symbol=" + encodeURIComponent(symbol)); }
  catch (e) { el.innerHTML = `<div class="stat"><div class="v flat">${e.message}</div></div>`; return; }
  const nn = (x) => num(x) ? "–" : x.toFixed(2);
  const rows = [
    ["β vs market (SPY)", nn(f.beta_market), ""],
    ["Corr vs tech (XLK)", nn(f.corr_tech), ""],
    ["Partial tech corr", nn(f.partial_tech), dirClass(f.partial_tech)],
    ["Tech-residual β", nn(f.tech_resid_beta), dirClass(f.tech_resid_beta)],
    ["Corr vs NVDA", nn(f.corr_nvda), ""],
  ];
  el.innerHTML = rows.map(([k, v, c]) =>
    `<div class="stat"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("");
  const p = f.partial_tech;
  let verdict = "moves with tech about as much as the market does";
  if (!num(p)) {
    if (p >= 0.3) verdict = "strong tech-cycle exposure beyond the market — an indirect tech play";
    else if (p >= 0.12) verdict = "moderate tech-cycle exposure beyond the market";
    else if (p <= -0.3) verdict = "moves against tech beyond the market — a rotation counterweight";
    else if (p <= -0.12) verdict = "leans against tech beyond the market";
  }
  $("#d-factor-hint").textContent =
    `Partial tech corr = co-movement with XLK after removing SPY. This name: ${verdict}.`;
}

async function loadAnalytics(symbol) {
  const el = $("#d-analytics");
  let s;
  try { s = await getJSON("/api/stats?symbol=" + encodeURIComponent(symbol)); }
  catch (e) { el.innerHTML = `<div class="stat"><div class="v flat">${e.message}</div></div>`; return; }
  const p1 = (x) => num(x) ? "–" : signed(x * 100, 1) + "%";
  const p0 = (x) => num(x) ? "–" : (x * 100).toFixed(1) + "%";
  const nn = (x) => num(x) ? "–" : x.toFixed(2);
  const rows = [
    ["Ann. return", p1(s.ann_return), dirClass(s.ann_return)],
    ["Ann. volatility", p0(s.ann_vol), ""],
    ["Sharpe", nn(s.sharpe), dirClass(s.sharpe)],
    ["Sortino", nn(s.sortino), dirClass(s.sortino)],
    ["Max drawdown", p0(s.max_drawdown), "down"],
    ["Beta vs SPY", nn(s.beta_spy), ""],
    ["Corr vs SPY", nn(s.corr_spy), ""],
    ["Up days", p0(s.positive_days), ""],
    ["Best day", p1(s.best_day), "up"],
    ["Worst day", p1(s.worst_day), "down"],
  ];
  el.innerHTML = rows.map(([k, v, c]) =>
    `<div class="stat"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("");
}

let simGen = 0;
let llInterval = "1h";

async function loadLeadLag(symbol) {
  const gen = ++simGen;
  const wrap = $("#d-similar");
  wrap.innerHTML = `<div class="loading">Computing…</div>`;
  let data;
  try { data = await getJSON(`/api/leadlag?symbol=${encodeURIComponent(symbol)}&interval=${llInterval}&n=30`); }
  catch (e) {
    if (gen === simGen) wrap.innerHTML = `<div class="wl-empty">${e.message}<br>Load bars with <code>python load_history.py --interval ${llInterval} --symbols ${symbol},SMH,QQQ,…</code></div>`;
    return;
  }
  if (gen !== simGen) return;
  const rows = data.results || [];
  const unitLabel = llInterval === "1h" ? "hour" : "5-min";
  const spanLabel = llInterval === "1h" ? "last ~1Y" : "last ~57d";
  if (!rows.length) { wrap.innerHTML = `<div class="wl-empty">No overlapping ${llInterval} data.</div>`; return; }
  let quotes = {};
  try {
    const qs = await getJSON("/api/quotes?symbols=" + encodeURIComponent(rows.map((r) => r.symbol).join(",")));
    qs.forEach((q) => { quotes[q.symbol] = q; });
  } catch (_) {}
  if (gen !== simGen) return;
  const nn = (x) => num(x) ? "–" : x.toFixed(2);
  wrap.innerHTML = `
    <div class="sim-note">
      <span class="ll-switch">${["1h", "5m"].map((iv) =>
        `<button class="mini-btn${iv === llInterval ? " active" : ""}" data-iv="${iv}">${iv}</button>`).join("")}</span>
      ${data.bars} bars · ${spanLabel} · corr(peer<sub>t</sub>, ${symbol}<sub>t+k</sub>): k&gt;0 ⇒ peer leads ${symbol}
    </div>
    <table class="wl">
      <thead><tr><th>Symbol</th><th>Same-${unitLabel} corr</th><th>Lead (k&gt;0)</th><th>Follow (k&lt;0)</th><th>Best lag</th><th>Relationship</th></tr></thead>
      <tbody>
        ${rows.map((r) => {
          const q = quotes[r.symbol] || {};
          const rel = r.direction === "inverse" ? "inverse" : r.verdict;
          const lag = r.best_lag_h === 0 ? "0" : (r.best_lag_min > 0 ? "+" : "") + r.best_lag_min + "m";
          return `<tr data-sym="${r.symbol}">
            <td><span class="sym">${r.symbol}</span>${q.name && q.name !== r.symbol ? `<span class="nm">${q.name}</span>` : ""}</td>
            <td>${nn(r.contemp)}</td>
            <td>${nn(r.lead_best)}</td>
            <td>${nn(r.follow_best)}</td>
            <td>${lag}</td>
            <td class="${r.best_lag_h === 0 ? "flat" : ""}">${rel}</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>`;
  wrap.querySelectorAll(".ll-switch [data-iv]").forEach((b) => {
    b.addEventListener("click", () => { llInterval = b.dataset.iv; loadLeadLag(symbol); });
  });
  wrap.querySelectorAll("tbody tr").forEach((tr) => {
    tr.addEventListener("click", () => { location.hash = "#/" + tr.dataset.sym; });
  });
}

async function loadSimilar(symbol, rank) {
  if (rank === "leadlag") return loadLeadLag(symbol);
  const gen = ++simGen;
  const wrap = $("#d-similar");
  wrap.innerHTML = `<div class="loading">Computing…</div>`;
  let data;
  try { data = await getJSON(`/api/similar?symbol=${encodeURIComponent(symbol)}&rank=${rank}&n=20`); }
  catch (e) { if (gen === simGen) wrap.innerHTML = `<div class="error-box">${e.message}</div>`; return; }
  if (gen !== simGen) return;
  const rows = data.results || [];
  if (!rows.length) { wrap.innerHTML = `<div class="wl-empty">No local history to compare against. Run <code>load_history.py</code>.</div>`; return; }

  let quotes = {};
  try {
    const qs = await getJSON("/api/quotes?symbols=" + encodeURIComponent(rows.map((r) => r.symbol).join(",")));
    qs.forEach((q) => { quotes[q.symbol] = q; });
  } catch (_) {}
  if (gen !== simGen) return;

  const cap = (x) => num(x) ? "–" : x.toFixed(2) + "×";
  wrap.innerHTML = `
    <div class="sim-note">2Y daily returns · ${data.observations} obs (${data.up_days} up / ${data.down_days} down) · universe ${data.universe}</div>
    <table class="wl">
      <thead><tr><th>Symbol</th><th>Corr</th><th>↑ days</th><th>↓ days</th><th>β</th><th>↑ capt</th><th>↓ capt</th><th>Last</th></tr></thead>
      <tbody>
        ${rows.map((r) => {
          const q = quotes[r.symbol] || {};
          return `<tr data-sym="${r.symbol}">
            <td><span class="sym">${r.symbol}</span>${q.name && q.name !== r.symbol ? `<span class="nm">${q.name}</span>` : ""}</td>
            <td>${num(r.correlation) ? "–" : r.correlation.toFixed(2)}</td>
            <td>${num(r.corr_up) ? "–" : r.corr_up.toFixed(2)}</td>
            <td>${num(r.corr_down) ? "–" : r.corr_down.toFixed(2)}</td>
            <td>${num(r.beta) ? "–" : r.beta.toFixed(2)}</td>
            <td>${cap(r.up_capture)}</td>
            <td>${cap(r.down_capture)}</td>
            <td class="${dirClass(q.changePercent)}">${num(q.changePercent) ? "–" : signed(q.changePercent) + "%"}</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>`;
  wrap.querySelectorAll("tbody tr").forEach((tr) => {
    tr.addEventListener("click", () => { location.hash = "#/" + tr.dataset.sym; });
  });
}

function paintWatchBtn(symbol) {
  const on = loadWatch().includes(symbol);
  const btn = $("#d-watch");
  if (!btn) return;
  btn.textContent = on ? "✓ In watchlist" : "+ Add to watchlist";
  btn.classList.toggle("on", on);
}

function themeColors() {
  const cs = getComputedStyle(document.documentElement);
  return {
    text: cs.getPropertyValue("--muted").trim(),
    grid: cs.getPropertyValue("--border").trim(),
    up: cs.getPropertyValue("--up").trim(),
    down: cs.getPropertyValue("--down").trim(),
    accent: cs.getPropertyValue("--accent").trim(),
  };
}

function setupChart() {
  const box = $("#chart");
  if (!box || !window.LightweightCharts) return;
  box.innerHTML = "";
  clearPicks();
  const col = themeColors();
  chart = LightweightCharts.createChart(box, {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: col.text, fontFamily: "inherit" },
    grid: { vertLines: { color: col.grid }, horzLines: { color: col.grid } },
    rightPriceScale: { borderColor: col.grid },
    timeScale: { borderColor: col.grid, timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });

  if (chartState.type === "candle") {
    priceSeries = chart.addCandlestickSeries({
      upColor: col.up, downColor: col.down, borderVisible: false,
      wickUpColor: col.up, wickDownColor: col.down,
    });
  } else {
    priceSeries = chart.addAreaSeries({
      lineColor: col.accent, topColor: col.accent + "44", bottomColor: col.accent + "05",
      lineWidth: 2, priceLineVisible: false,
    });
  }
  volSeries = chart.addHistogramSeries({
    priceFormat: { type: "volume" }, priceScaleId: "vol",
  });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

  chart.subscribeCrosshairMove((param) => {
    const el = $("#chart-legend");
    if (!el) return;
    const bar = param.time && param.point ? param.seriesData.get(priceSeries) : null;
    hoverBar = bar ? { time: param.time, value: bar.value !== undefined ? bar.value : bar.close } : null;
    if (!bar) { el.hidden = true; return; }
    const vol = param.seriesData.get(volSeries);
    el.hidden = false;
    el.innerHTML = legendHTML(param.time, bar, vol);
  });
}

function fmtBarTime(t) {
  if (t && typeof t === "object") {
    return new Date(Date.UTC(t.year, t.month - 1, t.day))
      .toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }
  return new Date(t * 1000)
    .toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function legendHTML(t, bar, vol) {
  const date = `<span class="lg-date">${fmtBarTime(t)}</span>`;
  const v = vol && !num(vol.value) ? `<span class="lg-vol">Vol ${big(vol.value)}</span>` : "";
  if (bar.value !== undefined) {                       // area / line series
    return `${date}<span class="lg-px">${price(bar.value)}</span>${v}`;
  }
  const cls = bar.close >= bar.open ? "up" : "down";   // candlestick series
  const cell = (k, val) => `<span class="lg-ohlc"><i>${k}</i>${price(val)}</span>`;
  return `${date}${cell("O", bar.open)}${cell("H", bar.high)}${cell("L", bar.low)}` +
         `<span class="lg-ohlc ${cls}"><i>C</i>${price(bar.close)}</span>${v}`;
}

function toTime(epochSec, interval) {
  if (/[mh]$/.test(interval)) return epochSec; // intraday -> unix seconds
  const d = new Date(epochSec * 1000);
  return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() };
}

let chartGen = 0;

async function loadChart() {
  const gen = ++chartGen;
  const msg = $("#chart-msg");
  const rangeIdx = chartState.rangeIdx;
  const [rangeLabel, opt] = RANGES[rangeIdx];
  const col = themeColors();
  try {
    const data = await getJSON(`/api/chart?symbol=${encodeURIComponent(chartState.symbol)}&range=${opt.range}&interval=${opt.interval}`);
    if (gen !== chartGen) return;  // a newer range was clicked while this was in flight
    const candles = data.candles || [];
    if (!candles.length) { msg.hidden = false; msg.textContent = "No price history for this range."; return; }
    msg.hidden = true;

    const seen = new Set();
    const pricePts = [];
    const volPts = [];
    for (const c of candles) {
      const t = toTime(c.time, opt.interval);
      const key = typeof t === "object" ? `${t.year}-${t.month}-${t.day}` : t;
      if (seen.has(key)) continue;
      seen.add(key);
      if (chartState.type === "candle") {
        pricePts.push({ time: t, open: c.open, high: c.high, low: c.low, close: c.close });
      } else {
        pricePts.push({ time: t, value: c.close });
      }
      const upBar = c.close >= (c.open ?? c.close);
      volPts.push({ time: t, value: c.volume ?? 0, color: (upBar ? col.up : col.down) + "66" });
    }
    clearPicks();
    priceSeries.setData(pricePts);
    volSeries.setData(volPts);
    chart.timeScale().fitContent();

    // Header quote derived from the range's own series (first vs last close).
    const meta = data.meta || {};
    const last = candles[candles.length - 1].close;
    // 1D shows the day's move (vs prior close); longer ranges show the move across the window.
    const base = rangeIdx === 0
      ? (meta.chartPreviousClose ?? candles[0].close)
      : candles[0].close;
    const chg = last - base;
    const chgPct = base ? (chg / base) * 100 : null;
    $("#d-name").textContent = meta.longName || meta.shortName || chartState.symbol;
    $("#d-meta").textContent = `${chartState.symbol} · ${meta.fullExchangeName || meta.exchangeName || ""} · ${meta.currency || "USD"}`;
    $("#d-price").textContent = price(last);
    const cc = dirClass(chg);
    $("#d-change").className = "detail-change " + cc;
    $("#d-change").textContent = `${arrow(chg)}${signed(chg)} (${pct(chgPct)}) · ${rangeLabel}`;
    const when = meta.regularMarketTime ? new Date(meta.regularMarketTime * 1000).toLocaleString() : "";
    $("#d-sub").textContent = when ? `As of ${when}` : "";

    const srcEl = $("#chart-src");
    const srcText = {
      sqlite: `Local SQLite · ${(data.stored_bars || 0).toLocaleString()} daily bars stored`,
      "sqlite-resampled": `Local SQLite · resampled from ${(data.stored_bars || 0).toLocaleString()} daily bars`,
      yahoo: "Live from Yahoo Finance",
    }[data.source] || "";
    srcEl.textContent = srcText;
    srcEl.className = "src-badge " + (data.source || "").split("-")[0];
    srcEl.hidden = !srcText;
  } catch (e) {
    if (gen !== chartGen) return;
    msg.hidden = false;
    msg.textContent = "Could not load chart: " + e.message;
  }
}

async function loadProfile(symbol) {
  const statsEl = $("#d-stats");
  let p;
  try { p = await getJSON("/api/profile?symbol=" + encodeURIComponent(symbol)); }
  catch (_) { p = {}; }

  const rows = [
    ["Prev Close", price(p.regularMarketPreviousClose)],
    ["Open", price(p.regularMarketOpen)],
    ["Day Range", num(p.regularMarketDayLow) || num(p.regularMarketDayHigh) ? "–" : `${price(p.regularMarketDayLow)} – ${price(p.regularMarketDayHigh)}`],
    ["52-Wk Range", num(p.fiftyTwoWeekLow) || num(p.fiftyTwoWeekHigh) ? "–" : `${price(p.fiftyTwoWeekLow)} – ${price(p.fiftyTwoWeekHigh)}`],
    ["Market Cap", big(p.marketCap)],
    ["Volume", intc(p.regularMarketVolume)],
    ["Avg Volume", intc(p.averageVolume)],
    ["P/E (TTM)", num(p.trailingPE) ? "–" : p.trailingPE.toFixed(2)],
    ["Fwd P/E", num(p.forwardPE) ? "–" : p.forwardPE.toFixed(2)],
    ["EPS (TTM)", num(p.trailingEps) ? "–" : p.trailingEps.toFixed(2)],
    ["Price/Book", num(p.priceToBook) ? "–" : p.priceToBook.toFixed(2)],
    ["Div Yield", divYield(p)],
    ["Beta", num(p.beta) ? "–" : p.beta.toFixed(2)],
    ["Profit Margin", num(p.profitMargins) ? "–" : (p.profitMargins * 100).toFixed(1) + "%"],
    ["Revenue (TTM)", big(p.totalRevenue)],
    ["Shares Out", big(p.sharesOutstanding)],
    ["Sector", p.sector || "–"],
    ["Industry", p.industry || "–"],
    ["Employees", intc(p.fullTimeEmployees)],
  ];
  statsEl.innerHTML = rows.map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

  if (p.longBusinessSummary) {
    const about = $("#d-about");
    about.hidden = false;
    const loc = [p.city, p.state, p.country].filter(Boolean).join(", ");
    about.innerHTML = `
      <h3>About ${p.longName || symbol}</h3>
      <p>${p.longBusinessSummary}</p>
      <div class="links">
        ${p.website ? `<a href="${p.website}" target="_blank" rel="noopener">Website ↗</a>` : ""}
        ${loc ? `<span>${loc}</span>` : ""}
        ${p.quoteType ? `<span>${p.quoteType}</span>` : ""}
      </div>`;
  }
}

/* ------------------------------------------------------------------ search box */
function initSearch() {
  const input = $("#search-input");
  const box = $("#search-results");
  let timer, items = [], active = -1;

  const close = () => { box.hidden = true; active = -1; };
  const open = () => { if (items.length) box.hidden = false; };

  const paint = () => {
    box.innerHTML = items.map((it, i) => `
      <div class="search-row${i === active ? " active" : ""}" data-sym="${it.symbol}">
        <span class="sym">${it.symbol}</span>
        <span class="nm">${it.name || ""}</span>
        <span class="ex">${it.exchange || ""} ${it.type || ""}</span>
      </div>`).join("");
    box.querySelectorAll(".search-row").forEach((r) => {
      r.addEventListener("mousedown", (e) => { e.preventDefault(); go(r.dataset.sym); });
    });
  };
  const go = (sym) => { input.value = ""; close(); location.hash = "#/" + sym; };

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { items = []; close(); return; }
    timer = setTimeout(async () => {
      try {
        items = await getJSON("/api/search?q=" + encodeURIComponent(q));
        if (!Array.isArray(items)) items = [];
        active = -1; paint(); open();
      } catch (_) { close(); }
    }, 180);
  });
  input.addEventListener("keydown", (e) => {
    if (box.hidden) {
      if (e.key === "Enter" && input.value.trim()) go(input.value.trim().toUpperCase());
      return;
    }
    if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, items.length - 1); paint(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); paint(); }
    else if (e.key === "Enter") { e.preventDefault(); go(active >= 0 ? items[active].symbol : input.value.trim().toUpperCase()); }
    else if (e.key === "Escape") close();
  });
  input.addEventListener("blur", () => setTimeout(close, 120));
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== input) { e.preventDefault(); input.focus(); }
  });
}

/* ------------------------------------------------------------------ theme */
function initTheme() {
  const saved = (() => { try { return localStorage.getItem(THEME_KEY); } catch (_) { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
    if (currentRoute()) { setupChart(); loadChart(); }
  });
}

/* ------------------------------------------------------------------ measure (ctrl+click) */
// Ctrl/Cmd+click any two values (chart points, stat tiles, table cells, prices) to see the
// change and % between them. A third pick starts a new measurement; Esc or ✕ clears.
let picks = [];
let pickMarkers = [];

const SUFFIX = { T: 1e12, B: 1e9, M: 1e6, K: 1e3 };
function parseValue(text) {
  const t = text.replace(/[\u2212\u2013]/g, "-").replace(/[▲▼\s,$]/g, "");
  const m = t.match(/^([+-]?\d*\.?\d+)([TBMK%×x])?$/);
  if (!m) return null;
  let v = parseFloat(m[1]);
  const u = m[2] || "";
  if (SUFFIX[u]) v *= SUFFIX[u];
  return { value: v, unit: u === "%" ? "%" : "" };
}

function timeNum(t) {
  return t && typeof t === "object" ? Date.UTC(t.year, t.month - 1, t.day) / 1000 : t;
}

function cellLabel(el) {
  const stat = el.closest(".stat");
  if (stat) return stat.querySelector(".k").textContent;
  if (el.closest(".detail-price")) return `${chartState ? chartState.symbol : ""} price`;
  const card = el.closest(".qcard");
  if (card) return `${card.dataset.sym} price`;
  const td = el.closest("td");
  if (td) {
    const tr = td.closest("tr");
    const table = td.closest("table");
    const col = table.querySelectorAll("thead th")[td.cellIndex];
    const sym = tr.dataset.sym || (tr.querySelector(".sym") || {}).textContent || "";
    const side = el.closest(".mv-side");
    return [sym, col ? col.textContent : side ? side.querySelector(".mv-head").textContent : ""].filter(Boolean).join(" ");
  }
  return "value";
}

function addPick(p) {
  if (picks.length >= 2) clearPicks();
  picks.push(p);
  if (p.el) p.el.classList.add("picked");
  if (p.time !== undefined && priceSeries) {
    pickMarkers.push({ time: p.time, position: "inBar", color: themeColors().accent, shape: "circle", text: picks.length === 1 ? "A" : "B" });
    pickMarkers.sort((a, b) => timeNum(a.time) - timeNum(b.time));
    try { priceSeries.setMarkers(pickMarkers); } catch (_) {}
  }
  paintMeasure();
}

function clearPicks() {
  picks.forEach((p) => p.el && p.el.classList.remove("picked"));
  picks = [];
  if (pickMarkers.length && priceSeries) { try { priceSeries.setMarkers([]); } catch (_) {} }
  pickMarkers = [];
  const panel = $("#measure");
  if (panel) panel.hidden = true;
}

function paintMeasure() {
  let panel = $("#measure");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "measure";
    document.body.appendChild(panel);
    panel.addEventListener("click", (e) => { if (e.target.closest("[data-clear]")) clearPicks(); });
  }
  panel.hidden = false;
  const fmt = (p) => p.unit === "%" ? p.value.toFixed(2) + "%" : Math.abs(p.value) >= 1e3 && !p.chart ? big(p.value) : price(p.value);
  const row = (tag, p) => `<div class="m-row"><b>${tag}</b><span class="m-lbl">${p.label}</span><span class="m-val">${fmt(p)}</span></div>`;
  let result = `<div class="m-hint">Ctrl+click a second value…</div>`;
  if (picks.length === 2) {
    const [a, b] = picks;
    const diff = b.value - a.value;
    const bothPct = a.unit === "%" && b.unit === "%";
    const rel = a.value !== 0 ? (diff / Math.abs(a.value)) * 100 : null;
    const cls = dirClass(diff);
    const days = a.time !== undefined && b.time !== undefined ? Math.round(Math.abs(timeNum(b.time) - timeNum(a.time)) / 86400) : null;
    result = `
      <div class="m-res ${cls}">
        <div class="m-pct">${bothPct ? signed(diff, 2) + " pp" : rel === null ? "n/a" : arrow(diff) + pct(rel)}</div>
        <div class="m-diff">${bothPct ? "" : "Δ " + (Math.abs(diff) >= 1e6 && !a.chart ? (diff < 0 ? "-" : "+") + big(Math.abs(diff)) : signed(diff, 2))}${days ? ` · ${days}d apart` : ""}</div>
      </div>`;
  }
  panel.innerHTML = `<button class="m-x" data-clear title="Clear (Esc)">✕</button>${row("A", picks[0])}${picks[1] ? row("B", picks[1]) : ""}${result}`;
}

document.addEventListener("click", (e) => {
  if (!(e.ctrlKey || e.metaKey) || e.target.closest("#measure")) return;
  const chartBox = e.target.closest("#chart");
  if (chartBox) {
    if (!hoverBar || num(hoverBar.value)) return;
    e.preventDefault();
    addPick({ label: `${chartState.symbol} · ${fmtBarTime(hoverBar.time)}`, value: hoverBar.value, unit: "", time: hoverBar.time, chart: true });
    return;
  }
  const el = e.target.closest(".stat .v, .detail-price, .qcard .price, td");
  if (!el) return;
  // table cells that hold a symbol + name are not values
  const parsed = parseValue(el.matches("td") && el.querySelector(".sym") ? "" : el.textContent.trim());
  if (!parsed) return;
  e.preventDefault();
  e.stopPropagation();
  addPick({ label: cellLabel(el), value: parsed.value, unit: parsed.unit, el });
}, true);

document.addEventListener("keydown", (e) => { if (e.key === "Escape" && picks.length) clearPicks(); });
