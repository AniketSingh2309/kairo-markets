// Markets view: live watchlist, instrument header, 1D-to-All charts (line or candles), outlook, research.
import { $, esc, store, fmt, money, signed, pctTxt, compact, timeTxt, tzName, css, splitSym, api, app, navigate, feed, logoHtml, onTabSleep } from "./core.js";
import { openAlertDialog } from "./alerts.js";
import { loadResearch } from "./research.js";

const S = {
  watch: [], quotes: {}, rows: new Map(), active: null, tab: "overview", range: "1D", charts: {}, logoKey: "", companies: {}, venues: {},
  chartType: store.get("kairo-chart-type", "line"),
  overlays: store.get("kairo-overlays", { sma20: true, sma50: true, sma200: false, bb: false }),
  horizon: 5, fc: null, fcState: "idle", fcError: null, day: null, research: {},
};
let dayES = null, ageTimer = 0;

export const getWatchlist = () => [...S.watch];
export const quoteOf = (sym) => S.quotes[sym];
export function knownNames() {
  const m = new Map();
  Object.values(S.quotes).forEach((q) => q.name && m.set(q.symbol, q.name));
  return m;
}
export function redraw() { drawChart(); drawSparks(); }

// ============================================================ watchlist
function renderWatchlist() {
  const ul = $("#wl"); S.rows.clear();
  $("#wlCount").textContent = S.watch.length || "";
  if (!S.watch.length) { ul.innerHTML = `<div class="wl-empty">Your watchlist is empty. Search a symbol and add it.</div>`; return; }
  ul.innerHTML = "";
  for (const sym of S.watch) {
    const li = document.createElement("li");
    const [base, ex] = splitSym(sym);
    li.title = sym;
    li.innerHTML = `<div class="wl-id">${logoHtml(sym, S.quotes[sym]?.name || app.names.get(sym), 30)}<div style="min-width:0"><div class="sym">${esc(base)}${ex ? `<small>${ex}</small>` : ""}</div><div class="nm">&nbsp;</div></div></div>
      <svg class="spark" width="52" height="26"></svg><div><div class="px num">—</div><div class="ch num">&nbsp;</div></div>
      <button class="rm" title="Remove from watchlist" aria-label="Remove ${esc(sym)}">×</button>`;
    li.onclick = (e) => { if (e.target.classList.contains("rm")) { toggleWatch(sym, false); e.stopPropagation(); } else navigate("markets", "", { symbol: sym }); };
    ul.appendChild(li); S.rows.set(sym, li);
    updateRow(sym);
  }
  markActiveRow();
}
function updateRow(sym, dir = 0) {
  const li = S.rows.get(sym), q = S.quotes[sym]; if (!li || !q) return;
  li.querySelector(".nm").textContent = q.error ? q.error : (q.name || "");
  const px = li.querySelector(".px");
  px.textContent = q.price != null ? fmt(q.price) : "—";
  const ch = li.querySelector(".ch");
  ch.textContent = q.change_pct != null ? pctTxt(q.change_pct) : "";
  ch.className = "ch num " + (q.change_pct == null ? "" : q.change_pct >= 0 ? "up" : "down");
  if (dir) { px.classList.remove("flash-up", "flash-down"); void px.offsetWidth; px.classList.add(dir > 0 ? "flash-up" : "flash-down"); setTimeout(() => px.classList.remove("flash-up", "flash-down"), 600); }
  drawSpark(li.querySelector(".spark"), q);
}
function drawSpark(svg, q) {
  const s = q.spark || []; if (s.length < 2) { svg.innerHTML = ""; return; }
  const ref = q.previous_close ?? s[0];
  const lo = Math.min(...s, ref), hi = Math.max(...s, ref), r = hi - lo || 1;
  const X = (i) => 1 + (i / (s.length - 1)) * 50, Y = (v) => 2 + (1 - (v - lo) / r) * 22;
  const col = (q.change_pct ?? 0) >= 0 ? css("--up") : css("--down");
  svg.innerHTML = `<line x1="0" x2="52" y1="${Y(ref)}" y2="${Y(ref)}" stroke="${css("--faint")}" stroke-dasharray="2,2" stroke-width="1"/>
    <polyline points="${s.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ")}" fill="none" stroke="${col}" stroke-width="1.6" stroke-linejoin="round"/>`;
}
function drawSparks() { S.rows.forEach((li, sym) => S.quotes[sym] && drawSpark(li.querySelector(".spark"), S.quotes[sym])); }
function markActiveRow() { S.rows.forEach((li, sym) => li.classList.toggle("active", sym === S.active)); }
function toggleWatch(sym, on) {
  S.watch = on ? [...new Set([...S.watch, sym])] : S.watch.filter((s) => s !== sym);
  store.set(`kairo-watch-${app.provider}`, S.watch);
  renderWatchlist(); renderActions(); startWatchStream();
}
function setFeed(state, txt) { $("#feed").className = "feed " + state; $("#feedTxt").textContent = txt; }
let feedWired = false;
function startWatchStream() {
  feed.require("watchlist", S.watch);
  if (feedWired) return;
  feedWired = true;
  feed.on("quote", (d) => {
    S.quotes[d.symbol] = { ...S.quotes[d.symbol], ...d, error: null };
    if (d.name) app.names.set(d.symbol, d.name);
    updateRow(d.symbol); if (d.symbol === S.active && !S.day?.snap) renderHeader();
  });
  feed.on("tick", (d) => {
    const q = S.quotes[d.symbol] || (S.quotes[d.symbol] = { symbol: d.symbol, spark: [] });
    const dir = q.price == null ? 0 : Math.sign(d.price - q.price);
    q.price = d.price;
    if (d.change != null) { q.change = d.change; q.change_pct = d.change_percent; }
    q.spark = [...(q.spark || []), d.price].slice(-90);
    updateRow(d.symbol, dir);
  });
  feed.on("failure", (d) => {
    S.quotes[d.symbol] = { ...S.quotes[d.symbol], symbol: d.symbol, error: d.code === "UNKNOWN_SYMBOL" ? "Unknown symbol" : "Data unavailable" };
    updateRow(d.symbol);
  });
  feed.on("status", (d) => {
    const map = { connected: ["on", "Live feed"], connecting: ["warn", "Connecting"], reconnecting: ["warn", "Reconnecting"], unavailable: ["", "Snapshot only"], idle: ["", "Idle"], paused: ["", "Paused"] };
    setFeed(...(map[d.stream] || ["", d.stream]));
  });
}

