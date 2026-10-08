// App shell: boot, routing between views, index strip, global search, Ctrl+K palette, theme.
import { $, $$, esc, api, store, app, onRoute, parseRoute, navigate, feed, fmt, pctTxt, tone, searchStocks, logoHtml, INDICES } from "./core.js";
import { initTerminal, showTerminal } from "./terminal.js";
import { initMarkets, showMarkets, knownNames, redraw } from "./markets.js";
import { initPortfolio, showPortfolio, hidePortfolio, openAddTrade, openImport } from "./portfolio.js";
import { initScreener, showScreener } from "./screener.js";
import { initNews, showNews } from "./news.js";
import { initAlerts, showAlerts, openAlertDialog } from "./alerts.js";
import { initExplore, showExplore, hideExplore } from "./explore.js";
import { initFunds, showFunds, searchFunds } from "./funds.js";

const VIEWS = ["explore", "markets", "terminal", "funds", "portfolio", "screener", "news", "alerts"];

// ---------------------------------------------------------------- theme
function toggleTheme() {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = t; store.set("kairo-theme", t);
  $('meta[name="theme-color"]').content = t === "dark" ? "#0d1118" : "#ffffff";
  redraw();
}
$("#themeBtn").onclick = toggleTheme;

// ---------------------------------------------------------------- index strip
const stripState = new Map();
function renderStrip() {
  const strip = $("#strip");
  const items = INDICES.filter(([s]) => stripState.get(s)?.price != null);
  if (!items.length) { strip.innerHTML = ""; return; }
  const nse = stripState.get("^NSEI");
  const mk = nse ? `<span class="mk ${nse.market_state === "open" ? "open" : ""}"><i></i>NSE ${nse.market_state === "open" ? "OPEN" : "CLOSED"}</span>` : "";
  strip.innerHTML = mk + items.map(([s, label]) => {
    const q = stripState.get(s);
    return `<a href="#/markets?symbol=${encodeURIComponent(s)}" data-s="${esc(s)}">${esc(label)} <b class="num">${fmt(q.price, 2)}</b><span class="num ${tone(q.change_pct)}">${pctTxt(q.change_pct)}</span></a>`;
  }).join("");
}
function initStrip() {
  if (app.provider === "mock") return; // the mock dataset has no indices
  feed.require("strip", INDICES.map(([s]) => s));
  feed.on("quote", (d) => { if (INDICES.some(([s]) => s === d.symbol)) { stripState.set(d.symbol, { ...stripState.get(d.symbol), ...d }); renderStrip(); } });
  feed.on("tick", (d) => {
    const q = stripState.get(d.symbol); if (!q) return;
    const dir = Math.sign(d.price - q.price);
    q.price = d.price; if (d.change_percent != null) q.change_pct = d.change_percent;
    renderStrip();
    const el = $(`#strip a[data-s="${CSS.escape(d.symbol)}"]`);
    if (el && dir) { el.classList.add(dir > 0 ? "flash-up" : "flash-down"); setTimeout(() => el.classList.remove("flash-up", "flash-down"), 600); }
  });
}

// ---------------------------------------------------------------- search box
const qInput = $("#q"), sugg = $("#suggest");
let suggIdx = -1;
function symbolNames() { return new Map([...INDICES, ...app.names, ...knownNames()]); }
const remote = { q: "", items: [] };
let remoteTimer = 0;
function suggestions(text) {
  const t = text.trim().toUpperCase();
  // Yours first (watchlist, holdings, recent), then every listed stock from the official lists.
  const mine = [...symbolNames()].filter(([s, n]) => !t || s.includes(t) || String(n).toUpperCase().includes(t)).slice(0, t ? 3 : 8);
  const listed = remote.q === text.trim().toLowerCase() ? remote.items.map((h) => [h.symbol, `${h.name} · ${h.exchange}`]) : [];
  const list = [...new Map([...mine, ...listed])].slice(0, 8);
  // Only offer a raw ticker when it could be one: tickers contain a letter ("500325" is a BSE code, found above).
  if (t && /^\^?[A-Z0-9][A-Z0-9.\-=&]{0,15}$/.test(t) && /[A-Z]/.test(t.split(".")[0]) && !list.some(([s]) => s === t)) list.push([t, "Open as ticker"]);
  return list;
}
function showSuggest() {
  renderSuggest();
  const q = qInput.value.trim().toLowerCase();
  clearTimeout(remoteTimer);
  if (q.length >= 2 && remote.q !== q) remoteTimer = setTimeout(async () => {
    const items = await searchStocks(q);
    if (qInput.value.trim().toLowerCase() === q) { remote.q = q; remote.items = items; renderSuggest(); }
  }, 150);
}
function renderSuggest() {
  const list = suggestions(qInput.value);
  suggIdx = list.length ? 0 : -1;
  sugg.innerHTML = list.map(([s, n], i) => `<button type="button" data-s="${esc(s)}" class="${i === 0 ? "sel" : ""}">${logoHtml(s, n === "Open as ticker" ? "" : n.split(" · ")[0], 26)}<b>${esc(s)}</b><span class="muted">${esc(n)}</span></button>`).join("");
  sugg.style.display = list.length ? "block" : "none";
}
function pick(sym) { qInput.value = ""; qInput.blur(); sugg.style.display = "none"; navigate("markets", "", { symbol: sym.toUpperCase() }); }
qInput.addEventListener("focus", showSuggest);
qInput.addEventListener("input", showSuggest);
qInput.addEventListener("blur", () => setTimeout(() => (sugg.style.display = "none"), 150));
qInput.addEventListener("keydown", (e) => {
  const items = $$("button", sugg);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault(); if (!items.length) return;
    suggIdx = (suggIdx + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items.forEach((b, i) => b.classList.toggle("sel", i === suggIdx));
  } else if (e.key === "Enter") {
    const s = items[suggIdx]?.dataset.s || qInput.value.trim();
    if (s) pick(s);
  } else if (e.key === "Escape") qInput.blur();
});
sugg.addEventListener("mousedown", (e) => { const b = e.target.closest("button"); if (b) pick(b.dataset.s); });

