// Screener: scan a universe, filter with presets, sort any column.
import { $, esc, api, fmt, money, pctTxt, tone, symHtml, store, navigate, universeCatalog, universeOptions, validUniverse, logoHtml, lastSeen } from "./core.js";
import { getWatchlist } from "./markets.js";

const PRESETS = [
  ["all", "All", () => true],
  ["gainers", "Top gainers today", (r) => r.chg_1d > 0, "chg_1d"],
  ["losers", "Top losers today", (r) => r.chg_1d < 0, "chg_1d", true],
  ["momentum", "3-month momentum", (r) => r.chg_3m > 10 && r.above_sma50, "chg_3m"],
  ["high", "Near 52-week high", (r) => r.from_52w_high != null && r.from_52w_high > -5, "from_52w_high"],
  ["oversold", "Oversold (RSI < 30)", (r) => r.rsi14 != null && r.rsi14 < 30, "rsi14", true],
  ["overbought", "Overbought (RSI > 70)", (r) => r.rsi14 != null && r.rsi14 > 70, "rsi14"],
  ["golden", "Golden cross", (r) => r.golden_cross === true, "chg_1m"],
  ["downtrend", "Below 200-day average", (r) => r.above_sma200 === false, "from_52w_high", true],
];
const COLS = [
  ["symbol", "Instrument"], ["price", "Price", "r"], ["chg_1d", "1D", "r"], ["chg_1w", "1W", "r"], ["chg_1m", "1M", "r"], ["chg_3m", "3M", "r"],
  ["chg_1y", "1Y", "r"], ["rsi14", "RSI", "r"], ["trend", "Trend"], ["from_52w_high", "From 52W high", "r"], ["volatility", "Volatility", "r"],
];
const MINE = [["watchlist", "My watchlist"]];
const X = { universe: store.get("kairo-screen-universe", ""), universes: { default: "", items: [] }, job: null, preset: "all", sort: "chg_1d", asc: false, q: "", timer: 0 };

async function load(refresh = false) {
  clearTimeout(X.timer);
  const custom = X.universe === "watchlist";
  const syms = getWatchlist().filter((s) => !s.startsWith("^") && !s.endsWith("=X"));
  if (custom && !syms.length) { X.job = { status: "ready", rows: [], errors: {}, total: 0, done: 0 }; render(); return; }
  const url = custom ? `/screener?symbols=${encodeURIComponent(syms.join(","))}` : `/screener?universe=${X.universe}`;
  const memo = `screener:${custom ? "watchlist" : X.universe}`;
  if (!X.job && !refresh) { const last = lastSeen.get(memo); if (last) { X.job = { ...last, status: "refreshing", done: 0, total: last.total }; render(); } }
  try { X.job = await api(url + (refresh ? "&refresh=true" : "")); }
  catch (err) { if (!X.job?.rows?.length) X.job = { status: "error", error: err.message, rows: [] }; }
  render();
  if (X.job.status === "ready") lastSeen.set(memo, X.job);
  if (X.job.status === "running" || X.job.status === "refreshing") X.timer = setTimeout(() => load(), 1200);
}

function cell(r, key) {
  const v = r[key];
  if (key === "symbol") return `<td><div class="cell-id">${logoHtml(r.symbol, r.name, 30)}<div><div class="cell-sym">${symHtml(r.symbol)}</div><div class="cell-name">${esc(r.name || "")}</div></div></div></td>`;
  if (key === "price") return `<td class="r num">${money(r.price, r.currency)}</td>`;
  if (key.startsWith("chg_") || key === "from_52w_high") return `<td class="r num ${tone(v)}">${pctTxt(v, key === "chg_1d" ? 2 : 1)}</td>`;
  if (key === "rsi14") return `<td class="r num ${v > 70 ? "down" : v < 30 ? "up" : ""}">${v == null ? "—" : v.toFixed(0)}</td>`;
  if (key === "volatility") return `<td class="r num">${v == null ? "—" : v.toFixed(0) + "%"}</td>`;
  if (key === "trend") {
    const t = (on, l) => on == null ? "" : `<span class="pill ${on ? "up" : "down"}">${on ? "▲" : "▼"} ${l}</span>`;
    return `<td>${t(r.above_sma50, "50D")}${t(r.above_sma200, "200D")}${r.golden_cross ? `<span class="pill up">GC</span>` : ""}</td>`;
  }
  return `<td>${esc(v)}</td>`;
}