// ============================================================ instrument
export function selectSymbol(sym) {
  sym = sym.toUpperCase();
  if (sym === S.active) return;
  S.active = sym; S.day = null; S.fc = null; S.fcState = "idle";
  document.title = `${splitSym(sym)[0]} · Kairo Markets`;
  markActiveRow(); renderActions();
  startDayStream(sym);
  renderHeader(); renderStats(); renderTape();
  if (S.tab === "outlook") loadForecast();
  loadChart();  // also starts prefetching the other ranges, so switching ranges is instant
  loadCompany(sym);
  loadVenues(sym);
  loadResearch(sym);
  renderOutlook(); renderResearch(); drawChart();
}
function renderActions() {
  const b = $("#watchBtn"), a = $("#alertBtn");
  if (!S.active) { b.hidden = a.hidden = true; return; }
  const inList = S.watch.includes(S.active);
  b.hidden = a.hidden = false;
  b.innerHTML = inList ? `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z"/></svg>In watchlist`
                       : `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>Watchlist`;
  b.onclick = () => toggleWatch(S.active, !inList);
  a.onclick = () => openAlertDialog(S.active, S.day?.price ?? S.quotes[S.active]?.price, S.day?.snap?.currency);
}
function renderHeader() {
  const sym = S.active; if (!sym) return;
  const d = S.day?.snap, q = S.quotes[sym] || {};
  const name = d?.name || q.name, exch = d?.exchange || q.exchange, cur = d?.currency || q.currency;
  const price = S.day?.price ?? q.price, chg = S.day?.change ?? q.change, chgPct = S.day?.changePct ?? q.change_pct;
  $("#iSym").textContent = splitSym(sym)[0]; $("#iSym").title = sym;
  const logoKey = `${sym}|${name || ""}`;
  if (S.logoKey !== logoKey) { S.logoKey = logoKey; $("#iLogo").innerHTML = logoHtml(sym, name, 40); }
  $("#iName").textContent = S.day?.error || name || " ";
  const v = S.venues[sym], dual = !!(v && v.NSE && v.BSE);
  const ex = $("#iExch"), exLabel = exch || splitSym(sym)[1]; ex.hidden = !exLabel || dual; ex.textContent = exLabel || "";
  renderVenues();
  const px = $("#iPx"); px.textContent = price != null ? money(price, cur) : "—";
  px.classList.toggle("up", S.day?.lastDir > 0); px.classList.toggle("down", S.day?.lastDir < 0);
  const c = $("#iChg");
  const rc = S.range !== "1D" ? rangeChange() : null;
  if (rc) {
    c.textContent = `${signed(rc.chg)} (${pctTxt(rc.pct)}) ${RANGE_LABEL[S.range]}`;
    c.className = "big-chg num " + (rc.chg >= 0 ? "up" : "down");
  } else {
    c.textContent = chg != null ? `${signed(chg)} (${pctTxt(chgPct)}) today` : "";
    c.className = "big-chg num " + (chg == null ? "" : chg >= 0 ? "up" : "down");
  }
  renderState();
}
function renderState() {
  const st = $("#iState"), day = S.day;
  if (!day) { st.hidden = true; $("#iAsof").innerHTML = "&nbsp;"; return; }
  st.hidden = false;
  let cls = "chip", html = "Connecting";
  if (day.error) html = "Unavailable";
  else if (day.market === "closed") html = "Market closed";
  else if (day.source === "stream") { cls += " live"; html = "<i></i>Live"; }
  else if (day.source === "poll" || day.stream === "reconnecting") { cls += " delayed"; html = "Delayed"; }
  else if (day.stream === "connected") { cls += " live"; html = "<i></i>Live"; }
  else if (day.snap) html = "Snapshot";
  st.className = cls; st.innerHTML = html;
  const since = day.lastRecv ? (performance.now() - day.lastRecv) / 1000 : null;
  $("#iAsof").textContent = day.tradeTime
    ? `Last trade ${timeTxt(day.tradeTime)} ${tzName}` + (since != null && day.market !== "closed" ? ` · updated ${since < 60 ? since.toFixed(1) + "s" : Math.round(since / 60) + "m"} ago` : "")
    : " ";
}

// ---------------------------------------------------------- intraday stream (active symbol)
function startDayStream(sym) {
  if (dayES) dayES.close();
  clearInterval(ageTimer);
  const day = S.day = { sym, points: [], price: null, change: null, changePct: null, lastDir: 0, prevClose: null, market: "", stream: "connecting",
                        source: "", tape: [], lastRecv: 0, tradeTime: null, snap: null, dayHigh: null, dayLow: null, volume: null, error: null };
  dayES = new EventSource(`/live/${encodeURIComponent(sym)}/stream`);
  dayES.addEventListener("snapshot", (e) => {
    if (S.day !== day) return;
    const d = JSON.parse(e.data);
    day.snap = d; day.prevClose = d.previous_close; day.market = d.market_state; day.source = d.source;
    if (d.name) app.names.set(sym, d.name);
    day.sessionStart = d.session_start ? Date.parse(d.session_start) : null; day.sessionEnd = d.session_end ? Date.parse(d.session_end) : null;
    const pts = d.points.map((p) => ({ t: Date.parse(p.t), p: p.price }));
    const lastSnap = pts.length ? pts[pts.length - 1].t : 0;
    day.points = d.source === "poll" ? pts : pts.concat(day.points.filter((p) => p.t > lastSnap));
    day.dayHigh = d.day_high; day.dayLow = d.day_low; day.volume = d.volume; day.tradeTime = Date.parse(d.market_time);
    setDayPrice(day, d.price, d.change, d.change_pct, d.source === "poll" ? d.market_time : null);
    day.lastRecv = performance.now();
    renderHeader(); renderStats(); renderTape(); drawChart();
  });
  dayES.addEventListener("tick", (e) => {
    if (S.day !== day) return;
    const d = JSON.parse(e.data);
    day.source = "stream"; day.tradeTime = Date.parse(d.time);
    if (d.session === "regular") day.market = "open";
    if (d.day_high != null) day.dayHigh = d.day_high;
    if (d.day_low != null) day.dayLow = d.day_low;
    if (d.day_volume != null) day.volume = d.day_volume;
    day.points.push({ t: Date.parse(d.time), p: d.price });
    if (day.points.length > 8000) day.points.splice(0, day.points.length - 8000);
    setDayPrice(day, d.price, d.change, d.change_percent, d.time);
    day.lastRecv = performance.now();
    renderHeader(); renderStats(); scheduleChart();
  });
  dayES.addEventListener("status", (e) => { if (S.day === day) { day.stream = JSON.parse(e.data).stream; renderState(); } });
  dayES.addEventListener("failure", (e) => {
    if (S.day !== day) return;
    const d = JSON.parse(e.data);
    if (d.fatal) { day.error = d.code === "UNKNOWN_SYMBOL" ? "Unknown symbol — search by company name instead" : d.message; dayES.close(); }
    renderHeader(); renderStats(); drawChart();
  });
  dayES.onerror = () => { if (S.day === day && !day.error) { day.stream = "reconnecting"; renderState(); } };
  ageTimer = setInterval(renderState, 500);
}
function setDayPrice(day, price, change, changePct, tradeIso) {
  const prev = day.price;
  day.price = price;
  if (change != null) { day.change = change; day.changePct = changePct; }
  else if (day.prevClose) { day.change = price - day.prevClose; day.changePct = day.change / day.prevClose * 100; }
  if (prev != null && price !== prev) {
    day.lastDir = price > prev ? 1 : -1;
    const el = $("#iPx"); el.classList.remove("flash-up", "flash-down"); void el.offsetWidth;
    el.classList.add(price > prev ? "flash-up" : "flash-down"); setTimeout(() => el.classList.remove("flash-up", "flash-down"), 600);
  }
  if (tradeIso) {
    day.tape.unshift({ t: Date.parse(tradeIso), p: price, dir: prev == null ? 0 : Math.sign(price - prev) });
    day.tape = day.tape.slice(0, 12);
    renderTape(true);
  }
}
function renderStats() {
  const day = S.day, d = day?.snap, cur = d?.currency, box = $("#stats");
  if (day?.error) { box.innerHTML = `<div class="muted" style="grid-column:1/-1">No quote available.</div>`; return; }
  if (!d) { box.innerHTML = Array.from({ length: 8 }, () => `<div class="stat"><div class="skeleton" style="height:12px;width:50%;margin-bottom:6px"></div><div class="skeleton" style="height:16px;width:70%"></div></div>`).join(""); return; }
  const item = (k, v) => `<div class="stat"><div class="k">${k}</div><div class="v num">${v}</div></div>`;
  const lo = day.dayLow, hi = day.dayHigh, pos = lo != null && hi != null && hi > lo ? (day.price - lo) / (hi - lo) * 100 : null;
  box.innerHTML = item("Open", money(d.open, cur)) + item("Prev. close", money(day.prevClose, cur)) + item("Day high", money(hi, cur)) + item("Day low", money(lo, cur)) +
    item("Volume", compact(day.volume)) + item("52W high", money(d.fifty_two_week_high, cur)) + item("52W low", money(d.fifty_two_week_low, cur)) +
    item("Session", day.market === "open" ? `<span class="up">Open</span>` : "Closed") +
    (pos != null ? `<div class="stat range"><div class="k">Day range</div><div class="range-bar"><i style="left:${Math.max(0, Math.min(100, pos))}%"></i></div>
      <div class="range-ends num"><span>${money(lo, cur)}</span><span>${money(hi, cur)}</span></div></div>` : "");
}
function renderTape(isNew) {
  const day = S.day, box = $("#tape");
  $("#tapeNote").textContent = day?.source === "stream" ? "streaming" : day?.market === "closed" ? "market closed" : "";
  if (!day || !day.tape.length) { box.innerHTML = `<div class="muted" style="display:block;border:0;padding:6px 0">${day?.market === "closed" ? "No trades while the market is closed." : "Waiting for trades…"}</div>`; return; }
  box.innerHTML = day.tape.map((x, i) => `<div class="${isNew && i === 0 ? "new" : ""}"><span class="muted">${timeTxt(x.t)}</span>
    <span class="${x.dir > 0 ? "up" : x.dir < 0 ? "down" : ""}">${fmt(x.p)}</span><span class="${x.dir > 0 ? "up" : x.dir < 0 ? "down" : "muted"}">${x.dir > 0 ? "▲" : x.dir < 0 ? "▼" : "·"}</span></div>`).join("");
}

