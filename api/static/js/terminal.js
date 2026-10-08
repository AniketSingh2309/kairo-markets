// Terminal: full-screen trading chart (1m-1D candles, indicators, drawing tools) with a side panel.
// Charting by TradingView Lightweight Charts (Apache-2.0, bundled in /static/vendor).
import { $, esc, api, store, fmt, money, pctTxt, tone, compact, splitSym, navigate, feed, logoHtml, searchStocks,
  INDICES, onTabSleep, onRoute, app, modal, toast } from "./core.js";
import { getWatchlist } from "./markets.js";
import { openTicket, showTradePanel, hideTradePanel, refreshStatus, tradable, placeBasket, saveBasket } from "./trading.js";

const LWC_URL = "/static/vendor/lightweight-charts.mjs";
const INTERVALS = [["1m", "1m"], ["5m", "5m"], ["15m", "15m"], ["1h", "1h"], ["1D", "1D"]];
const BAR_S = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1D": 86400 };
// Bottom quick ranges: how far back to show, and the interval that suits it.
const SPANS = [["1D", "1d", "1m", 1], ["5D", "5d", "5m", 5], ["1M", "1m", "15m", 31], ["3M", "3m", "1h", 92],
  ["1Y", "1y", "1D", 366], ["5Y", "5y", "1D", 5 * 366], ["All", "all", "1D", null]];
const IND = [
  ["vol", "Volume"], ["sma20", "SMA 20"], ["sma50", "SMA 50"], ["sma200", "SMA 200"], ["ema20", "EMA 20"],
  ["bb", "Bollinger Bands (20, 2)"], ["vwap", "VWAP (intraday)"], ["rsi", "RSI (14)"], ["macd", "MACD (12, 26, 9)"],
];
const IND_COLORS = { sma20: "#e9a23b", sma50: "#4ea1ff", sma200: "#c27bff", ema20: "#2bb3c0", vwap: "#f06292", bb: "#8892a6" };
const TOOLS = [
  ["cross", "Crosshair", '<path d="M12 3v18M3 12h18"/>'],
  ["trend", "Trend line", '<path d="M4 19 20 5"/><circle cx="4" cy="19" r="2"/><circle cx="20" cy="5" r="2"/>'],
  ["hline", "Horizontal line", '<path d="M3 12h18"/><circle cx="12" cy="12" r="2"/>'],
  ["measure", "Measure", '<path d="M4 20 20 4M7 20l-3-3M20 7l-3-3M9 15l2 2M13 11l2 2"/>'],
];

const T = {
  lib: null, chart: null, sym: "", data: null, bars: [], interval: store.get("kairo-term-interval", "5m"),
  type: store.get("kairo-term-type", "candle"), ind: store.get("kairo-term-ind", { vol: true, sma20: false, sma50: false, sma200: false, ema20: false, bb: false, vwap: false, rsi: false, macd: false }),
  scale: store.get("kairo-term-scale", "normal"), tool: "cross", pending: null, hideDraw: false, panel: store.get("kairo-term-panel", "watch"),
  series: {}, es: null, day: null, loading: false, error: null, visible: false, span: null, off: [], dayVol: null, liveTimer: 0,
  optTimer: 0,
};
// Option chain state
const C = { und: store.get("kairo-chain-und", "NIFTY"), expiry: null, data: null, list: null, timer: 0, full: false, centred: "", error: null,
  tab: store.get("kairo-ocf-tab", "chain"), view: store.get("kairo-ocf-view", "prices"), hist: null };
const isOpt = (s) => (s || "").startsWith("OPT:");
const SESSION_ANCHOR = 13500;  // 09:15 IST in seconds after UTC midnight: NSE candles start there

// ------------------------------------------------------------------ indicators (pure)
const sma = (v, n) => { const o = new Array(v.length).fill(null); let s = 0; for (let i = 0; i < v.length; i++) { s += v[i]; if (i >= n) s -= v[i - n]; if (i >= n - 1) o[i] = s / n; } return o; };
const ema = (v, n) => { const o = new Array(v.length).fill(null), k = 2 / (n + 1); let e = null; for (let i = 0; i < v.length; i++) { if (i === n - 1) { e = v.slice(0, n).reduce((a, b) => a + b, 0) / n; } else if (i >= n) e = v[i] * k + e * (1 - k); if (i >= n - 1) o[i] = e; } return o; };
function bollinger(v, n = 20, k = 2) {
  const mid = sma(v, n), up = [], lo = [];
  for (let i = 0; i < v.length; i++) {
    if (mid[i] == null) { up.push(null); lo.push(null); continue; }
    let s = 0; for (let j = i - n + 1; j <= i; j++) s += (v[j] - mid[i]) ** 2;
    const sd = Math.sqrt(s / n); up.push(mid[i] + k * sd); lo.push(mid[i] - k * sd);
  }
  return { mid, up, lo };
}
function rsi(v, n = 14) {
  const o = new Array(v.length).fill(null); let g = 0, l = 0;
  for (let i = 1; i < v.length; i++) {
    const d = v[i] - v[i - 1], up = Math.max(d, 0), dn = Math.max(-d, 0);
    if (i <= n) { g += up; l += dn; if (i === n) { g /= n; l /= n; o[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l); } }
    else { g = (g * (n - 1) + up) / n; l = (l * (n - 1) + dn) / n; o[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l); }
  }
  return o;
}
function macd(v) {
  const f = ema(v, 12), s = ema(v, 26), line = v.map((_, i) => (f[i] != null && s[i] != null ? f[i] - s[i] : null));
  const start = line.findIndex((x) => x != null), sig = new Array(v.length).fill(null);
  if (start >= 0) { const e = ema(line.slice(start), 9); e.forEach((x, i) => (sig[start + i] = x)); }
  return { line, sig, hist: line.map((x, i) => (x != null && sig[i] != null ? x - sig[i] : null)) };
}
const dayKey = (t) => { const d = new Date(t * 1000); return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; };
function vwap(bars) {
  const o = []; let pv = 0, vv = 0, key = "";
  for (const b of bars) {
    const k = dayKey(b.time); if (k !== key) { key = k; pv = 0; vv = 0; }
    const tp = (b.high + b.low + b.close) / 3; pv += tp * (b.vol || 0); vv += b.vol || 0;
    o.push(vv > 0 ? pv / vv : null);
  }
  return o;
}

// ------------------------------------------------------------------ chart
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const dfmt = (opts) => new Intl.DateTimeFormat(undefined, opts);
const F = {
  time: dfmt({ hour: "2-digit", minute: "2-digit" }), day: dfmt({ day: "numeric", month: "short" }),
  month: dfmt({ month: "short", year: "2-digit" }), year: dfmt({ year: "numeric" }),
  full: dfmt({ weekday: "short", day: "numeric", month: "short", year: "numeric" }),
  fullTime: dfmt({ day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }),
};

async function lib() {
  if (!T.lib) T.lib = await import(LWC_URL);
  return T.lib;
}