function render() {
  const sel = $("#scrUniverse");
  sel.innerHTML = universeOptions(X.universes, X.universe, MINE);
  $("#scrPresets").innerHTML = PRESETS.map(([k, l]) => `<button data-p="${k}" class="${k === X.preset ? "on" : ""}">${l}</button>`).join("");
  const box = $("#scrBody"), job = X.job;
  if (!job) { box.innerHTML = `<div class="card"><div class="skeleton" style="height:240px"></div></div>`; return; }
  if (job.status === "error") { box.innerHTML = `<div class="card" style="color:var(--down)">${esc(job.error)}</div>`; return; }
  const [, , test] = PRESETS.find((p) => p[0] === X.preset);
  const q = X.q.trim().toUpperCase();
  let rows = job.rows.filter(test).filter((r) => !q || r.symbol.includes(q) || (r.name || "").toUpperCase().includes(q));
  const key = X.sort;
  rows = rows.slice().sort((a, b) => {
    const va = key === "symbol" ? a.symbol : a[key], vb = key === "symbol" ? b.symbol : b[key];
    if (va == null) return 1; if (vb == null) return -1;
    return (va > vb ? 1 : va < vb ? -1 : 0) * (X.asc ? 1 : -1);
  });
  const running = job.status === "running", updating = job.status === "refreshing";
  const progress = running ? `<div class="progress"><i style="width:${job.total ? job.done / job.total * 100 : 0}%"></i></div><div class="muted" style="font-size:12px;margin-bottom:8px">Scanning ${job.done} of ${job.total}…</div>`
    : updating ? `<div class="muted" style="font-size:12px;margin-bottom:8px">Updating prices${job.total ? ` · ${job.done} of ${job.total}` : ""}…</div>` : "";
  const errs = Object.keys(job.errors || {});
  box.innerHTML = `<div class="card">${progress}
    <div class="card-h"><span class="card-t">${rows.length} of ${job.rows.length} match</span><span class="muted" style="font-size:12px">${job.computed_at ? `Daily closes · scanned ${new Date(job.computed_at).toLocaleTimeString()}` : ""}
      <button class="btn sm ghost" id="scrRefresh" ${running ? "disabled" : ""}>Rescan</button></span></div>
    <div class="table-wrap"><table id="scrTable"><tr>${COLS.map(([k, l, c]) => `<th class="${c || ""} ${k === "trend" ? "" : "sort"} ${X.sort === k ? "on" + (X.asc ? " asc" : "") : ""}" data-k="${k}">${l}</th>`).join("")}</tr>
    ${rows.map((r) => `<tr class="click" data-sym="${esc(r.symbol)}">${COLS.map(([k]) => cell(r, k)).join("")}</tr>`).join("") ||
      `<tr><td colspan="${COLS.length}" class="muted">${running ? "Results appear as the scan completes." : "No stocks match this filter."}</td></tr>`}</table></div>
    ${errs.length ? `<div class="fine">No data for ${errs.map(esc).join(", ")}.</div>` : ""}</div>
    <div class="fine">Computed from one year of daily prices. RSI above 70 / below 30 is conventionally read as overbought / oversold. GC = golden cross (50-day average above 200-day). Not investment advice.</div>`;
  $("#scrTable").onclick = (e) => {
    const th = e.target.closest("th.sort");
    if (th) { const k = th.dataset.k; X.asc = X.sort === k ? !X.asc : k === "symbol"; X.sort = k; render(); return; }
    const tr = e.target.closest("tr[data-sym]"); if (tr) navigate("markets", "", { symbol: tr.dataset.sym });
  };
  $("#scrRefresh").onclick = () => load(true);
}

export async function initScreener() {
  X.universes = await universeCatalog();
  X.universe = validUniverse(X.universes, X.universe, MINE);
  $("#scrUniverse").onchange = (e) => { X.universe = e.target.value; store.set("kairo-screen-universe", X.universe); X.job = null; render(); load(); };
  $("#scrPresets").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const p = PRESETS.find((x) => x[0] === b.dataset.p); X.preset = p[0];
    if (p[3]) { X.sort = p[3]; X.asc = !!p[4]; }
    render();
  };
  $("#scrSearch").oninput = (e) => { X.q = e.target.value; render(); };
}
export async function showScreener() {
  render(); load();
  X.universes = await universeCatalog();  // counts fill in once the lists have downloaded
  $("#scrUniverse").innerHTML = universeOptions(X.universes, X.universe, MINE);
}