// ============================================================ charts
const M = { l: 8, r: 72, t: 22, b: 26 };
let chartRaf = 0;
function scheduleChart() { if (!chartRaf) chartRaf = requestAnimationFrame(() => { chartRaf = 0; drawChart(); }); }
function drawChart() {
  const box = $("#chart"); if (!box || box.offsetParent === null) return;
  $("#overlays").hidden = S.range === "1D";
  if (S.range === "1D" && S.chartType === "line") draw1D(box); else drawBars(box);
}
function axisLabels(W, H, lo, hi, y, avoidY = []) {
  let g = "";
  for (let k = 0; k <= 5; k++) {
    const v = lo + (hi - lo) * k / 5, yy = y(v);
    g += `<line x1="${M.l}" x2="${W - M.r}" y1="${yy}" y2="${yy}" stroke="${css("--grid")}"/>`;
    if (!avoidY.some((a) => Math.abs(a - yy) < 16)) g += `<text x="${W - M.r + 8}" y="${yy + 4}" font-size="11" fill="${css("--axis")}">${fmt(v)}</text>`;
  }
  return g;
}
function priceTag(W, yy, v, color, ink = "#fff") {
  return `<rect x="${W - M.r + 2}" y="${yy - 10}" width="${M.r - 6}" height="20" rx="4" fill="${color}"/>` +
         `<text x="${W - M.r + 8}" y="${yy + 4}" font-size="11.5" font-weight="700" fill="${ink}">${fmt(v)}</text>`;
}
function directional(xy, w) {
  if (xy.length < 2) return "";
  const UP = css("--up"), DN = css("--down"), FL = css("--faint");
  let out = "", dir = 0, d = `M${xy[0][0].toFixed(1)},${xy[0][1].toFixed(1)}`;
  const flush = () => { if (d.includes("L")) out += `<path d="${d}" fill="none" stroke="${dir > 0 ? UP : dir < 0 ? DN : FL}" stroke-width="${w}" stroke-linejoin="round" stroke-linecap="round"/>`; };
  for (let i = 1; i < xy.length; i++) {
    const step = Math.sign(xy[i - 1][1] - xy[i][1]) || dir; // smaller y = higher price
    if (step !== dir && d.includes("L")) { flush(); d = `M${xy[i - 1][0].toFixed(1)},${xy[i - 1][1].toFixed(1)}`; }
    dir = step; d += `L${xy[i][0].toFixed(1)},${xy[i][1].toFixed(1)}`;
  }
  flush(); return out;
}
function crosshair(svg, W, H, xs, onMove, onLeave) {
  const NS = "http://www.w3.org/2000/svg";
  const g = document.createElementNS(NS, "g"); g.setAttribute("pointer-events", "none"); g.style.display = "none"; svg.appendChild(g);
  const hit = document.createElementNS(NS, "rect");
  Object.entries({ x: M.l, y: 0, width: Math.max(0, W - M.l - M.r), height: Math.max(0, H - M.b), fill: "transparent" }).forEach(([k, v]) => hit.setAttribute(k, v));
  svg.appendChild(hit);
  hit.addEventListener("mousemove", (e) => {
    const mx = e.clientX - svg.getBoundingClientRect().left;
    let lo = 0, hi = xs.length - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (xs[mid] < mx) lo = mid; else hi = mid; }
    const i = Math.abs(xs[lo] - mx) <= Math.abs(xs[hi] - mx) ? lo : hi;
    g.style.display = ""; g.innerHTML = onMove(i);
  });
  hit.addEventListener("mouseleave", () => { g.style.display = "none"; if (onLeave) onLeave(); });
}
function draw1D(box) {
  const day = S.day;
  if (!day || day.error || !day.points.length) {
    box.innerHTML = day?.error ? `<div class="empty">${esc(day.error)}</div>` : S.active ? `<div class="skeleton" style="width:100%;height:100%"></div>` : `<div class="empty">Select a symbol to load its chart.</div>`; return;
  }
  const W = box.clientWidth, H = box.clientHeight, pts = day.points, last = pts[pts.length - 1];
  let t0 = day.sessionStart && day.sessionStart <= pts[0].t ? day.sessionStart : pts[0].t;
  let t1 = day.sessionEnd && day.market === "open" && day.sessionEnd > last.t ? day.sessionEnd : last.t;
  if (t1 - t0 < 60000) { t0 = last.t - 30 * 60000; t1 = last.t + 60000; }
  const vals = pts.map((p) => p.p); if (day.prevClose) vals.push(day.prevClose);
  let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo || hi * 0.002) * 0.1; lo -= pad; hi += pad;
  const x = (t) => M.l + (t - t0) / (t1 - t0) * (W - M.l - M.r), y = (v) => M.t + (1 - (v - lo) / (hi - lo)) * (H - M.t - M.b);
  const UP = css("--up"), DN = css("--down"), upDay = day.prevClose == null || last.p >= day.prevClose;
  const tagCol = day.lastDir > 0 ? UP : day.lastDir < 0 ? DN : upDay ? UP : DN;
  let area = ""; pts.forEach((p, i) => (area += `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.p).toFixed(1)}`));
  area += `L${x(last.t).toFixed(1)},${H - M.b}L${x(pts[0].t).toFixed(1)},${H - M.b}Z`;
  let g = axisLabels(W, H, lo, hi, y, [y(last.p)]);
  for (let k = 0; k <= 4; k++) { const t = t0 + (t1 - t0) * k / 4; g += `<text x="${x(t)}" y="${H - 7}" font-size="11" fill="${css("--axis")}" text-anchor="${k === 0 ? "start" : k === 4 ? "end" : "middle"}">${timeTxt(t, false)}</text>`; }
  const pc = day.prevClose != null ? `<line x1="${M.l}" x2="${W - M.r}" y1="${y(day.prevClose)}" y2="${y(day.prevClose)}" stroke="${css("--axis")}" stroke-dasharray="3,4" opacity=".7"/>
    <text x="${M.l + 4}" y="${y(day.prevClose) - 5}" font-size="11" fill="${css("--axis")}">Prev. close ${fmt(day.prevClose)}</text>` : "";
  const gid = "area-" + (upDay ? "up" : "down");
  box.innerHTML = `<svg width="${W}" height="${H}"><defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${upDay ? UP : DN}" stop-opacity=".2"/><stop offset="1" stop-color="${upDay ? UP : DN}" stop-opacity="0"/></linearGradient></defs>
    ${g}${pc}<path d="${area}" fill="url(#${gid})"/>${directional(pts.map((p) => [x(p.t), y(p.p)]), 1.8)}
    <line x1="${M.l}" x2="${W - M.r}" y1="${y(last.p)}" y2="${y(last.p)}" stroke="${tagCol}" stroke-dasharray="2,3" opacity=".7"/>
    ${priceTag(W, y(last.p), last.p, tagCol)}
    ${day.market === "open" ? `<circle cx="${x(last.t)}" cy="${y(last.p)}" r="9" fill="${tagCol}" opacity=".18"><animate attributeName="r" values="4;11;4" dur="1.6s" repeatCount="indefinite"/></circle>` : ""}
    <circle cx="${x(last.t)}" cy="${y(last.p)}" r="3.5" fill="${tagCol}"/></svg>`;
  crosshair(box.querySelector("svg"), W, H, pts.map((p) => x(p.t)), (i) => {
    const p = pts[i], cx = x(p.t), cy = y(p.p), col = css("--muted"), bx = Math.min(Math.max(cx - 36, M.l), W - M.r - 72);
    return `<line x1="${cx}" x2="${cx}" y1="${M.t}" y2="${H - M.b}" stroke="${col}" stroke-dasharray="3,3"/><line x1="${M.l}" x2="${W - M.r}" y1="${cy}" y2="${cy}" stroke="${col}" stroke-dasharray="3,3"/>
      <circle cx="${cx}" cy="${cy}" r="4" fill="${css("--panel")}" stroke="${css("--text")}" stroke-width="1.5"/>${priceTag(W, cy, p.p, css("--border-2"), css("--text"))}
      <rect x="${bx}" y="${H - M.b + 3}" width="72" height="18" rx="4" fill="${css("--border-2")}"/>
      <text x="${bx + 36}" y="${H - M.b + 16}" font-size="11" text-anchor="middle" fill="${css("--text")}">${timeTxt(p.t)}</text>`;
  });
}
// ---------------------------------------------------------- range charts: 1D candles, 1W ... All (line or candles)
const RANGE_LABEL = { "1D": "today", "1W": "1W", "1M": "1M", "3M": "3M", "6M": "6M", "1Y": "1Y", "5Y": "5Y", ALL: "all time" };
const BAR_MS = { "5m": 3e5, "15m": 9e5, "60m": 36e5, "1h": 36e5 };
const chartKey = (sym, r) => `${sym}|${r}`;
const currentChart = () => S.charts[chartKey(S.active, S.range)];