function buildChart() {
  const L = T.lib, box = $("#termChart");
  const keep = T.chart ? T.chart.timeScale().getVisibleLogicalRange() : null;
  if (T.chart) { T.chart.remove(); T.chart = null; T.series = {}; }
  const grid = css("--grid") || "rgba(128,128,128,.12)", text = css("--muted") || "#8a93a6", border = css("--border") || "#2a2f3a";
  const chart = T.chart = L.createChart(box, {
    autoSize: true,
    layout: { background: { type: L.ColorType.Solid, color: css("--panel") || "#11151c" }, textColor: text,
      fontFamily: "Inter, system-ui, sans-serif", fontSize: 11, attributionLogo: true, panes: { separatorColor: border, enableResize: true } },
    grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    crosshair: { mode: L.CrosshairMode.Normal },
    rightPriceScale: { borderColor: border, mode: { normal: 0, log: 1, pct: 2 }[T.scale] ?? 0 },
    timeScale: { borderColor: border, timeVisible: T.interval !== "1D", secondsVisible: false, rightOffset: 8, barSpacing: 7,
      tickMarkFormatter: (t, type) => {
        const d = new Date(t * 1000);
        if (type === L.TickMarkType.Year) return F.year.format(d);
        if (type === L.TickMarkType.Month) return F.month.format(d);
        if (type === L.TickMarkType.DayOfMonth) return F.day.format(d);
        return F.time.format(d);
      } },
    localization: { timeFormatter: (t) => (T.interval === "1D" ? F.full : F.fullTime).format(new Date(t * 1000)), priceFormatter: (p) => fmt(p) },
  });
  const UP = css("--up") || "#22c55e", DN = css("--down") || "#ef4444";
  T.series.main = T.type === "candle"
    ? chart.addSeries(L.CandlestickSeries, { upColor: UP, downColor: DN, wickUpColor: UP, wickDownColor: DN, borderVisible: false })
    : chart.addSeries(L.AreaSeries, { lineColor: css("--brand") || "#7c5cff", topColor: "rgba(124,92,255,.25)", bottomColor: "rgba(124,92,255,0)", lineWidth: 2 });
  if (T.ind.vol && T.bars.some((b) => b.vol > 0)) {  // indices have no volume
    T.series.vol = chart.addSeries(L.HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  }
  const overlay = (key, opts = {}) => chart.addSeries(L.LineSeries, { color: IND_COLORS[key], lineWidth: 1.5, lastValueVisible: false,
    priceLineVisible: false, crosshairMarkerVisible: false, ...opts });
  for (const k of ["sma20", "sma50", "sma200", "ema20", "vwap"]) if (T.ind[k]) T.series[k] = overlay(k);
  if (T.ind.bb) { T.series.bbUp = overlay("bb", { lineStyle: 2 }); T.series.bbLo = overlay("bb", { lineStyle: 2 }); T.series.bbMid = overlay("bb", { lineWidth: 1 }); }
  let pane = 1;
  if (T.ind.rsi) {
    T.series.rsi = chart.addSeries(L.LineSeries, { color: "#c27bff", lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true }, pane);
    T.series.rsi.createPriceLine({ price: 70, color: DN, lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
    T.series.rsi.createPriceLine({ price: 30, color: UP, lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
    pane++;
  }
  if (T.ind.macd) {
    T.series.macdH = chart.addSeries(L.HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, pane);
    T.series.macd = chart.addSeries(L.LineSeries, { color: "#4ea1ff", lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false }, pane);
    T.series.macdS = chart.addSeries(L.LineSeries, { color: "#e9a23b", lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false }, pane);
  }
  // Indicator panes get one part each, the price pane four (relative sizes survive window resizes).
  const panes = chart.panes();
  if (panes.length > 1) { panes[0].setStretchFactor(4); panes.slice(1).forEach((p) => p.setStretchFactor(1)); }
  chart.subscribeCrosshairMove(onCrosshair);
  chart.subscribeClick(onChartClick);
  applyData(true);
  if (keep) chart.timeScale().setVisibleLogicalRange(keep);
}

function setSeriesData(s, values, bars) {
  if (!s) return;
  s.setData(bars.map((b, i) => (values[i] == null ? { time: b.time } : { time: b.time, value: values[i] })));
}

function applyData(full = false) {
  if (!T.chart || !T.bars.length) return;
  const bars = T.bars, closes = bars.map((b) => b.close), UP = css("--up") || "#22c55e", DN = css("--down") || "#ef4444";
  if (T.type === "candle") T.series.main.setData(bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
  else T.series.main.setData(bars.map((b) => ({ time: b.time, value: b.close })));
  if (T.series.vol) T.series.vol.setData(bars.map((b) => ({ time: b.time, value: b.vol || 0, color: (b.close >= b.open ? UP : DN) + "66" })));
  if (T.series.sma20) setSeriesData(T.series.sma20, sma(closes, 20), bars);
  if (T.series.sma50) setSeriesData(T.series.sma50, sma(closes, 50), bars);
  if (T.series.sma200) setSeriesData(T.series.sma200, sma(closes, 200), bars);
  if (T.series.ema20) setSeriesData(T.series.ema20, ema(closes, 20), bars);
  if (T.series.vwap) setSeriesData(T.series.vwap, T.interval === "1D" ? [] : vwap(bars), bars);
  if (T.series.bbUp) { const b = bollinger(closes); setSeriesData(T.series.bbUp, b.up, bars); setSeriesData(T.series.bbLo, b.lo, bars); setSeriesData(T.series.bbMid, b.mid, bars); }
  if (T.series.rsi) setSeriesData(T.series.rsi, rsi(closes), bars);
  if (T.series.macd) {
    const m = macd(closes);
    setSeriesData(T.series.macd, m.line, bars); setSeriesData(T.series.macdS, m.sig, bars);
    T.series.macdH.setData(bars.map((b, i) => (m.hist[i] == null ? { time: b.time } : { time: b.time, value: m.hist[i], color: m.hist[i] >= 0 ? UP + "aa" : DN + "aa" })));
  }
  if (full) { drawDrawings(); updateLegend(); }
}

// ------------------------------------------------------------------ data + live
async function loadBars() {
  const sym = T.sym, iv = T.interval;
  T.loading = true; T.error = null; renderStatus();
  if (isOpt(sym)) { await loadOption(sym, iv); return; }
  try {
    const d = await api(`/terminal/bars/${encodeURIComponent(sym)}?interval=${iv}`);
    if (sym !== T.sym || iv !== T.interval) return;
    T.data = d;
    T.bars = d.t.map((t, i) => ({ time: t, open: d.o[i], high: d.h[i], low: d.l[i], close: d.c[i], vol: d.v[i] }));
    if (d.name) app.names.set(sym, d.name);
  } catch (err) {
    if (sym !== T.sym || iv !== T.interval) return;
    T.error = err.message; T.bars = [];
  }
  T.loading = false;
  await lib();
  buildChart();
  if (T.span) applySpan(T.span); else T.chart?.timeScale().setVisibleLogicalRange({ from: T.bars.length - 150, to: T.bars.length + 8 });
  renderStatus(); renderHeader(); renderDetails();
}

/** Ticks -> candles, buckets aligned to the 09:15 IST session start (so 1h candles read 9:15, 10:15, ...). */
function bucket(times, prices, size) {
  const out = [];
  for (let i = 0; i < times.length; i++) {
    const t = times[i], p = prices[i], start = t - ((((t - SESSION_ANCHOR) % size) + size) % size);
    const last = out[out.length - 1];
    if (last && last.time === start) { last.high = Math.max(last.high, p); last.low = Math.min(last.low, p); last.close = p; }
    else out.push({ time: start, open: p, high: p, low: p, close: p, vol: 0 });
  }
  return out;
}
async function fetchOption(sym, iv) {
  const d = await api(`/options/contract?id=${encodeURIComponent(sym.slice(4))}`);
  T.opt = d;
  T.data = { name: d.label, currency: "INR", previous_close: d.prev_close, interval: iv };
  T.bars = bucket(d.t, d.p, BAR_S[iv]);
}
async function loadOption(sym, iv) {
  try { await fetchOption(sym, iv); }
  catch (err) { if (sym !== T.sym) return; T.error = err.message; T.bars = []; }
  if (sym !== T.sym || iv !== T.interval) return;
  T.loading = false;
  await lib();
  buildChart();
  T.chart?.timeScale().fitContent();
  renderStatus(); renderHeader(); renderDetails();
}

/** Fold a trade into the current bar, or open the next bar when its time window has started. */
function onTrade(price, tSec, dayVolume) {
  if (!T.bars.length || !price) return;
  const size = BAR_S[T.interval], last = T.bars[T.bars.length - 1];
  let vol = 0;
  if (dayVolume != null) { if (T.dayVol != null && dayVolume >= T.dayVol) vol = dayVolume - T.dayVol; T.dayVol = dayVolume; }
  if (tSec < last.time) return;
  if (tSec >= last.time + size && (T.interval !== "1D" || dayKey(tSec) !== dayKey(last.time))) {
    const start = last.time + Math.floor((tSec - last.time) / size) * size;
    T.bars.push({ time: start, open: price, high: price, low: price, close: price, vol });
  } else {
    last.close = price; last.high = Math.max(last.high, price); last.low = Math.min(last.low, price); last.vol = (last.vol || 0) + vol;
  }
  const b = T.bars[T.bars.length - 1];
  if (T.series.main) T.series.main.update(T.type === "candle" ? { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close } : { time: b.time, value: b.close });
  if (T.series.vol) T.series.vol.update({ time: b.time, value: b.vol || 0, color: ((b.close >= b.open ? css("--up") : css("--down")) || "#888") + "66" });
  clearTimeout(T.liveTimer); T.liveTimer = setTimeout(() => applyData(false), 1500);  // indicators: at most every 1.5 s
  updateLegend(); renderHeader();
}

function startStream() {
  stopStream();
  if (!T.visible || !T.sym) return;
  if (isOpt(T.sym)) {  // option contracts: NSE's trade series, refreshed while the market is open
    const sym = T.sym;
    T.optTimer = setInterval(async () => {
      if (T.sym !== sym || !T.opt?.market_open || !T.chart) return;
      try { await fetchOption(sym, T.interval); applyData(false); updateLegend(); renderHeader(); renderDetails(); } catch { /* keep the last chart */ }
    }, 5000);
    return;
  }
  const sym = T.sym, es = T.es = new EventSource(`/live/${encodeURIComponent(sym)}/stream`);
  T.dayVol = null;
  es.addEventListener("snapshot", (e) => {
    if (T.sym !== sym) return;
    const d = JSON.parse(e.data);
    T.day = d; T.dayVol = d.volume ?? null;
    renderHeader(); renderDetails();
  });
  es.addEventListener("tick", (e) => {
    if (T.sym !== sym) return;
    const d = JSON.parse(e.data);
    if (T.day) Object.assign(T.day, { price: d.price, change: d.change ?? T.day.change, change_pct: d.change_percent ?? T.day.change_pct,
      day_high: d.day_high ?? T.day.day_high, day_low: d.day_low ?? T.day.day_low, market_state: d.session === "regular" ? "open" : T.day.market_state });
    onTrade(d.price, Math.floor(Date.parse(d.time) / 1000), d.day_volume ?? null);
    renderDetails();
  });
  es.addEventListener("status", (e) => { T.stream = JSON.parse(e.data).stream; renderStatus(); });
}
function stopStream() { if (T.es) { T.es.close(); T.es = null; } clearInterval(T.optTimer); }

// ------------------------------------------------------------------ drawings (per symbol + interval)
const drawKey = () => `kairo-draw:${T.sym}|${T.interval}`;
const drawings = () => store.get(drawKey(), []);
const saveDrawings = (list) => store.set(drawKey(), list);
function drawDrawings() {
  if (!T.chart || !T.series.main) return;
  (T.series.drawn || []).forEach((s) => { try { T.chart.removeSeries(s); } catch { /* gone with a rebuild */ } });
  (T.series.lines || []).forEach((l) => { try { T.series.main.removePriceLine(l); } catch { /* ignore */ } });
  T.series.drawn = []; T.series.lines = [];
  if (T.hideDraw) return;
  const L = T.lib, col = css("--brand") || "#7c5cff";
  for (const d of drawings()) {
    if (d.kind === "hline") T.series.lines.push(T.series.main.createPriceLine({ price: d.p, color: col, lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: "" }));
    if (d.kind === "trend" && d.t1 !== d.t2) {
      const s = T.chart.addSeries(L.LineSeries, { color: col, lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
      s.setData([{ time: d.t1, value: d.p1 }, { time: d.t2, value: d.p2 }].sort((a, b) => a.time - b.time));
      T.series.drawn.push(s);
    }
  }
}
function onChartClick(param) {
  if (!param.point || T.tool === "cross" || !T.series.main) return;
  const price = T.series.main.coordinateToPrice(param.point.y), time = param.time ?? T.chart.timeScale().coordinateToTime(param.point.x);
  if (price == null || time == null) return;
  if (T.tool === "hline") { saveDrawings([...drawings(), { kind: "hline", p: price }]); drawDrawings(); setTool("cross"); return; }
  if (!T.pending) { T.pending = { t: time, p: price, x: param.point.x, y: param.point.y, logical: param.logical }; renderHint(); return; }
  const a = T.pending; T.pending = null;
  if (T.tool === "trend") { saveDrawings([...drawings(), { kind: "trend", t1: a.t, p1: a.p, t2: time, p2: price }]); drawDrawings(); }
  if (T.tool === "measure") showMeasure(a, { t: time, p: price, x: param.point.x, y: param.point.y, logical: param.logical });
  setTool("cross");
}
function showMeasure(a, b) {
  const box = $("#termMeasure"), chg = b.p - a.p, pct = (chg / a.p) * 100, bars = Math.abs(Math.round((b.logical ?? 0) - (a.logical ?? 0)));
  const left = Math.min(a.x, b.x), top = Math.min(a.y, b.y);
  box.hidden = false;
  box.className = `term-measure ${chg >= 0 ? "up" : "down"}`;
  Object.assign(box.style, { left: `${left}px`, top: `${top}px`, width: `${Math.max(2, Math.abs(b.x - a.x))}px`, height: `${Math.max(2, Math.abs(b.y - a.y))}px` });
  box.innerHTML = `<span>${chg >= 0 ? "+" : "−"}${fmt(Math.abs(chg))} (${pctTxt(pct)}) · ${bars} bars</span>`;
  const hide = () => { box.hidden = true; T.chart?.timeScale().unsubscribeVisibleLogicalRangeChange(hide); };
  T.chart.timeScale().subscribeVisibleLogicalRangeChange(hide);
  setTimeout(() => document.addEventListener("pointerdown", function once(e) { if (!box.contains(e.target)) { hide(); document.removeEventListener("pointerdown", once); } }), 0);
}
function setTool(tool) {
  T.tool = tool; T.pending = null;
  document.querySelectorAll("#termTools [data-tool]").forEach((b) => b.classList.toggle("on", b.dataset.tool === tool));
  $("#termChart").classList.toggle("drawing", tool !== "cross");
  renderHint();
}
function renderHint() {
  const h = $("#termHint");
  const msg = { trend: T.pending ? "Click the second point" : "Click the first point of the line", measure: T.pending ? "Click the end point" : "Click the start point",
    hline: "Click a price level" }[T.tool];
  h.hidden = !msg; h.textContent = msg ? `${msg} · Esc to cancel` : "";
}

// ------------------------------------------------------------------ header, legend, status, panel
function updateLegend(param) {
  const box = $("#termLegend"); if (!box) return;
  const bar = param?.time != null ? T.bars.find((b) => b.time === param.time) : T.bars[T.bars.length - 1];
  const name = T.data?.name || app.names.get(T.sym) || T.sym;
  const ivLabel = T.interval === "1D" ? "1D" : T.interval;
  if (!bar) { box.innerHTML = `<b>${esc(splitSym(T.sym)[0])}</b> · ${ivLabel}`; return; }
  const i = T.bars.indexOf(bar), prev = i > 0 ? T.bars[i - 1].close : bar.open, chg = bar.close - prev;
  box.innerHTML = `<b>${esc(name)}</b><span class="muted"> · ${ivLabel}${splitSym(T.sym)[1] ? " · " + splitSym(T.sym)[1] : ""}</span>
    <span>O<i class="${tone(bar.close - bar.open)}">${fmt(bar.open)}</i></span><span>H<i class="${tone(bar.close - bar.open)}">${fmt(bar.high)}</i></span>
    <span>L<i class="${tone(bar.close - bar.open)}">${fmt(bar.low)}</i></span><span>C<i class="${tone(bar.close - bar.open)}">${fmt(bar.close)}</i></span>
    <span class="${tone(chg)}">${chg >= 0 ? "+" : "−"}${fmt(Math.abs(chg))} (${pctTxt((chg / prev) * 100)})</span>
    ${bar.vol ? `<span class="muted">Vol ${compact(bar.vol)}</span>` : ""}`;
}
const onCrosshair = (param) => updateLegend(param.time != null ? param : null);
/** Display name: indices by their name (NIFTY 50), everything else by ticker (RELIANCE). */
const label = (sym) => (isOpt(sym) ? T.data?.name || sym.slice(4) : INDICES.find(([s]) => s === sym)?.[1] || splitSym(sym)[0]);

function renderHeader() {
  const d = (isOpt(T.sym) ? null : T.day) || {}, last = T.bars[T.bars.length - 1];
  const price = d.price ?? last?.close, cur = d.currency || T.data?.currency;
  const prevClose = d.previous_close ?? T.data?.previous_close;
  const chg = d.change ?? (price != null && prevClose ? price - prevClose : null), pct = d.change_pct ?? (chg != null && prevClose ? (chg / prevClose) * 100 : null);
  $("#termPx").innerHTML = `${logoHtml(isOpt(T.sym) ? "^OPT" : T.sym, T.data?.name, 24)}<b>${esc(label(T.sym))}</b>
    <span class="num">${price != null ? money(price, cur) : "—"}</span><span class="num ${tone(chg)}">${chg != null ? `${chg >= 0 ? "+" : "−"}${fmt(Math.abs(chg))} (${pctTxt(pct)})` : ""}</span>`;
  document.title = `${label(T.sym)} ${price != null ? fmt(price) : ""} · Terminal · Kairo`;
}
function renderStatus() {
  const box = $("#termStatus");
  if (T.loading && !T.bars.length) { box.hidden = false; box.innerHTML = `<div class="skeleton" style="width:100%;height:100%"></div>`; return; }
  if (T.error && !T.bars.length) {
    box.hidden = false;
    box.innerHTML = `<div class="empty">${/404|Unknown/i.test(T.error) ? "No chart data for this symbol." : esc(T.error)}
      ${T.interval !== "1D" ? `<button class="btn sm" id="termTryDaily">Show daily candles</button>` : ""}</div>`;
    $("#termTryDaily")?.addEventListener("click", () => setInterval_("1D"));
    return;
  }
  box.hidden = true;
  const live = $("#termLive");
  const state = isOpt(T.sym) ? (T.opt ? (T.opt.market_open ? "delayed" : "closed") : "")
    : T.day?.market_state === "open" ? (T.stream === "connected" ? "live" : "delayed") : T.day ? "closed" : "";
  live.className = `chip ${state === "live" ? "live" : state === "delayed" ? "delayed" : ""}`;
  live.innerHTML = state === "live" ? "<i></i>Live" : state === "delayed" ? "Delayed" : state === "closed" ? "Market closed" : "";
  live.hidden = !state;
}
function renderDetails() {
  if (T.panel !== "details") return;
  if (isOpt(T.sym)) { renderOptionDetails(); return; }
  const d = T.day, box = $("#termPanelBody");
  if (!d) { box.innerHTML = `<div class="skeleton" style="height:180px"></div>`; return; }
  const cur = d.currency, row = (k, v) => `<div class="tp-row"><span class="muted">${k}</span><b class="num">${v}</b></div>`;
  const lo = d.day_low, hi = d.day_high, pos = lo != null && hi != null && hi > lo ? ((d.price - lo) / (hi - lo)) * 100 : null;
  const lo52 = d.fifty_two_week_low, hi52 = d.fifty_two_week_high, pos52 = lo52 != null && hi52 > lo52 ? ((d.price - lo52) / (hi52 - lo52)) * 100 : null;
  const bar = (p, a, b) => `<div class="range-bar"><i style="left:${Math.max(0, Math.min(100, p))}%"></i></div><div class="range-ends num"><span>${money(a, cur)}</span><span>${money(b, cur)}</span></div>`;
  box.innerHTML = `<div class="tp-title">${esc(d.name || T.sym)}</div>
    ${row("Open", money(d.open, cur))}${row("Prev. close", money(d.previous_close, cur))}${row("Day high", money(hi, cur))}${row("Day low", money(lo, cur))}
    ${row("Volume", compact(d.volume))}
    ${pos != null ? `<div class="tp-sub">Today's range</div>${bar(pos, lo, hi)}` : ""}
    ${pos52 != null ? `<div class="tp-sub">52-week range</div>${bar(pos52, lo52, hi52)}` : ""}
    <a class="btn sm" style="margin-top:14px" href="#/markets?symbol=${encodeURIComponent(T.sym)}">Open stock page</a>`;
}
function renderOptionDetails() {
  const box = $("#termPanelBody"), o = T.opt;
  if (!o) { box.innerHTML = `<div class="skeleton" style="height:160px"></div>`; return; }
  const last = o.p[o.p.length - 1], hi = Math.max(...o.p), lo = Math.min(...o.p), prev = o.prev_close;
  const row = (k, v) => `<div class="tp-row"><span class="muted">${k}</span><b class="num">${v}</b></div>`;
  const chg = prev ? last - prev : null;
  box.innerHTML = `<div class="tp-title">${esc(o.label || o.id)}</div>
    ${row("Last price", money(last, "INR"))}${row("Prev. close", money(prev, "INR"))}
    ${row("Change", chg == null ? "—" : `<span class="${tone(chg)}">${chg >= 0 ? "+" : "−"}${fmt(Math.abs(chg))} (${pctTxt((chg / prev) * 100)})</span>`)}
    ${row("Day high", money(hi, "INR"))}${row("Day low", money(lo, "INR"))}${row("Trades today", o.p.length.toLocaleString())}
    <div class="fine" style="margin-top:10px">Prices from NSE, refreshed every 5 seconds while the market is open.</div>
    <a class="btn sm" style="margin-top:12px" href="#/terminal?symbol=${encodeURIComponent(C.data?.yahoo || "^NSEI")}">Chart the underlying</a>`;
}

function renderWatch() {
  if (T.panel !== "watch") return;
  const list = getWatchlist(), box = $("#termPanelBody");
  if (!list.length) { box.innerHTML = `<div class="muted" style="padding:10px 2px">Your watchlist is empty. Add stocks from their stock page.</div>`; return; }
  box.innerHTML = list.map((s) => {
    const q = feed.last(s) || {};
    return `<button class="tw-row ${s === T.sym ? "on" : ""}" data-sym="${esc(s)}">${logoHtml(s, q.name, 26)}
      <span class="tw-id"><b>${esc(label(s))}</b><small class="muted">${esc(q.name || "")}</small></span>
      <span class="tw-px num">${q.price != null ? fmt(q.price) : "—"}<small class="${tone(q.change_pct)}">${q.change_pct != null ? pctTxt(q.change_pct) : ""}</small></span></button>`;
  }).join("");
}
function renderPanel() {
  document.querySelectorAll("#termTabs button").forEach((b) => b.classList.toggle("on", b.dataset.p === T.panel));
  if (T.panel !== "trade") hideTradePanel();
  if (T.panel === "watch") renderWatch(); else if (T.panel === "chain") { renderChain(); loadChain(); }
  else if (T.panel === "trade") showTradePanel($("#termPanelBody"), openSymbol); else renderDetails();
  scheduleChain();
}

// ------------------------------------------------------------------ option chain
const chainVisible = () => T.visible && (T.panel === "chain" || C.full);
async function loadChain(force = false) {
  if (!chainVisible()) return;
  const und = C.und, exp = C.expiry;
  if (!C.list) { try { C.list = await api("/options/underlyings"); } catch { C.list = { indices: [], stocks: [] }; } }
  try {
    const d = await api(`/options/chain?symbol=${encodeURIComponent(und)}${exp ? `&expiry=${encodeURIComponent(exp)}` : ""}`);
    if (und !== C.und || (exp && exp !== C.expiry)) return;
    C.data = d; C.expiry = d.expiry; C.error = null;
  } catch (err) { if (und === C.und) { C.error = err.message; if (!force) C.data = C.data?.symbol === und ? C.data : null; } }
  renderChain(); renderChainFull(); scheduleChain();
  if (C.full && C.tab === "oi" && Date.now() - (C.histAt || 0) > 60000) { C.histAt = Date.now(); loadOiHistory(); }
}
function scheduleChain() {
  clearTimeout(C.timer);
  if (!chainVisible()) return;
  C.timer = setTimeout(() => loadChain(), C.data?.market_open === false ? 60000 : 5000);
}
function setUnderlying(sym) {
  if (!sym || sym === C.und) return;
  C.und = sym; C.expiry = null; C.data = null; C.centred = ""; store.set("kairo-chain-und", sym);
  renderChain(); renderChainFull(); loadChain();
}
function chainPickers() {
  const opt = (x) => `<option value="${esc(x.symbol)}" ${x.symbol === C.und ? "selected" : ""}>${esc(x.symbol)}</option>`;
  const und = C.list ? `<optgroup label="Indices">${C.list.indices.map(opt).join("")}</optgroup><optgroup label="Stocks">${C.list.stocks.map(opt).join("")}</optgroup>`
    : `<option>${esc(C.und)}</option>`;
  const exps = (C.data?.expiries || []).map((e) => `<option value="${esc(e)}" ${e === C.expiry ? "selected" : ""}>${esc(e.slice(0, 6).replace("-", " "))}${e.slice(-4) !== String(new Date().getFullYear()) ? " " + e.slice(-2) : ""}</option>`).join("");
  return `<select class="oc-sel" data-pick="und" aria-label="Underlying">${und}</select><select class="oc-sel" data-pick="exp" aria-label="Expiry">${exps}</select>`;
}
const ltpCell = (side, cls = "") => side && side.ltp
  ? `<button class="oc-ltp ${cls}" data-id="${esc(side.id)}" title="Chart this option">₹${fmt(side.ltp)}<small class="${tone(side.pchg)}">${side.pchg != null ? pctTxt(side.pchg) : ""}</small></button>`
  : `<span class="oc-ltp muted ${cls}">—</span>`;
function spotRow(d, cols) {
  const chg = T.sym === d.yahoo && T.day ? T.day.change_pct : null;
  return `<div class="oc-spot" style="grid-column:1/-1" data-spot><span>${fmt(d.spot)}${chg != null ? ` <i class="${tone(chg)}">${pctTxt(chg)}</i>` : ""}</span></div>`.replace("grid-column:1/-1", `grid-column:1/${cols + 1}`);
}
function renderChain() {
  if (T.panel !== "chain") return;
  const box = $("#termPanelBody"), d = C.data;
  const head = `<div class="oc-head">${chainPickers()}<button class="term-btn" id="ocExpand" title="Full option chain" aria-label="Full option chain">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg></button></div>`;
  if (!d) {
    box.innerHTML = head + (C.error ? `<div class="muted" style="padding:12px 2px">${esc(C.error)}</div>` : `<div class="skeleton" style="height:320px;margin-top:8px"></div>`);
    return;
  }
  const maxOi = Math.max(1, ...d.rows.flatMap((r) => [r.ce?.oi || 0, r.pe?.oi || 0]));
  let html = "", spotDone = false;
  for (const r of d.rows) {
    if (!spotDone && d.spot != null && r.strike > d.spot) { html += spotRow(d, 3); spotDone = true; }
    const ceItm = d.spot != null && r.strike < d.spot, peItm = d.spot != null && r.strike > d.spot;
    html += `<div class="oc-row ${r.strike === d.atm ? "atm" : ""}">
      <div class="oc-c ${ceItm ? "itm" : ""}">${ltpCell(r.ce)}</div>
      <div class="oc-k"><b>${fmt(r.strike, r.strike % 1 ? 2 : 0)}</b><span class="oc-oi"><i class="ce" style="width:${((r.ce?.oi || 0) / maxOi) * 50}%"></i><i class="pe" style="width:${((r.pe?.oi || 0) / maxOi) * 50}%"></i></span></div>
      <div class="oc-p ${peItm ? "itm" : ""}">${ltpCell(r.pe, "r")}</div></div>`;
  }
  if (!spotDone && d.spot != null) html += spotRow(d, 3);
  const scroller = box, keep = scroller.scrollTop;
  box.innerHTML = head + `<div class="oc-cols"><span>Call LTP</span><span>Strike</span><span class="r">Put LTP</span></div><div class="oc-list">${html}</div>
    <div class="oc-foot muted">PCR ${d.totals.pcr ?? "—"} · Max pain ${d.totals.max_pain != null ? fmt(d.totals.max_pain, 0) : "—"}${d.vix ? ` · VIX ${fmt(d.vix.value)}` : ""} · ${d.market_open ? "updates every 5 s" : "market closed"}</div>`;
  const key = `${d.symbol}|${d.expiry}`;
  if (C.centred !== key) { C.centred = key; box.querySelector("[data-spot]")?.scrollIntoView({ block: "center" }); }
  else scroller.scrollTop = keep;
}
// ------------------------------------------------------------------ full option chain: chain / OI analysis / straddle
const OCF_TABS = [["chain", "Chain"], ["oi", "OI analysis"], ["straddle", "Straddle"], ["strategy", "Strategy builder"]];
const vixTxt = (v) => (v ? `<span>India VIX <b>${fmt(v.value)}</b> <i class="${tone(v.change_pct)}">${v.change_pct != null ? pctTxt(v.change_pct) : ""}</i></span>` : "");
const nz = (v, dp = 0) => (v == null || v === 0 ? "—" : fmt(v, dp));  // NSE sends 0 when it has no value (IV deep in the money)

function renderChainFull() {
  const box = $("#termChainFull");
  box.hidden = !C.full;
  if (!C.full) { destroyStraddle(); return; }
  const d = C.data;
  const top = `<div class="ocf-top"><div class="oc-head">${chainPickers()}</div>
    <div class="seg ocf-tabs">${OCF_TABS.map(([k, l]) => `<button data-ocf="${k}" class="${k === C.tab ? "on" : ""}">${l}</button>`).join("")}</div>
    ${C.tab === "chain" ? `<div class="seg"><button data-view="prices" class="${C.view === "prices" ? "on" : ""}">Prices</button><button data-view="greeks" class="${C.view === "greeks" ? "on" : ""}">Greeks</button></div>` : ""}
    <div class="term-sp"></div>
    <button class="term-btn" id="ocClose" title="Close" aria-label="Close full option chain">✕</button></div>
    ${d ? `<div class="ocf-stats"><span>Spot <b>${fmt(d.spot)}</b></span><span>PCR <b>${d.totals.pcr ?? "—"}</b></span>
      <span>Max pain <b>${d.totals.max_pain != null ? fmt(d.totals.max_pain, 0) : "—"}</b></span>
      <span>Call OI <b>${compact(d.totals.ce_oi)}</b></span><span>Put OI <b>${compact(d.totals.pe_oi)}</b></span>${vixTxt(d.vix)}
      <span>Expiry in <b>${d.days_to_expiry != null ? `${fmt(d.days_to_expiry, 1)} days` : "—"}</b></span>
      <span class="muted">${d.observed_at ? `as of ${new Date(d.observed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}</span></div>` : ""}`;
  if (C.tab !== "straddle") destroyStraddle();
  if (!d) { box.innerHTML = top + `<div class="skeleton" style="height:60%;margin:12px"></div>`; return; }
  if (C.tab === "oi") { renderOi(box, top, d); return; }
  if (C.tab === "straddle") { renderStraddle(box, top, d); return; }
  if (C.tab === "strategy") { renderStrategy(box, top, d); return; }
  const g = C.view === "greeks";
  const oiCell = (v, max, cls) => `<td class="num oi-cell"><i class="${cls}" style="width:${((v || 0) / max) * 100}%"></i><span>${v == null ? "—" : compact(v)}</span></td>`;
  const maxOi = Math.max(1, ...d.rows.flatMap((r) => [r.ce?.oi || 0, r.pe?.oi || 0]));
  const gk = (side, key, dp) => { const v = side?.greeks?.[key]; return v == null ? "—" : fmt(v, dp); };
  const callCols = (c, itm) => g
    ? `<td class="num ${itm}">${gk(c, "delta", 2)}</td><td class="num ${itm}">${gk(c, "gamma", 4)}</td><td class="num ${itm}">${gk(c, "theta", 2)}</td><td class="num ${itm}">${gk(c, "vega", 2)}</td><td class="num ${itm}">${c?.greeks ? fmt(c.greeks.iv, 2) : "—"}</td>`
    : `${oiCell(c?.oi, maxOi, "ce")}<td class="num ${itm} ${tone(c?.coi)}">${c?.coi == null ? "—" : compact(c.coi)}</td><td class="num ${itm}">${c?.vol == null ? "—" : compact(c.vol)}</td><td class="num ${itm}">${nz(c?.iv, 2)}</td>`;
  const putCols = (p, itm) => g
    ? `<td class="num ${itm}">${p?.greeks ? fmt(p.greeks.iv, 2) : "—"}</td><td class="num ${itm}">${gk(p, "vega", 2)}</td><td class="num ${itm}">${gk(p, "theta", 2)}</td><td class="num ${itm}">${gk(p, "gamma", 4)}</td><td class="num ${itm}">${gk(p, "delta", 2)}</td>`
    : `<td class="num ${itm}">${nz(p?.iv, 2)}</td><td class="num ${itm}">${p?.vol == null ? "—" : compact(p.vol)}</td><td class="num ${itm} ${tone(p?.coi)}">${p?.coi == null ? "—" : compact(p.coi)}</td>${oiCell(p?.oi, maxOi, "pe")}`;
  const n = g ? 6 : 5, cols = n * 2 + 1;
  let rows = "", spotDone = false;
  for (const r of d.rows) {
    if (!spotDone && d.spot != null && r.strike > d.spot) { rows += `<tr class="ocf-spot" data-spot><td colspan="${cols}"><span>${fmt(d.spot)}</span></td></tr>`; spotDone = true; }
    const ceItm = d.spot != null && r.strike < d.spot ? "itm" : "", peItm = d.spot != null && r.strike > d.spot ? "itm" : "";
    rows += `<tr class="${r.strike === d.atm ? "atm" : ""}">${callCols(r.ce, ceItm)}<td class="${ceItm}">${ltpCell(r.ce)}</td>
      <td class="ocf-k">${fmt(r.strike, r.strike % 1 ? 2 : 0)}</td><td class="${peItm}">${ltpCell(r.pe, "r")}</td>${putCols(r.pe, peItm)}</tr>`;
  }
  const heads = g ? ["Delta", "Gamma", "Theta", "Vega", "IV"] : ["OI", "Chg OI", "Volume", "IV"];
  const wrap = box.querySelector(".ocf-table"), keep = wrap && box.dataset.view === C.view ? wrap.scrollTop : null;
  box.dataset.view = C.view;
  box.innerHTML = top + `<div class="ocf-table"><table><thead><tr><th colspan="${n}" class="ocf-side">CALLS</th><th></th><th colspan="${n}" class="ocf-side">PUTS</th></tr>
    <tr>${heads.map((h) => `<th>${h}</th>`).join("")}<th>LTP</th><th>Strike</th><th>LTP</th>${heads.slice().reverse().map((h) => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows}</tbody></table></div>
    ${g ? `<div class="fine ocf-note">Black-Scholes with NSE's implied volatility (or the IV implied by the last price where NSE shows none), ${fmt((d.risk_free_rate || 0) * 100, 1)}% risk-free rate, to the 3:30 pm expiry. Theta is per day; vega per 1 point of IV.</div>` : ""}`;
  const t = box.querySelector(".ocf-table");
  if (keep == null) box.querySelector("[data-spot]")?.scrollIntoView({ block: "center" }); else t.scrollTop = keep;
}

// --- OI analysis
function nearAtm(d, each) {
  const i = Math.max(0, d.rows.findIndex((r) => r.strike === d.atm));
  return d.rows.slice(Math.max(0, i - each), i + each + 1);
}
function strikeBars(rows, d, signed) {
  const val = (r, k) => (signed ? r[k]?.coi : r[k]?.oi) || 0;
  const max = Math.max(1, ...rows.flatMap((r) => [Math.abs(val(r, "ce")), Math.abs(val(r, "pe"))]));
  const bar = (v, cls) => {
    const h = (Math.abs(v) / max) * (signed ? 50 : 100);
    return signed ? `<i class="${cls} ${v < 0 ? "neg" : ""}" style="height:${h}%;${v < 0 ? "top:50%" : "bottom:50%"}" title="${compact(v)}"></i>`
      : `<i class="${cls}" style="height:${h}%;bottom:0" title="${compact(v)}"></i>`;
  };
  return `<div class="oib ${signed ? "signed" : ""}">${rows.map((r, i) => `<div class="oib-col ${r.strike === d.atm ? "atm" : ""} ${r.strike === d.totals.max_pain ? "mp" : ""}">
      <div class="oib-bars">${bar(val(r, "ce"), "ce")}${bar(val(r, "pe"), "pe")}</div>
      <div class="oib-k">${i % 2 === 0 || r.strike === d.atm ? fmt(r.strike, 0) : ""}</div></div>`).join("")}</div>`;
}
function pcrChart(points) {
  if (!points || points.length < 2) return `<div class="muted oi-empty">PCR is recorded every 5 minutes during market hours while Kairo is running. ${points?.length ? "One point so far today." : "No points yet today."}</div>`;
  const W = 600, H = 150, pad = 26, vals = points.map((p) => p.pcr).filter((v) => v != null);
  const lo = Math.min(...vals), hi = Math.max(...vals), rng = hi - lo || 0.1;
  const t0 = Date.parse(points[0].at), t1 = Date.parse(points[points.length - 1].at) || t0 + 1;
  const x = (p) => pad + ((Date.parse(p.at) - t0) / Math.max(1, t1 - t0)) * (W - pad * 2), y = (v) => H - pad - ((v - lo) / rng) * (H - pad * 2);
  const path = points.filter((p) => p.pcr != null).map((p, i) => `${i ? "L" : "M"}${x(p).toFixed(1)},${y(p.pcr).toFixed(1)}`).join("");
  const tm = (p) => new Date(p.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `<svg viewBox="0 0 ${W} ${H}" class="pcr-svg" preserveAspectRatio="none"><path d="${path}" fill="none" stroke="var(--brand)" stroke-width="2"/>
    <text x="${pad}" y="${H - 6}" class="ax">${tm(points[0])}</text><text x="${W - pad}" y="${H - 6}" class="ax" text-anchor="end">${tm(points[points.length - 1])}</text>
    <text x="4" y="${y(hi) + 4}" class="ax">${hi.toFixed(2)}</text><text x="4" y="${y(lo) + 4}" class="ax">${lo.toFixed(2)}</text></svg>`;
}
async function loadOiHistory() {
  if (!C.data || C.tab !== "oi" || !C.full) return;
  try { C.hist = await api(`/options/oi-history?symbol=${encodeURIComponent(C.data.symbol)}&expiry=${encodeURIComponent(C.data.expiry)}`); } catch { C.hist = { points: [] }; }
  if (C.tab === "oi" && C.full) renderChainFull();
}
function renderOi(box, top, d) {
  const near = nearAtm(d, 12), table = nearAtm(d, 8);
  const ceCoi = d.rows.reduce((a, r) => a + (r.ce?.coi || 0), 0), peCoi = d.rows.reduce((a, r) => a + (r.pe?.coi || 0), 0);
  const label = (b) => (b ? `<span class="bu ${b.tone}">${b.label}</span>` : `<span class="muted">—</span>`);
  const hist = C.hist?.expiry === d.expiry && C.hist?.symbol === d.symbol ? C.hist.points : null;
  box.innerHTML = top + `<div class="oi-wrap">
    <div class="oi-cards"><div><span class="muted">Calls OI change today</span><b class="num ${tone(ceCoi)}">${ceCoi >= 0 ? "+" : "−"}${compact(Math.abs(ceCoi))}</b></div>
      <div><span class="muted">Puts OI change today</span><b class="num ${tone(peCoi)}">${peCoi >= 0 ? "+" : "−"}${compact(Math.abs(peCoi))}</b></div>
      <div><span class="muted">PCR</span><b class="num">${d.totals.pcr ?? "—"}</b><small class="muted">${d.totals.pcr == null ? "" : d.totals.pcr > 1.2 ? "Put-heavy (often read as support)" : d.totals.pcr < 0.7 ? "Call-heavy (often read as resistance)" : "Balanced"}</small></div>
      <div><span class="muted">Max pain</span><b class="num">${d.totals.max_pain != null ? fmt(d.totals.max_pain, 0) : "—"}</b><small class="muted">Spot ${fmt(d.spot)}</small></div></div>
    <div class="oi-grid">
      <div class="oi-box"><div class="oi-h">Open interest by strike <span class="lg"><i class="ce"></i>Calls <i class="pe"></i>Puts <i class="atm"></i>At the money <i class="mp"></i>Max pain</span></div>${strikeBars(near, d, false)}</div>
      <div class="oi-box"><div class="oi-h">Change in OI today</div>${strikeBars(near, d, true)}</div>
      <div class="oi-box"><div class="oi-h">PCR through the day</div>${hist ? pcrChart(hist) : `<div class="skeleton" style="height:120px"></div>`}</div>
      <div class="oi-box"><div class="oi-h">Build-up near the money <span class="muted">from today's price and OI change</span></div>
        <table class="bu-table"><tr><th>Calls</th><th>Strike</th><th>Puts</th></tr>
        ${table.map((r) => `<tr class="${r.strike === d.atm ? "atm" : ""}"><td>${label(r.ce?.buildup)}</td><td class="ocf-k">${fmt(r.strike, 0)}</td><td>${label(r.pe?.buildup)}</td></tr>`).join("")}</table></div>
    </div>
    <div class="fine ocf-note">Build-up: price up + OI up = long build-up; price down + OI up = short build-up; price up + OI down = short covering; price down + OI down = long unwinding. These are common readings, not signals.</div></div>`;
  if (!hist) loadOiHistory();
}

// --- straddle
const S = { strike: null, data: null, chart: null, key: "", timer: 0 };
function destroyStraddle() { clearTimeout(S.timer); if (S.chart) { S.chart.remove(); S.chart = null; } S.key = ""; }
async function loadStraddle() {
  const d = C.data; if (!d || C.tab !== "straddle" || !C.full) return;
  const strike = S.strike ?? d.atm;
  try { S.data = await api(`/options/straddle?symbol=${encodeURIComponent(d.symbol)}&expiry=${encodeURIComponent(d.expiry)}&strike=${strike}`); S.error = null; }
  catch (err) { S.data = null; S.error = err.message; }
  if (C.tab === "straddle" && C.full) { S.key = ""; renderChainFull(); }
  clearTimeout(S.timer);
  if (S.data?.market_open) S.timer = setTimeout(loadStraddle, 15000);
}
function renderStraddle(box, top, d) {
  if (!T.lib) { lib().then(() => C.tab === "straddle" && renderChainFull()); return; }
  const s = S.data, want = S.strike ?? d.atm;
  const fresh = s && s.symbol === d.symbol && s.expiry === d.expiry && s.strike === want;
  const strikes = nearAtm(d, 15).map((r) => r.strike);
  const pick = `<select class="oc-sel" id="stStrike" aria-label="Strike">${strikes.map((k) => `<option value="${k}" ${k === want ? "selected" : ""}>${fmt(k, 0)}${k === d.atm ? " (ATM)" : ""}</option>`).join("")}</select>`;
  const key = fresh ? `${s.symbol}|${s.expiry}|${s.strike}|${s.t.length}` : "";
  if (fresh && key === S.key && S.chart) return;  // nothing new: keep the chart (and its zoom)
  destroyStraddle();
  const stats = fresh ? `<div class="oi-cards">
      <div><span class="muted">Straddle premium</span><b class="num">₹${fmt(s.premium)}</b><small class="${tone(s.premium - s.open)}">${pctTxt((s.premium / s.open - 1) * 100)} since the open</small></div>
      <div><span class="muted">Implied move</span><b class="num">±${fmt(s.implied_move_pct, 2)}%</b><small class="muted">premium ÷ spot, to expiry</small></div>
      <div><span class="muted">Breakevens at expiry</span><b class="num">${fmt(s.breakevens[0], 0)} – ${fmt(s.breakevens[1], 0)}</b></div>
      <div><span class="muted">Yesterday's close</span><b class="num">${s.prev_close ? "₹" + fmt(s.prev_close) : "—"}</b></div></div>` : "";
  box.innerHTML = top + `<div class="oi-wrap"><div class="st-head"><b>${esc(d.symbol)} ${fmt(want, 0)} straddle</b> · call + put, today ${pick}
      <span class="lg"><i class="st"></i>Straddle <i class="ce"></i>Call <i class="pe"></i>Put</span></div>${stats}
    <div class="st-chart" id="stChart">${fresh ? "" : S.error ? `<div class="muted oi-empty">${esc(S.error)}</div>` : `<div class="skeleton" style="height:100%"></div>`}</div>
    <div class="fine ocf-note">Built from NSE's trades for both contracts, per minute. NSE only publishes today's trades, so the chart starts at the open.</div></div>`;
  if (!fresh) { if (!S.error || S.strike !== want) loadStraddle(); return; }
  S.key = key;
  const L = T.lib, el = $("#stChart");
  const chart = S.chart = L.createChart(el, { autoSize: true, layout: { background: { type: L.ColorType.Solid, color: css("--panel") }, textColor: css("--muted"), fontSize: 11, attributionLogo: true },
    grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } }, rightPriceScale: { borderColor: css("--border") },
    timeScale: { borderColor: css("--border"), timeVisible: true, secondsVisible: false, tickMarkFormatter: (t) => F.time.format(new Date(t * 1000)) },
    localization: { timeFormatter: (t) => F.time.format(new Date(t * 1000)), priceFormatter: (p) => fmt(p) } });  // local time, not UTC
  const line = (color, width, values) => chart.addSeries(L.LineSeries, { color, lineWidth: width, priceLineVisible: false }).setData(s.t.map((t, i) => ({ time: t, value: values[i] })));
  line(css("--brand"), 2.5, s.straddle); line(css("--down"), 1.5, s.ce); line(css("--up"), 1.5, s.pe);
  chart.timeScale().fitContent();
}

// --- strategy builder
const B = { sym: null, exp: null, legs: [], res: null, err: null, at: 0, seq: 0, timer: 0, product: store.get("kairo-sg-product", "DELIVERY") };
// Strikes are steps away from the ATM row: [side, kind, step]
const TEMPLATES = [
  ["Bull call spread", [["BUY", "CE", 0], ["SELL", "CE", 2]]], ["Bear put spread", [["BUY", "PE", 0], ["SELL", "PE", -2]]],
  ["Bull put spread", [["SELL", "PE", 0], ["BUY", "PE", -2]]], ["Bear call spread", [["SELL", "CE", 0], ["BUY", "CE", 2]]],
  ["Long straddle", [["BUY", "CE", 0], ["BUY", "PE", 0]]], ["Short straddle", [["SELL", "CE", 0], ["SELL", "PE", 0]]],
  ["Long strangle", [["BUY", "CE", 2], ["BUY", "PE", -2]]], ["Short strangle", [["SELL", "CE", 2], ["SELL", "PE", -2]]],
  ["Iron condor", [["BUY", "PE", -6], ["SELL", "PE", -3], ["SELL", "CE", 3], ["BUY", "CE", 6]]],
  ["Iron butterfly", [["BUY", "PE", -4], ["SELL", "PE", 0], ["SELL", "CE", 0], ["BUY", "CE", 4]]],
];
const rupees = (v, unlimited = "Unlimited") => (v == null ? unlimited : `${v < 0 ? "−" : ""}₹${fmt(Math.abs(v), 0)}`);
function sgSync(d) {
  if (B.sym !== d.symbol) { B.legs = []; B.res = null; B.err = null; }
  else if (B.exp !== d.expiry) { const ks = new Set(d.rows.map((r) => r.strike)); B.legs = B.legs.filter((l) => ks.has(l.strike)); B.res = null; }
  else return;
  B.sym = d.symbol; B.exp = d.expiry;
  if (B.legs.length) sgQueue();
}
function sgTemplate(i, d) {
  const atm = Math.max(0, d.rows.findIndex((r) => r.strike === d.atm));
  B.name = TEMPLATES[i][0];
  B.legs = TEMPLATES[i][1].map(([side, kind, step]) => ({ side, kind, lots: 1, strike: d.rows[Math.min(d.rows.length - 1, Math.max(0, atm + step))].strike }));
  sgQueue(0);
}
function sgQueue(wait = 250, edited = false) {
  if (edited) B.name = "";
  clearTimeout(B.timer);
  if (!B.legs.length) { B.res = null; B.err = null; sgBody(); return; }
  B.timer = setTimeout(sgAnalyse, wait);
}
async function sgAnalyse() {
  const seq = ++B.seq, legs = B.legs.map((l) => ({ ...l }));
  try {
    const r = await api("/options/strategy", { method: "POST", body: { symbol: B.sym, expiry: B.exp, legs } });
    if (seq !== B.seq) return;
    B.res = r; B.err = null;
  } catch (err) { if (seq !== B.seq) return; B.res = null; B.err = err.message; }
  B.at = Date.now();
  sgBody();
}
function sgLegsHtml(d) {
  const strikes = d.rows.map((r) => r.strike), row = (k) => d.rows.find((r) => r.strike === k);
  const legs = B.legs.map((l, i) => {
    const side = row(l.strike)?.[l.kind === "CE" ? "ce" : "pe"];
    return `<div class="sg-leg" data-i="${i}">
      <button class="sg-tg ${l.side === "BUY" ? "b" : "s"}" data-flip="side" title="Buy / sell">${l.side === "BUY" ? "B" : "S"}</button>
      <button class="sg-tg k" data-flip="kind" title="Call / put">${l.kind}</button>
      <select class="oc-sel" data-f="strike" aria-label="Strike">${strikes.map((k) => `<option value="${k}" ${k === l.strike ? "selected" : ""}>${fmt(k, k % 1 ? 2 : 0)}${k === d.atm ? " ATM" : ""}</option>`).join("")}</select>
      <label class="sg-lots"><input type="number" min="1" max="500" step="1" value="${l.lots}" data-f="lots" aria-label="Lots"> lot${l.lots > 1 ? "s" : ""}</label>
      <span class="num sg-px">${side?.ltp ? "₹" + fmt(side.ltp) : "—"}</span>
      <span class="muted sg-iv">${side?.greeks?.iv ? `IV ${fmt(side.greeks.iv, 1)}` : ""}</span>
      <button class="icon-btn sg-x" data-drop aria-label="Remove leg">✕</button></div>`;
  }).join("");
  return `<div class="sg-tpl">${TEMPLATES.map(([n], i) => `<button class="chip" data-tpl="${i}">${n}</button>`).join("")}</div>
    <div class="sg-legs">${legs || `<div class="muted sg-hint">Pick a ready-made strategy above or add legs one by one.</div>`}</div>
    <div class="sg-add">${B.legs.length < 8 ? `<button class="btn sm" data-add>+ Add leg</button>` : ""}
      <span class="muted">Lot size ${d.lot_size ? fmt(d.lot_size, 0) : "—"} · prices are the last traded premiums</span></div>`;
}
function sgChart(r, el) {
  const W = Math.max(320, el.clientWidth || 700), H = 260, L = 64, R = 12, T = 14, Bm = 28;
  const dev = r.spot * (r.vol_used / 100) * Math.sqrt(Math.max(r.days_to_expiry, 0.5) / 365);
  const ks = r.legs.map((l) => l.strike);
  const x0 = Math.min(r.spot - 3 * dev, ...ks) - r.spot * 0.01, x1 = Math.max(r.spot + 3 * dev, ...ks) + r.spot * 0.01;
  const pts = r.curve.filter((p) => p.s >= x0 && p.s <= x1);
  if (pts.length < 2) return "";
  const vals = pts.flatMap((p) => [p.expiry, p.today]);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals); const padv = (hi - lo || 1) * 0.08; lo -= padv; hi += padv;
  const sx0 = pts[0].s, sx1 = pts[pts.length - 1].s;
  const X = (s) => L + ((s - sx0) / (sx1 - sx0)) * (W - L - R), Y = (v) => T + ((hi - v) / (hi - lo)) * (H - T - Bm);
  const path = (k) => pts.map((p, i) => `${i ? "L" : "M"}${X(p.s).toFixed(1)},${Y(p[k]).toFixed(1)}`).join("");
  const z = Y(0), area = `${path("expiry")}L${X(sx1).toFixed(1)},${z.toFixed(1)}L${X(sx0).toFixed(1)},${z.toFixed(1)}Z`;
  const ticks = Array.from({ length: 5 }, (_, i) => sx0 + ((sx1 - sx0) * (i + 0.5)) / 5);
  const be = r.breakevens.filter((b) => b >= sx0 && b <= sx1);
  B.chart = { pts, X, Y, W, H };
  return `<svg class="sg-svg" viewBox="0 0 ${W} ${H}" width="100%" height="${H}">
    <defs><clipPath id="sgUp"><rect x="0" y="0" width="${W}" height="${z}"/></clipPath><clipPath id="sgDn"><rect x="0" y="${z}" width="${W}" height="${H - z}"/></clipPath></defs>
    <path d="${area}" fill="var(--up)" opacity=".16" clip-path="url(#sgUp)"/><path d="${area}" fill="var(--down)" opacity=".16" clip-path="url(#sgDn)"/>
    <line x1="${L}" x2="${W - R}" y1="${z}" y2="${z}" class="sg-zero"/>
    <line x1="${X(r.spot)}" x2="${X(r.spot)}" y1="${T}" y2="${H - Bm}" class="sg-spot"/><text x="${X(r.spot) + 4}" y="${T + 10}" class="ax">Spot ${fmt(r.spot, 0)}</text>
    ${be.map((b) => `<circle cx="${X(b)}" cy="${z}" r="3.5" class="sg-be"/><text x="${X(b)}" y="${z - 8}" class="ax" text-anchor="middle">${fmt(b, 0)}</text>`).join("")}
    <path d="${path("today")}" fill="none" stroke="var(--brand)" stroke-width="1.6" stroke-dasharray="5 4"/>
    <path d="${path("expiry")}" fill="none" stroke="var(--text)" stroke-width="2"/>
    <text x="${L - 6}" y="${Y(hi - padv) + 4}" class="ax" text-anchor="end">${rupees(hi - padv)}</text><text x="${L - 6}" y="${z + 4}" class="ax" text-anchor="end">0</text>
    <text x="${L - 6}" y="${Y(lo + padv) + 4}" class="ax" text-anchor="end">${rupees(lo + padv)}</text>
    ${ticks.map((s) => `<text x="${X(s)}" y="${H - 8}" class="ax" text-anchor="middle">${fmt(s, 0)}</text>`).join("")}
    <g class="sg-hov" visibility="hidden"><line y1="${T}" y2="${H - Bm}"/><circle r="4"/></g></svg>`;
}
function sgResHtml() {
  if (!B.legs.length) return "";
  const r = B.res;
  if (!r) return B.err ? `<div class="muted oi-empty">${esc(B.err)}</div>` : `<div class="skeleton" style="height:300px"></div>`;
  const debit = Math.max(0, -r.net_premium), need = debit + (r.margin || 0);
  const g = r.greeks;
  return `<div class="oi-cards sg-cards">
      <div><span class="muted">${r.net_premium >= 0 ? "You receive" : "You pay"}</span><b class="num">${rupees(Math.abs(r.net_premium))}</b><small class="muted">net premium</small></div>
      <div><span class="muted">Max profit</span><b class="num up">${rupees(r.max_profit)}</b></div>
      <div><span class="muted">Max loss</span><b class="num down">${rupees(r.max_loss)}</b></div>
      <div><span class="muted">Breakeven${r.breakevens.length > 1 ? "s" : ""}</span><b class="num">${r.breakevens.length ? r.breakevens.map((b) => fmt(b, 0)).join(" · ") : "—"}</b></div>
      <div><span class="muted">Chance of profit</span><b class="num">${r.pop != null ? fmt(r.pop, 0) + "%" : "—"}</b><small class="muted">at ${fmt(r.vol_used, 1)}% volatility</small></div>
      <div><span class="muted">Funds needed (paper)</span><b class="num">${rupees(need)}</b><small class="muted">${r.margin ? `incl. ₹${fmt(r.margin, 0)} margin` : "premium only"}</small></div></div>
    <div class="sg-greeks"><span>Delta <b class="num">${fmt(g.delta, 2)}</b></span><span>Gamma <b class="num">${fmt(g.gamma, 4)}</b></span>
      <span>Theta <b class="num ${tone(g.theta)}">${rupees(g.theta, "—")}/day</b></span><span>Vega <b class="num">${rupees(g.vega, "—")}</b></span>
      <span class="sg-read muted" id="sgRead">Hover the chart to see profit or loss at any price</span>
      <span class="lg"><i class="sx"></i>At expiry <i class="st"></i>Today</span></div>
    <div class="sg-chart" id="sgChart"></div>
    <div class="sg-act"><div class="seg"><button data-prod="INTRADAY" class="${B.product === "INTRADAY" ? "on" : ""}">Intraday</button><button data-prod="DELIVERY" class="${B.product === "DELIVERY" ? "on" : ""}">Carry forward</button></div>
      <span class="term-sp"></span><button class="btn" data-sgsave>Save as basket</button><button class="btn primary" data-sgplace>Place ${r.legs.length} order${r.legs.length > 1 ? "s" : ""}</button></div>
    <div class="fine ocf-note">Payoff for ${fmt(r.days_to_expiry, 1)} days to expiry. "Today" uses Black-Scholes with each leg's implied volatility; chance of profit assumes a lognormal move at the ATM volatility. Paper margin for written options is 15% of the strike value (real SPAN margin differs). Buy legs are placed first.</div>`;
}
function sgOrders() {
  return (B.res?.legs || []).map((l) => ({ symbol: l.symbol, label: l.label, side: l.side, qty: l.qty, order_type: "MARKET", product: B.product }));
}
function sgBody() {
  const wrap = $("#termChainFull .sg-wrap"), d = C.data;
  if (!wrap || !d) return;
  const legsEl = wrap.querySelector(".sg-build");
  if (!(legsEl && legsEl.contains(document.activeElement) && document.activeElement.tagName === "INPUT")) legsEl.innerHTML = sgLegsHtml(d);
  wrap.querySelector(".sg-res").innerHTML = sgResHtml();
  const ch = wrap.querySelector("#sgChart");
  if (ch && B.res) { ch.innerHTML = sgChart(B.res, ch); wireSgHover(ch); }
}
function wireSgHover(ch) {
  const svg = ch.querySelector("svg"), read = $("#sgRead"); if (!svg || !B.chart) return;
  const hov = svg.querySelector(".sg-hov"), [line, dot] = hov.children;
  svg.onpointermove = (e) => {
    const { pts, X, Y, W } = B.chart, rect = svg.getBoundingClientRect(), x = ((e.clientX - rect.left) * W) / rect.width;
    let p = pts[0]; for (const q of pts) if (Math.abs(X(q.s) - x) < Math.abs(X(p.s) - x)) p = q;
    line.setAttribute("x1", X(p.s)); line.setAttribute("x2", X(p.s)); dot.setAttribute("cx", X(p.s)); dot.setAttribute("cy", Y(p.expiry));
    hov.setAttribute("visibility", "visible");
    read.innerHTML = `At <b class="num">${fmt(p.s, 0)}</b>: expiry <b class="num ${tone(p.expiry)}">${p.expiry >= 0 ? "+" : ""}${rupees(p.expiry)}</b> · today <b class="num ${tone(p.today)}">${p.today >= 0 ? "+" : ""}${rupees(p.today)}</b>`;
  };
  svg.onpointerleave = () => { hov.setAttribute("visibility", "hidden"); read.textContent = "Hover the chart to see profit or loss at any price"; };
}
function renderStrategy(box, top, d) {
  sgSync(d);
  const key = `${d.symbol}|${d.expiry}`;
  if (box.dataset.sg === key && box.querySelector(".sg-wrap")) {
    // Chain refresh: swap the header, keep the builder (and anything being typed); re-price every 15 s.
    const tmp = document.createElement("div"); tmp.innerHTML = top;
    box.querySelectorAll(":scope > .ocf-top, :scope > .ocf-stats").forEach((el) => el.remove());
    box.prepend(...tmp.children);
    if (B.legs.length && d.market_open && Date.now() - B.at > 15000) sgAnalyse();
    return;
  }
  box.dataset.sg = key;
  box.innerHTML = top + `<div class="oi-wrap sg-wrap"><div class="sg-build"></div><div class="sg-res"></div></div>`;
  sgBody();
}
async function sgClick(e) {
  const d = C.data, t = e.target;
  const tpl = t.closest("[data-tpl]"); if (tpl) { sgTemplate(Number(tpl.dataset.tpl), d); sgBody(); return; }
  if (t.closest("[data-add]")) { B.legs.push({ side: "BUY", kind: "CE", strike: d.atm, lots: 1 }); sgQueue(0, true); sgBody(); return; }
  const leg = t.closest(".sg-leg"), i = leg ? Number(leg.dataset.i) : -1;
  const flip = t.closest("[data-flip]");
  if (flip && i >= 0) { const l = B.legs[i]; if (flip.dataset.flip === "side") l.side = l.side === "BUY" ? "SELL" : "BUY"; else l.kind = l.kind === "CE" ? "PE" : "CE"; sgQueue(250, true); sgBody(); return; }
  if (t.closest("[data-drop]") && i >= 0) { B.legs.splice(i, 1); sgQueue(250, true); sgBody(); return; }
  const prod = t.closest("[data-prod]"); if (prod) { B.product = prod.dataset.prod; store.set("kairo-sg-product", B.product); sgBody(); return; }
  if (t.closest("[data-sgplace]") && B.res) { await placeBasket(sgOrders()); return; }
  if (t.closest("[data-sgsave]") && B.res) {
    const name = prompt("Name this basket", `${B.sym} ${B.name || "strategy"} ${B.exp.slice(0, 6).replace("-", " ")}`);
    if (name) await saveBasket(sgOrders(), name);
  }
}
function sgChange(e) {
  const leg = e.target.closest(".sg-leg"); if (!leg) return;
  const l = B.legs[Number(leg.dataset.i)], f = e.target.dataset.f;
  if (f === "strike") l.strike = Number(e.target.value);
  if (f === "lots") l.lots = Math.min(500, Math.max(1, Math.round(Number(e.target.value) || 1)));
  e.target.blur?.();
  sgQueue(250, true); sgBody();
}

function wireChain() {
  const onPick = (e) => {
    if (e.target.closest(".sg-wrap")) { sgChange(e); return; }
    if (e.target.id === "stStrike") { S.strike = Number(e.target.value); S.data = null; S.error = null; renderChainFull(); return; }
    const s = e.target.closest("select[data-pick]"); if (!s) return;
    if (s.dataset.pick === "und") { S.strike = null; setUnderlying(s.value); }
    else { C.expiry = s.value; C.centred = ""; C.data = null; S.strike = null; renderChain(); renderChainFull(); loadChain(); }
  };
  const onClick = (e) => {
    if (e.target.closest(".sg-wrap")) { sgClick(e); return; }
    const b = e.target.closest(".oc-ltp[data-id]");
    if (b) { C.full = false; renderChainFull(); openSymbol(`OPT:${b.dataset.id}`); return; }
    const tab = e.target.closest("[data-ocf]");
    if (tab) { C.tab = tab.dataset.ocf; store.set("kairo-ocf-tab", C.tab); lib().then(renderChainFull); return; }
    const view = e.target.closest("[data-view]");
    if (view) { C.view = view.dataset.view; store.set("kairo-ocf-view", C.view); renderChainFull(); return; }
    if (e.target.closest("#ocExpand")) { C.full = true; renderChainFull(); loadChain(); }
    if (e.target.closest("#ocClose")) { C.full = false; renderChainFull(); scheduleChain(); }
  };
  for (const el of [$("#termPanelBody"), $("#termChainFull")]) { el.addEventListener("change", onPick); el.addEventListener("click", onClick); }
}
/** The chain follows the chart: a stock or index with options brings up its own chain. */
async function followUnderlying(sym) {
  if (isOpt(sym)) return;
  try { const r = await api(`/options/for/${encodeURIComponent(sym)}`); if (T.sym === sym) setUnderlying(r.symbol); } catch { /* no options: keep the current chain */ }
}
function ticket(side) {
  const last = T.bars[T.bars.length - 1];
  openTicket({ symbol: T.sym, side, ltp: (isOpt(T.sym) ? null : T.day?.price) ?? last?.close ?? null, label: label(T.sym) });
}
function renderToolbar() {
  const can = tradable(T.sym);
  for (const id of ["#termBuy", "#termSell"]) { $(id).disabled = !can; $(id).title = can ? (id === "#termBuy" ? "Buy (B)" : "Sell (S)") : "Indices can't be traded. Open a stock or an option."; }
  document.querySelectorAll("#termIntervals button").forEach((b) => { b.classList.toggle("on", b.dataset.i === T.interval); b.disabled = isOpt(T.sym) && b.dataset.i === "1D"; });
  document.querySelectorAll("#termSpans button").forEach((b) => (b.disabled = isOpt(T.sym) && b.dataset.s !== "1d"));
  document.querySelectorAll("#termSpans button").forEach((b) => b.classList.toggle("on", b.dataset.s === T.span));
  $("#termType").classList.toggle("on", T.type === "candle");
  $("#termType").title = T.type === "candle" ? "Show line" : "Show candles";
  document.querySelectorAll("#termScale button").forEach((b) => b.classList.toggle("on", b.dataset.m === T.scale));
  $("#termHide").classList.toggle("on", T.hideDraw);
  $("#termIndCount").textContent = Object.values(T.ind).filter(Boolean).length || "";
}

// ------------------------------------------------------------------ actions
function setInterval_(iv, keepSpan = false) {
  if (!keepSpan) T.span = null;
  if (iv === T.interval && T.bars.length) { renderToolbar(); return; }
  T.interval = iv; store.set("kairo-term-interval", iv); T.bars = []; T.pending = null;
  renderToolbar(); loadBars();
}
function applySpan(key) {
  const span = SPANS.find((s) => s[1] === key); if (!span || !T.chart || !T.bars.length) return;
  const [, , , days] = span, last = T.bars[T.bars.length - 1].time;
  if (days == null) { T.chart.timeScale().fitContent(); return; }
  const from = last - days * 86400, i = T.bars.findIndex((b) => b.time >= from);
  T.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, i === -1 ? 0 : i) - 0.5, to: T.bars.length + 4 });
}
function chooseSpan(key) {
  const span = SPANS.find((s) => s[1] === key); if (!span) return;
  T.span = key;
  if (span[2] !== T.interval) setInterval_(span[2], true); else { applySpan(key); renderToolbar(); }
}
function openSymbol(sym) {
  sym = sym.toUpperCase();
  if (sym === T.sym && T.bars.length) return;
  T.sym = sym; store.set("kairo-term-symbol", sym); T.opt = null;
  if (isOpt(sym) && T.interval === "1D") { T.interval = "5m"; store.set("kairo-term-interval", "5m"); }
  renderToolbar();
  followUnderlying(sym);
  T.data = null; T.bars = []; T.day = null; T.pending = null; T.error = null;
  if (location.hash.indexOf(`symbol=${encodeURIComponent(sym)}`) === -1) history.replaceState(null, "", `#/terminal?symbol=${encodeURIComponent(sym)}`);
  renderHeader(); renderPanel(); loadBars(); startStream();
}

function wireSearch() {
  const input = $("#termSearch"), list = $("#termSuggest");
  let items = [], sel = 0, timer = 0;
  const close = () => { list.hidden = true; };
  const show = () => {
    list.hidden = !items.length;
    list.innerHTML = items.map(([s, n], i) => `<button type="button" data-s="${esc(s)}" class="${i === sel ? "sel" : ""}">${logoHtml(s, n, 22)}<b>${esc(splitSym(s)[0])}</b><span class="muted">${esc(n)}</span></button>`).join("");
  };
  input.addEventListener("input", () => {
    const q = input.value.trim(); clearTimeout(timer); sel = 0;
    const idx = INDICES.filter(([s, n]) => q && (s.toLowerCase().includes(q.toLowerCase()) || n.toLowerCase().includes(q.toLowerCase())));
    items = idx.slice(0, 3); show();
    if (q.length >= 2) timer = setTimeout(async () => {
      const hits = await searchStocks(q);
      if (input.value.trim() !== q) return;
      items = [...idx.slice(0, 3), ...hits.map((h) => [h.symbol, `${h.name} · ${h.exchange}`])].slice(0, 9); show();
    }, 150);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); sel = (sel + (e.key === "ArrowDown" ? 1 : -1) + items.length) % Math.max(1, items.length); show(); }
    else if (e.key === "Enter") { const it = items[sel]; if (it) { openSymbol(it[0]); input.value = ""; input.blur(); close(); } }
    else if (e.key === "Escape") { input.blur(); close(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
  list.addEventListener("mousedown", (e) => { const b = e.target.closest("button"); if (b) { openSymbol(b.dataset.s); input.value = ""; close(); } });
}

// ------------------------------------------------------------------ public
export function initTerminal() {
  $("#termIntervals").innerHTML = INTERVALS.map(([k, l]) => `<button data-i="${k}">${l}</button>`).join("");
  $("#termIntervals").onclick = (e) => { const b = e.target.closest("button"); if (b) setInterval_(b.dataset.i); };
  $("#termSpans").innerHTML = SPANS.map(([l, k]) => `<button data-s="${k}">${l}</button>`).join("");
  $("#termSpans").onclick = (e) => { const b = e.target.closest("button"); if (b) chooseSpan(b.dataset.s); };
  $("#termType").onclick = () => { T.type = T.type === "candle" ? "line" : "candle"; store.set("kairo-term-type", T.type); renderToolbar(); if (T.lib && T.bars.length) buildChart(); };
  $("#termScale").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    T.scale = T.scale === b.dataset.m ? "normal" : b.dataset.m; store.set("kairo-term-scale", T.scale); renderToolbar();
    T.chart?.priceScale("right").applyOptions({ mode: { normal: 0, log: 1, pct: 2 }[T.scale] });
  };
  $("#termIndMenu").innerHTML = IND.map(([k, l]) => `<label><input type="checkbox" data-k="${k}"> ${l}</label>`).join("");
  $("#termIndBtn").onclick = (e) => {
    e.stopPropagation(); const m = $("#termIndMenu"); m.hidden = !m.hidden;
    m.querySelectorAll("input").forEach((i) => (i.checked = !!T.ind[i.dataset.k]));
  };
  $("#termIndMenu").onchange = (e) => {
    const k = e.target.dataset.k; if (!k) return;
    T.ind = { ...T.ind, [k]: e.target.checked }; store.set("kairo-term-ind", T.ind); renderToolbar();
    if (T.lib && T.bars.length) buildChart();
  };
  document.addEventListener("click", (e) => { if (!e.target.closest("#termIndMenu, #termIndBtn")) $("#termIndMenu").hidden = true; });
  $("#termTools").innerHTML = TOOLS.map(([k, l, svg]) => `<button data-tool="${k}" title="${l}" aria-label="${l}"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${svg}</svg></button>`).join("")
    + `<span class="tt-sep"></span><button id="termHide" title="Hide drawings" aria-label="Hide drawings"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg></button>
       <button id="termClear" title="Delete all drawings" aria-label="Delete all drawings"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button>`;
  $("#termTools").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.tool) setTool(b.dataset.tool);
    else if (b.id === "termHide") { T.hideDraw = !T.hideDraw; renderToolbar(); drawDrawings(); }
    else if (b.id === "termClear" && drawings().length && confirm("Delete all drawings on this chart?")) { saveDrawings([]); drawDrawings(); }
  };
  $("#termShot").onclick = () => {
    if (!T.chart) return;
    const a = document.createElement("a");
    a.href = T.chart.takeScreenshot().toDataURL("image/png"); a.download = `${splitSym(T.sym)[0]}-${T.interval}.png`; a.click();
  };
  $("#termFull").onclick = () => { const el = $("#view-terminal"); document.fullscreenElement ? document.exitFullscreen() : el.requestFullscreen?.(); };
  $("#termTabs").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; T.panel = b.dataset.p; store.set("kairo-term-panel", T.panel); renderPanel(); };
  $("#termPanelBody").onclick = (e) => { const b = e.target.closest(".tw-row"); if (b) openSymbol(b.dataset.sym); };
  document.addEventListener("keydown", (e) => {
    if (!T.visible || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    if (e.key === "Escape") setTool("cross");
    if ((e.key === "b" || e.key === "B") && !e.ctrlKey && !e.metaKey && tradable(T.sym) && !document.querySelector(".scrim")) { e.preventDefault(); ticket("BUY"); }
    if ((e.key === "s" || e.key === "S") && !e.ctrlKey && !e.metaKey && tradable(T.sym) && !document.querySelector(".scrim")) { e.preventDefault(); ticket("SELL"); }
  });
  wireSearch();
  wireChain();
  $("#termChainBtn").onclick = () => { C.full = true; renderChainFull(); loadChain(); };  // narrow screens: no side panel
  $("#termTradeBtn").onclick = () => modal("Trading", `<div class="term-panel-body" id="tradeModalBody"></div>`, (m) => {
    showTradePanel(m.querySelector("#tradeModalBody"), (s) => { document.querySelector(".scrim")?.remove(); hideTradePanel(); openSymbol(s); });
    const obs = new MutationObserver(() => { if (!document.body.contains(m)) { hideTradePanel(); obs.disconnect(); } });
    obs.observe(document.body, { childList: true });
  });
  $("#termBuy").onclick = () => ticket("BUY");
  $("#termSell").onclick = () => ticket("SELL");
  refreshStatus();
  setInterval(() => { const c = $("#termClock"); if (c && T.visible) c.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }, 1000);
  new MutationObserver(() => { if (T.lib && T.bars.length) buildChart(); }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  onTabSleep((awake) => { if (!T.visible) return; if (awake) startStream(); else stopStream(); });
  onRoute((r) => { if (r.view !== "terminal") hideTerminal(); });
  T.off.push(feed.on("quote", () => T.visible && renderWatch()), feed.on("tick", () => T.visible && renderWatch()));
}

export function showTerminal(params) {
  T.visible = true;
  const broker = params.get("broker");
  if (broker === "connected") { toast("Upstox connected", "Switch to Live in Trade → Funds when you're ready.", "ok"); T.panel = "trade"; refreshStatus(); }
  if (broker === "failed") toast("Upstox login failed", "Check the app key, secret and redirect URL, then try again.", "err");
  const sym = (params.get("symbol") || T.sym || store.get("kairo-term-symbol", "") || "^NSEI").toUpperCase();
  feed.require("terminal", getWatchlist().filter((s) => !s.startsWith("MF")));
  renderToolbar(); renderPanel();
  if (sym !== T.sym || !T.bars.length) openSymbol(sym);
  else { startStream(); renderHeader(); }
}
export function hideTerminal() {
  if (!T.visible) return;
  T.visible = false; stopStream(); feed.release("terminal"); clearTimeout(C.timer);
}