// ---------------------------------------------------------------- command palette (Ctrl+K)
const PAGES = [["explore", "Explore", "Market movers, sectors, screens"], ["markets", "Stocks", "Live chart, outlook, research"],
  ["terminal", "Terminal", "Full-screen trading chart"], ["funds", "Mutual funds", "Search, top funds, calculators"], ["funds/compare", "Compare funds", "Side by side"],
  ["portfolio", "Portfolio", "Holdings, XIRR, P&L"], ["portfolio/tax", "Tax P&L", "Capital gains estimate"],
  ["portfolio/health", "Portfolio health check", "Diversification and risk"], ["screener", "Screener", "Scan a market"],
  ["news", "News", "Headlines for your stocks"], ["alerts", "Alerts", "Price alerts"]];
const ACTIONS = [["Add a trade", "Portfolio", () => openAddTrade()], ["Import trades from CSV", "Portfolio", () => openImport()],
  ["New price alert", "Alerts", () => openAlertDialog()], ["Toggle light / dark theme", "Appearance", toggleTheme]];
function openPalette() {
  if ($(".palette")) return;
  const root = document.createElement("div");
  root.className = "palette";
  root.innerHTML = `<div class="palette-box" role="dialog" aria-label="Command palette"><input placeholder="Search stocks, pages and actions…" aria-label="Command">
    <div class="palette-list"></div><div class="palette-foot"><span>↑↓ to move</span><span>Enter to open</span><span>Esc to close</span></div></div>`;
  document.body.appendChild(root);
  const input = root.querySelector("input"), list = root.querySelector(".palette-list");
  let items = [], sel = 0;
  const close = () => root.remove();
  function build() {
    const t = input.value.trim().toLowerCase();
    const match = (...xs) => !t || xs.some((x) => String(x).toLowerCase().includes(t));
    const mine = [...symbolNames()].filter(([s, n]) => match(s, n)).slice(0, t ? 3 : 5);
    const listed = stockHits.q === t ? stockHits.items.map((h) => [h.symbol, `${h.name} · ${h.exchange}`]) : [];
    const syms = [...new Map([...mine, ...listed])].slice(0, 8)
      .map(([s, n]) => ({ group: "Stocks & indices", label: s, hint: n, run: () => navigate("markets", "", { symbol: s }) }));
    const pages = PAGES.filter(([, l, h]) => match(l, h)).map(([r, l, h]) => ({ group: "Go to", label: l, hint: h, run: () => { const [v, sub] = r.split("/"); navigate(v, sub || ""); } }));
    const acts = ACTIONS.filter(([l, h]) => match(l, h)).map(([l, h, run]) => ({ group: "Actions", label: l, hint: h, run }));
    // A raw ticker guess ranks last, so "tax" opens Tax P&L rather than a symbol called TAX.
    const raw = t.toUpperCase();
    const guess = t && /^\^?[A-Z0-9][A-Z0-9.\-=&]{0,15}$/.test(raw) && /[A-Z]/.test(raw.split(".")[0]) && !syms.some((x) => x.label === raw)
      ? [{ group: "Open as symbol", label: raw, hint: "Look up this ticker", run: () => navigate("markets", "", { symbol: raw }) }] : [];
    // Typing the start of a page or action ("tax", "add") means you want that, not a ticker.
    const navFirst = t && [...pages, ...acts].some((x) => x.label.toLowerCase().startsWith(t));
    const fundItems = (fundHits.q === t ? fundHits.items : []).map((f) => ({ group: "Mutual funds", label: f.name, hint: `${f.sub_category || ""} · 3Y ${f.returns?.["3Y"] != null ? f.returns["3Y"].toFixed(1) + "%" : "—"}`, run: () => navigate("funds", f.code) }));
    items = navFirst ? [...pages, ...acts, ...syms, ...fundItems, ...guess] : [...syms, ...fundItems, ...pages, ...acts, ...guess];
    sel = Math.min(sel, Math.max(0, items.length - 1));
    let last = "";
    list.innerHTML = items.map((it, i) => { const g = it.group !== last ? `<div class="palette-group">${esc(it.group)}</div>` : ""; last = it.group;
      return `${g}<button class="palette-item ${i === sel ? "sel" : ""}" data-i="${i}"><span>${esc(it.label)}</span><span class="muted">${esc(it.hint || "")}</span></button>`; }).join("")
      || `<div class="palette-group">No matches</div>`;
    list.querySelector(".sel")?.scrollIntoView({ block: "nearest" });
  }
  const run = (i) => { const it = items[i]; if (!it) return; close(); it.run(); };
  const fundHits = { q: "", items: [] }, stockHits = { q: "", items: [] };
  let fundTimer = 0, stockTimer = 0;
  input.addEventListener("input", () => {
    sel = 0; build();
    const t = input.value.trim().toLowerCase();
    clearTimeout(fundTimer); clearTimeout(stockTimer);
    if (t.length >= 2) stockTimer = setTimeout(async () => { const items = await searchStocks(t); if (input.value.trim().toLowerCase() === t) { stockHits.q = t; stockHits.items = items; build(); } }, 150);
    if (t.length >= 3) fundTimer = setTimeout(async () => { const items = await searchFunds(t); if (input.value.trim().toLowerCase() === t) { fundHits.q = t; fundHits.items = items; build(); } }, 250);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); sel = (sel + (e.key === "ArrowDown" ? 1 : -1) + items.length) % Math.max(1, items.length); build(); }
    else if (e.key === "Enter") { e.preventDefault(); run(sel); }
    else if (e.key === "Escape") close();
  });
  list.addEventListener("mousedown", (e) => { const b = e.target.closest("[data-i]"); if (b) { e.preventDefault(); run(Number(b.dataset.i)); } });
  root.addEventListener("mousedown", (e) => { if (e.target === root) close(); });
  build(); input.focus();
}
document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
  else if (e.key === "/" && !typing && !document.querySelector(".scrim, .palette")) { e.preventDefault(); qInput.focus(); }
});
$(".search kbd").onclick = openPalette;