const RANGES = ["1D", "1W", "1M", "3M", "6M", "1Y", "5Y", "ALL"];
const chartTtl = (range) => (["1D", "1W", "1M"].includes(range) ? 60e3 : 600e3);
const fresh = (entry, range) => entry && (entry.state === "loading" || (entry.state === "ready" && Date.now() - entry.at < chartTtl(range)));

async function fetchChart(sym, range, tries = 2) {
  const key = chartKey(sym, range), have = S.charts[key];
  S.charts[key] = { ...(have || {}), state: "loading", at: have?.at ?? 0 };
  for (let i = 0; i < tries; i++) {
    try {
      const data = await api(`/chart/${encodeURIComponent(sym)}?range=${range}`);
      delete S.charts[key];  // re-insert: newest last, for the size cap below
      S.charts[key] = { state: "ready", data, at: Date.now() };
      if (data.name) app.names.set(sym, data.name);
      const keys = Object.keys(S.charts);
      keys.slice(0, Math.max(0, keys.length - 80)).forEach((k) => delete S.charts[k]);
      return true;
    } catch (err) {
      if (/404|422|Unknown symbol/i.test(err.message) || i === tries - 1) {
        S.charts[key] = { state: "error", error: err.message, data: have?.data, at: Date.now() };
        return false;
      }
      await new Promise((r) => setTimeout(r, 600));  // a stalled request: try once more right away
    }
  }
  return false;
}
async function loadChart(force = false) {
  const sym = S.active, range = S.range; if (!sym) return;
  if (!force && fresh(S.charts[chartKey(sym, range)], range)) return;
  const ok = await fetchChart(sym, range);
  if (sym === S.active && range === S.range) { drawChart(); renderHeader(); }
  if (ok) prefetchRanges(sym);
}
/** After the first chart, quietly load the stock's other ranges so switching is instant. */
let prefetching = "";
async function prefetchRanges(sym) {
  if (prefetching === sym) return;
  prefetching = sym;
  for (const r of RANGES) {
    if (S.active !== sym) break;
    if (r === "1D" && S.chartType === "line") continue;  // the live 1D line has its own stream
    if (!fresh(S.charts[chartKey(sym, r)], r)) await fetchChart(sym, r, 1);
  }
  if (prefetching === sym) prefetching = "";
}

/** The fetched bars with live trades folded in: ticks extend or start intraday bars, and move the last daily close. */
function liveBars(data) {
  const bars = data.bars.map((b) => ({ ...b, t: Date.parse(b.t) }));
  const day = S.day, n = bars.length;
  if (!n || !day || day.sym !== S.active || day.price == null) return bars;
  let cur = bars[n - 1] = { ...bars[n - 1] };
  const size = BAR_MS[data.interval];
  if (size) {
    for (const p of day.points) {
      if (p.t < cur.t) continue;
      if (p.t >= cur.t + size) { cur = { t: cur.t + Math.floor((p.t - cur.t) / size) * size, o: p.p, h: p.p, l: p.p, c: p.p }; bars.push(cur); }
      else { cur.c = p.p; cur.h = Math.max(cur.h, p.p); cur.l = Math.min(cur.l, p.p); }
    }
  } else if (day.tradeTime && day.tradeTime >= cur.t) {
    cur.c = day.price; cur.h = Math.max(cur.h, day.price); cur.l = Math.min(cur.l, day.price);
  }
  return bars;
}
function rangeChange() {
  const entry = currentChart(); if (!entry?.data) return null;
  const bars = liveBars(entry.data), base = entry.data.previous_close ?? bars[0]?.o;
  if (!bars.length || !base) return null;
  const last = bars[bars.length - 1].c;
  return { chg: last - base, pct: (last / base - 1) * 100, base };
}
function tickTxt(t, data) {
  const d = new Date(t);
  if (S.range === "1D") return timeTxt(t, false);
  if (["1W", "1M", "3M", "6M"].includes(S.range)) return d.toLocaleDateString([], { day: "numeric", month: "short" });
  if (S.range === "1Y") return d.toLocaleDateString([], { month: "short", year: "2-digit" });
  return data.interval === "1d" ? d.toLocaleDateString([], { month: "short", year: "2-digit" }) : d.getFullYear();
}
function pointTxt(t, data) {
  const d = new Date(t);
  if (BAR_MS[data.interval]) return `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${timeTxt(t, false)}`;
  if (data.interval === "1wk") return `Week of ${d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })}`;
  if (data.interval === "1mo") return d.toLocaleDateString([], { month: "long", year: "numeric" });
  return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}
function drawBars(box) {
  const entry = currentChart();
  if (!entry || !entry.data) {
    if (S.day?.error) { box.innerHTML = `<div class="empty">${esc(S.day.error)}</div>`; return; }
    box.innerHTML = entry?.state === "error"
      ? `<div class="empty">${/404|Unknown symbol/i.test(entry.error || "") ? "No chart for this symbol." : "Couldn't load this chart."}
          <button class="btn sm" id="chartRetry" style="margin-left:8px">Retry</button></div>`
      : `<div class="skeleton" style="width:100%;height:100%"></div>`;
    $("#chartRetry")?.addEventListener("click", () => { loadChart(true); drawChart(); });
    if (!entry) loadChart();
    return;
  }
  const data = entry.data, pts = liveBars(data), n = pts.length, ov = S.overlays, candle = S.chartType === "candle";
  const showOv = S.range !== "1D", base = data.previous_close ?? pts[0].o, lastP = pts[n - 1];
  const W = box.clientWidth, H = box.clientHeight;
  const vals = [];
  pts.forEach((p) => {
    if (candle) vals.push(p.h, p.l); else vals.push(p.c);
    if (showOv && ov.bb && p.bb_upper != null) vals.push(p.bb_upper, p.bb_lower);
    if (showOv && ov.sma200 && p.sma200 != null) vals.push(p.sma200);
    if (showOv && ov.sma50 && p.sma50 != null) vals.push(p.sma50);
  });
  const baseLine = ["1D", "1W"].includes(S.range) && data.previous_close != null;
  if (baseLine) vals.push(data.previous_close);
  let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo || hi * 0.01) * 0.08;
  lo = lo >= 0 ? Math.max(0, lo - pad) : lo - pad; hi += pad;  // prices can't go below zero
  const step = (W - M.l - M.r) / n, x = (i) => M.l + step * (i + 0.5), y = (v) => M.t + (1 - (v - lo) / (hi - lo)) * (H - M.t - M.b);
  const UP = css("--up"), DN = css("--down"), upPeriod = lastP.c >= base, tone = upPeriod ? UP : DN;
  let g = axisLabels(W, H, lo, hi, y, [y(lastP.c)]);
  for (let k = 0; k < 5; k++) {
    const i = Math.round((n - 1) * k / 4);
    g += `<text x="${x(i)}" y="${H - 7}" font-size="11" fill="${css("--axis")}" text-anchor="${k === 0 ? "start" : k === 4 ? "end" : "middle"}">${tickTxt(pts[i].t, data)}</text>`;
  }
  if (showOv && ov.bb) {
    const b = pts.map((p, i) => [i, p]).filter(([, p]) => p.bb_upper != null);
    if (b.length) g += `<path d="M${b.map(([i, p]) => `${x(i).toFixed(1)},${y(p.bb_upper).toFixed(1)}`).join("L")}L${b.slice().reverse().map(([i, p]) => `${x(i).toFixed(1)},${y(p.bb_lower).toFixed(1)}`).join("L")}Z" fill="${css("--brand-soft")}" stroke="${css("--faint")}" stroke-width=".6"/>`;
  }
  if (baseLine) {
    const by = y(data.previous_close);
    g += `<line x1="${M.l}" x2="${W - M.r}" y1="${by}" y2="${by}" stroke="${css("--axis")}" stroke-dasharray="3,4" opacity=".7"/>
      <text x="${M.l + 4}" y="${by - 5}" font-size="11" fill="${css("--axis")}">${S.range === "1D" ? "Prev. close" : "Close before"} ${fmt(data.previous_close)}</text>`;
  }
  if (candle) {
    const bw = Math.max(1, Math.min(14, step * 0.64));
    pts.forEach((p, i) => {
      const col = p.c >= p.o ? UP : DN, cx = x(i), top = y(Math.max(p.o, p.c)), bot = y(Math.min(p.o, p.c));
      g += `<line x1="${cx}" x2="${cx}" y1="${y(p.h)}" y2="${y(p.l)}" stroke="${col}" stroke-width="1"/><rect x="${cx - bw / 2}" y="${top}" width="${bw}" height="${Math.max(1, bot - top)}" fill="${col}"/>`;
    });
  } else {
    let d = ""; pts.forEach((p, i) => (d += `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.c).toFixed(1)}`));
    const gid = "rg-" + (upPeriod ? "up" : "down");
    g += `<defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${tone}" stop-opacity=".22"/><stop offset="1" stop-color="${tone}" stop-opacity="0"/></linearGradient></defs>
      <path d="${d}L${x(n - 1).toFixed(1)},${H - M.b}L${x(0).toFixed(1)},${H - M.b}Z" fill="url(#${gid})"/>
      <path d="${d}" fill="none" stroke="${tone}" stroke-width="1.8" stroke-linejoin="round"/>`;
  }
  const line = (k, c) => { let d = "", on = false; pts.forEach((p, i) => { if (p[k] == null) { on = false; return; } d += `${on ? "L" : "M"}${x(i).toFixed(1)},${y(p[k]).toFixed(1)}`; on = true; }); return d ? `<path d="${d}" fill="none" stroke="${c}" stroke-width="1.3" opacity=".9"/>` : ""; };
  if (showOv && ov.sma20) g += line("sma20", "#e9a23b");
  if (showOv && ov.sma50) g += line("sma50", "#4ea1ff");
  if (showOv && ov.sma200) g += line("sma200", "#c27bff");
  g += priceTag(W, y(lastP.c), lastP.c, candle ? (lastP.c >= lastP.o ? UP : DN) : tone);
  const pct = (v) => pctTxt((v / base - 1) * 100);
  const legend = (p) => candle
    ? `<b>${pointTxt(p.t, data)}</b>O <span class="num">${fmt(p.o)}</span> · H <span class="num">${fmt(p.h)}</span> · L <span class="num">${fmt(p.l)}</span> · C <span class="num ${p.c >= p.o ? "up" : "down"}">${fmt(p.c)}</span>`
    : `<b>${pointTxt(p.t, data)}</b><span class="num">${fmt(p.c)}</span> <span class="num ${p.c >= base ? "up" : "down"}">${pct(p.c)}</span> <span class="muted">vs ${S.range === "1D" ? "prev. close" : "start"}</span>`;
  box.innerHTML = `<div class="legend-ohlc" id="ohlc">${legend(lastP)}</div><svg width="${W}" height="${H}">${g}</svg>`;
  crosshair(box.querySelector("svg"), W, H, pts.map((_, i) => x(i)), (i) => {
    $("#ohlc").innerHTML = legend(pts[i]);
    const cx = x(i), cy = y(pts[i].c);
    return `<line x1="${cx}" x2="${cx}" y1="${M.t}" y2="${H - M.b}" stroke="${css("--muted")}" stroke-dasharray="3,3"/>
      ${candle ? "" : `<circle cx="${cx}" cy="${cy}" r="4" fill="${css("--panel")}" stroke="${tone}" stroke-width="1.6"/>`}${priceTag(W, cy, pts[i].c, css("--border-2"), css("--text"))}`;
  }, () => ($("#ohlc").innerHTML = legend(lastP)));
}
function renderChartType() {
  const b = $("#chartType"), on = S.chartType === "candle";
  b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on));
  b.title = on ? "Show line" : "Show candles"; b.setAttribute("aria-label", b.title);
}
// ---------------------------------------------------------- NSE / BSE switch
async function loadVenues(sym) {
  if (!/\.(NS|BO)$/.test(sym) || S.venues[sym]) { renderVenues(); return; }
  try { S.venues[sym] = await api(`/stocks/venues/${encodeURIComponent(sym)}`); } catch { S.venues[sym] = { NSE: null, BSE: null }; }
  if (sym === S.active) renderHeader();
}
function renderVenues() {
  const box = $("#iVenue"), v = S.venues[S.active];
  if (!v || !v.NSE || !v.BSE) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = ["NSE", "BSE"].map((x) => `<button type="button" data-sym="${esc(v[x])}" class="${v[x] === S.active ? "on" : ""}"
    title="${x === "BSE" && v.bse_code ? `BSE code ${esc(v.bse_code)}` : `Price on ${x}`}">${x}</button>`).join("");
  box.onclick = (e) => { const b = e.target.closest("button"); if (b && b.dataset.sym !== S.active) navigate("markets", "", { symbol: b.dataset.sym }); };
}