// ---------------------------------------------------------------- routing
let current = null;
function route(r) {
  const sym = r.params.get("symbol") || "";
  if (r.view === "markets" && /^MF\d{3,8}$/.test(sym)) { navigate("funds", sym.slice(2)); return; }  // funds have their own page
  const view = VIEWS.includes(r.view) ? r.view : "explore";
  VIEWS.forEach((v) => ($(`#view-${v}`).hidden = v !== view));
  $$("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.v === view));
  if (current === "portfolio" && view !== "portfolio") hidePortfolio();
  if (current === "explore" && view !== "explore") hideExplore();
  current = view;
  const titles = { explore: "Explore", funds: "Mutual funds", portfolio: "Portfolio", screener: "Screener", news: "News", alerts: "Alerts" };
  if (titles[view]) document.title = `${titles[view]} · Kairo Markets`;
  if (view === "explore") showExplore();
  else if (view === "markets") showMarkets(r.params);
  else if (view === "terminal") showTerminal(r.params);
  else if (view === "funds") showFunds(r.sub, r.params);
  else if (view === "portfolio") showPortfolio(r.sub);
  else if (view === "screener") showScreener();
  else if (view === "news") showNews(r.sub, r.params);
  else if (view === "alerts") showAlerts();
}
onRoute(route);

// ---------------------------------------------------------------- boot
(async function boot() {
  try {
    const h = await api("/health");
    app.provider = h.data_provider; app.llm = h.llm_configured; app.model = h.model;
  } catch { /* defaults */ }
  const legacy = new URLSearchParams(location.search).get("symbol");
  if (legacy && !location.hash) history.replaceState(null, "", "/" + `#/markets?symbol=${encodeURIComponent(legacy)}`);
  initMarkets(); initPortfolio(); initNews(); initAlerts(); initStrip(); initFunds(); initTerminal();
  await Promise.all([initScreener(), initExplore()]);
  route(parseRoute());
})();