// ---------------------------------------------------------- about the company
async function loadCompany(sym) {
  const had = S.companies[sym];
  if (had && (had.state !== "error" || had.status === 404)) { renderAbout(); return; }  // other failures retry on revisit
  S.companies[sym] = { state: "loading" }; renderAbout();
  try { S.companies[sym] = { state: "ready", data: await api(`/company/${encodeURIComponent(sym)}`) }; }
  catch (err) { S.companies[sym] = { state: "error", error: err.message, status: err.status }; }
  const keys = Object.keys(S.companies); keys.slice(0, Math.max(0, keys.length - 60)).forEach((k) => delete S.companies[k]);
  if (sym === S.active) renderAbout();
}
function bigMoney(v, cur) {
  if (v == null) return null;
  if (cur === "INR") return `₹${Math.round(v / 1e7).toLocaleString("en-IN")} Cr`;
  const sym = { USD: "$", EUR: "€", GBP: "£", JPY: "¥" }[cur] ?? "";
  const [d, u] = v >= 1e12 ? [1e12, "T"] : v >= 1e9 ? [1e9, "B"] : v >= 1e6 ? [1e6, "M"] : [1, ""];
  return `${sym}${(v / d).toFixed(2)}${u}${sym ? "" : " " + (cur || "")}`.trim();
}
function renderAbout() {
  const box = $("#about"), c = S.companies[S.active];
  if (!c || (c.state === "error" && c.status === 404)) { box.hidden = true; return; }  // no profile for this one
  if (c.state === "error") {
    box.hidden = false;
    box.innerHTML = `<div class="card-h"><span class="card-t">About</span></div><div class="muted">Couldn't load company details.
      <button class="btn sm" id="aboutRetry" style="margin-left:8px">Retry</button></div>`;
    $("#aboutRetry").onclick = () => { const sym = S.active; delete S.companies[sym]; loadCompany(sym); };
    return;
  }
  box.hidden = false;
  if (c.state === "loading") {
    box.innerHTML = `<div class="card-h"><span class="card-t">About</span></div>` +
      [92, 100, 96, 60].map((w) => `<div class="skeleton" style="height:12px;width:${w}%;margin:8px 0"></div>`).join("");
    return;
  }
  const d = c.data, host = d.website ? d.website.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "") : null;
  const facts = [
    ["Sector", d.sector], ["Industry", d.industry],
    ["Headquarters", [d.city, d.state, d.country].filter(Boolean).join(", ") || null],
    ["Employees", d.employees != null ? d.employees.toLocaleString(d.currency === "INR" ? "en-IN" : undefined) : null],
    ["Market cap", bigMoney(d.market_cap, d.currency)],
    ["Website", host ? `<a href="${esc(d.website)}" target="_blank" rel="noopener noreferrer">${esc(host)}</a>` : null, true],
  ].filter(([, v]) => v);
  const people = (d.officers || []).slice(0, 4);
  if (!d.description && !facts.length && !people.length) { box.hidden = true; return; }
  const long = (d.description || "").length > 360;
  box.innerHTML = `<div class="card-h"><span class="card-t">About ${esc(d.name || splitSym(S.active)[0])}</span>
      <a class="muted about-src" href="${esc(d.source_url || "#")}" target="_blank" rel="noopener noreferrer">Source: Yahoo Finance</a></div>
    ${d.description ? `<p class="about-text${long ? " clamp" : ""}" id="aboutText">${esc(d.description)}</p>
      ${long ? `<button class="link-btn" id="aboutMore" type="button">Read more</button>` : ""}` : ""}
    ${facts.length ? `<div class="about-facts">${facts.map(([k, v, html]) => `<div><div class="k">${k}</div><div class="v">${html ? v : esc(v)}</div></div>`).join("")}</div>` : ""}
    ${people.length ? `<div class="about-people"><div class="k">Key people</div>${people.map((o) =>
      `<div class="person"><span class="logo" style="--s:30px;--c:var(--panel-2)"><b style="color:var(--muted)">${esc(o.name.replace(/^(Mr|Ms|Mrs|Dr|Shri|Smt)\.?\s+/i, "").split(/\s+/).filter((w) => /^[A-Za-z]/.test(w)).slice(0, 2).map((w) => w[0]).join(""))}</b></span>
        <div><div class="pn">${esc(o.name)}</div><div class="pt">${esc(o.title || "")}</div></div></div>`).join("")}</div>` : ""}`;
  $("#aboutMore")?.addEventListener("click", (e) => {
    const t = $("#aboutText"), open = t.classList.toggle("clamp");
    e.target.textContent = open ? "Read more" : "Show less";
  });
}

function renderOverlayToggles() { [...$("#overlays").children].forEach((b) => b.classList.toggle("on", !!S.overlays[b.dataset.o])); }

// ============================================================ outlook
async function loadForecast() {
  const sym = S.active, h = S.horizon; if (!sym) return;
  S.fcState = "loading"; S.fcError = null; renderOutlook();
  try {
    const body = await api(`/forecast/${encodeURIComponent(sym)}?horizon_days=${h}`);
    if (sym !== S.active || h !== S.horizon) return;
    S.fc = body; S.fcState = "ready";
  } catch (err) {
    if (sym !== S.active) return;
    S.fc = null; S.fcState = "error"; S.fcError = err.message;
  }
  renderOutlook();
}
function renderOutlook() {
  const box = $("#tab-outlook");
  const hSeg = `<div class="seg" id="hSeg">${[1, 5, 10, 20].map((h) => `<button data-h="${h}" class="${h === S.horizon ? "on" : ""}">${h}D</button>`).join("")}</div>`;
  const head = `<div class="card-h" style="margin-bottom:14px"><div><div class="card-t">Directional outlook</div><div class="muted" style="font-size:12.5px">Statistical model on 2 years of daily prices · back-tested on every request</div></div>${hSeg}</div>`;
  if (!S.active) { box.innerHTML = head; return; }
  if (S.fcState !== "ready") {
    box.innerHTML = head + (S.fcState === "error" ? `<div class="card empty" style="height:140px">${esc(S.fcError)}</div>`
      : `<div class="tiles">${"<div class='tile'><div class='skeleton' style='height:70px'></div></div>".repeat(4)}</div>`);
    wireHorizon(); return;
  }
  const fc = S.fc.forecast;
  if (!fc) { box.innerHTML = head + `<div class="card empty" style="height:120px">No outlook: ${esc(S.fc.reason)}</div>`; wireHorizon(); return; }
  const dirCls = fc.direction === "up" ? "dir-up" : fc.direction === "down" ? "dir-down" : "dir-flat";
  const dirTxt = { up: "Up", down: "Down", no_clear_edge: "No clear edge" }[fc.direction];
  const bt = fc.backtest, cur = fc.currency;
  let html = head + `<div class="tiles">
    <div class="tile ${dirCls}"><div class="k">Next ${fc.horizon_days} trading day${fc.horizon_days > 1 ? "s" : ""}</div><div class="v">${fc.direction === "up" ? "▲ " : fc.direction === "down" ? "▼ " : ""}${dirTxt}</div><div class="d">as of close ${esc(fc.as_of)}</div></div>
    <div class="tile"><div class="k">Probability of a higher close</div><div class="v num">${(fc.probability_up * 100).toFixed(1)}%</div>
      <div class="meter"><i style="width:${fc.probability_up * 100}%"></i><b></b></div></div>
    <div class="tile"><div class="k">Model confidence</div><div class="v" style="color:${fc.confidence === "moderate" ? "var(--up)" : "var(--warn)"};text-transform:capitalize">${esc(fc.confidence)}</div><div class="d">${esc(fc.confidence_reason)}</div></div>
    <div class="tile"><div class="k">Expected range (68%)</div><div class="v num" style="font-size:19px">${money(fc.expected_low, cur)} – ${money(fc.expected_high, cur)}</div><div class="d num">±${fc.sigma_pct.toFixed(2)}% from ${money(fc.last_close, cur)}</div></div>
  </div>`;
  if (bt) {
    const rows = [["Kairo model", bt.models[0], false], ["Indicator consensus", bt.models[1], false], ["Naive baseline", { hit_rate: bt.baseline_hit_rate }, true]];
    html += `<div class="card" style="margin-top:14px"><div class="card-h"><span class="card-t">Track record on ${esc(splitSym(S.active)[0])}</span><span class="muted" style="font-size:12px">${bt.models[0].predictions} out-of-sample calls · ${esc(bt.first_test_date)} → ${esc(bt.last_test_date)}</span></div>
      ${rows.map(([name, m, base]) => `<div class="bt"><span>${name}</span><div class="track"><div class="fill ${base ? "base" : ""}" style="width:${(m.hit_rate || 0) * 100}%"></div><span class="mid"></span></div>
        <span class="num"><b>${m.hit_rate != null ? (m.hit_rate * 100).toFixed(1) + "%" : "—"}</b>${m.ci_low != null ? ` <span class="muted" style="font-size:11.5px">${(m.ci_low * 100).toFixed(0)}–${(m.ci_high * 100).toFixed(0)}%</span>` : ""}</span></div>`).join("")}
      <div class="verdict ${bt.significant ? "yes" : "no"}">${bt.significant
        ? `The model has beaten the baseline by ${(bt.edge_vs_baseline * 100).toFixed(1)} points with statistical significance on this stock.`
        : `No demonstrated edge on this stock: the model has not reliably beaten a naive guess, so treat this outlook as close to a coin flip.`}</div></div>`;
  }
  html += `<div class="card"><div class="card-h"><span class="card-t">Technical readings</span><span class="muted" style="font-size:12px">Signal = textbook reading · Weight = learned from this stock's history</span></div>
    <div class="table-wrap"><table><tr><th>Indicator</th><th>Value</th><th>Reading</th><th>Signal</th><th>Weight</th><th>Hit rate</th></tr>
    ${fc.indicators.map((r) => `<tr><td><div style="font-weight:600">${esc(r.name)}</div><div class="cat">${esc(r.category)}</div></td><td class="num">${esc(r.value)}</td><td class="muted">${esc(r.reading)}</td>
      <td>${voteBar(r.vote)}</td><td>${r.weight == null ? "—" : voteBar(r.weight)}</td><td class="num">${r.hit_rate == null ? "—" : `${(r.hit_rate * 100).toFixed(0)}% <span class="muted">n=${r.samples}</span>`}</td></tr>`).join("")}
    </table></div><div class="fine">${esc(fc.disclaimer)}</div></div>`;
  box.innerHTML = html; wireHorizon();
}
function voteBar(v) { const w = Math.min(Math.abs(v), 1) * 48; return `<div class="vote" title="${v.toFixed(2)}"><i style="left:${v >= 0 ? 48 : 48 - w}px;width:${w}px;background:${v >= 0 ? "var(--up)" : "var(--down)"}"></i><b></b></div>`; }
function wireHorizon() { const seg = $("#hSeg"); if (seg) seg.onclick = (e) => { const b = e.target.closest("button"); if (!b) return; S.horizon = Number(b.dataset.h); loadForecast(); }; }

// ============================================================ research
const PROMPTS = ["How has the stock moved this month and what is in the news?", "Will it go up or down next week?", "What are the main risks right now?", "Is the stock overbought or oversold?"];
async function runResearch() {
  const sym = S.active, question = $("#askQ").value.trim(); if (!sym || question.length < 3) return;
  S.research[sym] = { state: "loading", question };
  renderResearch(); $("#askBtn").disabled = true; $("#askBtn").textContent = "Generating…";
  try {
    const body = await api("/analyze?include_trace=false", { method: "POST", timeout: 180000, body: { symbol: sym, question, period: $("#askPeriod").value, horizon_days: S.horizon } })
      .catch((err) => { if (err.status === 404 || err.status === 500) return err.body; throw err; });
    S.research[sym] = body ? { state: "ready", data: body } : { state: "error", message: "No response" };
  } catch (err) { S.research[sym] = { state: "error", message: err.message }; }
  $("#askBtn").disabled = false; $("#askBtn").textContent = "Generate";
  if (sym === S.active) renderResearch();
}
function renderResearch() {
  const box = $("#research"), r = S.research[S.active];
  if (!r) { box.innerHTML = ""; return; }
  if (r.state === "loading") { box.innerHTML = `<div class="card" style="margin-top:14px"><div class="muted" style="margin-bottom:10px">Fetching data, checking freshness and writing a cited note for ${esc(S.active)}…</div>${"<div class='skeleton' style='height:14px;margin:8px 0'></div>".repeat(4)}</div>`; return; }
  if (r.state === "error") { box.innerHTML = `<div class="card" style="margin-top:14px;color:var(--down)">${esc(r.message)}</div>`; return; }
  const d = r.data; let h = "";
  if (d.freshness?.notice) h += `<div class="notice" style="margin-top:14px">${esc(d.freshness.notice)}</div>`;
  const shown = (d.flags || []).filter((f) => f.code !== "FORECAST_UNAVAILABLE");
  if (shown.length) h += `<div class="card" style="margin-top:14px"><div class="card-h"><span class="card-t">Data checks</span></div>${shown.map((f) => `<div class="flag"><span class="sev sev-${esc(f.severity)}">${esc(f.severity)}</span><span>${esc(f.message)}</span></div>`).join("")}</div>`;
  h += `<div class="card" style="margin-top:14px">`;
  if (d.answer) {
    const a = d.answer;
    h += `<div class="provenance">Machine-generated by ${esc(a.generated_by)} from the cited data below · ${new Date().toLocaleString()}</div>
      <div class="note-h">${esc(a.headline)}</div><ul class="claims">${a.claims.map((c) => `<li>${esc(c.text)}${c.citations.map((i) => `<span class="cite" data-id="${esc(i)}">${esc(i)}</span>`).join("")}</li>`).join("")}</ul>`;
    if (a.caveats?.length) h += `<div class="fine">${a.caveats.map(esc).join(" · ")}</div>`;
  } else h += `<div class="muted">No note could be written for this request — the data checks above explain why. Source data is still listed below.</div>`;
  h += `</div>`;
  const facts = [...(d.reported_facts || []), ...(d.computed_metrics || [])];
  if (facts.length) {
    h += `<div class="card"><details><summary>Source data · ${facts.length} facts from ${d.sources.length} sources</summary><div class="table-wrap" style="margin-top:10px"><table><tr><th>ID</th><th>Fact</th><th>Value</th><th>Type</th><th>Observed</th></tr>
      ${facts.map((f) => `<tr id="fact-${esc(f.id)}"><td class="num">${esc(f.id)}</td><td>${esc(f.label)}</td><td class="num">${esc(f.display)}</td><td class="muted">${f.kind === "reported" ? "Reported" : "Computed"}</td><td class="muted num">${esc(new Date(f.observed_at).toLocaleString())}</td></tr>`).join("")}
      </table></div><div class="fine">${d.sources.map((s) => esc(s.source_id)).join("<br>")}</div></details></div>`;
  }
  h += `<div class="fine">Status ${esc(d.status)} · ${(d.metrics.latency_ms / 1000).toFixed(1)}s · ${d.metrics.tool_calls} data calls · ${d.metrics.llm_calls} model calls · ref ${esc(d.request_id)}</div>`;
  box.innerHTML = h;
  box.querySelectorAll(".cite").forEach((el) => (el.onclick = () => {
    const det = box.querySelector("details"); if (det) det.open = true;
    const row = document.getElementById("fact-" + el.dataset.id); if (!row) return;
    box.querySelectorAll("tr.hl").forEach((x) => x.classList.remove("hl")); row.classList.add("hl"); row.scrollIntoView({ behavior: "smooth", block: "center" });
  }));
}

// ============================================================ init & routing
onTabSleep((awake) => {
  if (!awake) { if (dayES) { dayES.close(); dayES = null; } clearInterval(ageTimer); return; }
  if (S.active) startDayStream(S.active);  // fresh snapshot + stream for the stock on screen
});

export function initMarkets() {
  S.watch = store.get(`kairo-watch-${app.provider}`, app.provider === "mock"
    ? ["AAPL", "MSFT", "TSLA", "NVDA", "RWLK"]
    : ["^NSEI", "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "AAPL", "MSFT", "NVDA", "BTC-USD"]);
  new ResizeObserver(() => scheduleChart()).observe($("#chart"));
  $("#rangeSeg").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    S.range = b.dataset.r; [...$("#rangeSeg").children].forEach((x) => x.classList.toggle("on", x === b));
    if (S.range !== "1D" || S.chartType === "candle") loadChart();
    drawChart(); renderHeader();
  };
  $("#chartType").onclick = () => {
    S.chartType = S.chartType === "candle" ? "line" : "candle"; store.set("kairo-chart-type", S.chartType);
    renderChartType(); if (S.range === "1D" && S.chartType === "candle") loadChart();
    drawChart();
  };
  renderChartType();
  $("#overlays").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; S.overlays[b.dataset.o] = !S.overlays[b.dataset.o]; store.set("kairo-overlays", S.overlays); renderOverlayToggles(); drawChart(); };
  $("#mTabs").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    S.tab = b.dataset.tab;
    [...$("#mTabs").children].forEach((x) => x.classList.toggle("on", x === b));
    ["overview", "outlook", "research"].forEach((t) => ($(`#tab-${t}`).hidden = t !== S.tab));
    if (S.tab === "outlook" && S.fcState === "idle") loadForecast();
    if (S.tab === "overview") scheduleChart();
  };
  $("#prompts").innerHTML = PROMPTS.map((p) => `<button type="button">${esc(p)}</button>`).join("");
  $("#prompts").onclick = (e) => { const b = e.target.closest("button"); if (b) { $("#askQ").value = b.textContent; runResearch(); } };
  $("#askForm").onsubmit = (e) => { e.preventDefault(); runResearch(); };
  $("#modelTag").textContent = app.llm ? `${app.model} · every statement cites its data` : "Research notes need a GROQ_API_KEY";
  renderOverlayToggles(); renderStats(); renderTape(); renderOutlook();
  renderWatchlist(); startWatchStream();
}
export function showMarkets(params) {
  const sym = (params.get("symbol") || S.active || S.watch.find((s) => !s.startsWith("^")) || S.watch[0] || "AAPL").toUpperCase();
  selectSymbol(sym);
  const tab = params.get("tab");
  if (tab) $(`#mTabs button[data-tab="${tab}"]`)?.click();
  scheduleChart();
}
