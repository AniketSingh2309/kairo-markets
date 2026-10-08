// Explore: market movers, sector heatmap, trading screens with track records, news, your investments.
import { $, esc, api, store, fmt, money, signedMoney, pctTxt, tone, ago, compact, symHtml, splitSym, sparkline, navigate, feed,
  universeCatalog, universeOptions, validUniverse, logoHtml, lastSeen, articleHref } from "./core.js";
import { getWatchlist } from "./markets.js";
import { ensurePortfolio, portfolioSummary } from "./portfolio.js";

const TABS = [
  ["gainers", "Gainers"], ["losers", "Losers"], ["volume", "Volume shockers"], ["high", "Near 52W high"], ["low", "Near 52W low"],
];
const E = {
  universe: store.get("kairo-explore-universe", ""), universes: { default: "", items: [] }, tab: "gainers", sector: null,
  movers: null, screens: null, openScreen: null, showAll: false, news: null, timers: [],
};

// ------------------------------------------------------------------ data
async function loadMovers(refresh = false) {
  const u = E.universe;
  if (!E.movers) { const last = lastSeen.get(`movers:${u}`); if (last) { E.movers = { ...last, status: "refreshing" }; renderMovers(); renderSectors(); renderBreadth(); } }
  try {
    const body = await api(`/explore/movers?universe=${u}${refresh ? "&refresh=true" : ""}`);
    if (u !== E.universe) return;
    if (body.result || !E.movers?.result) E.movers = body;  // keep the remembered list until real data arrives
    else E.movers = { ...E.movers, progress: body.progress };
    if (body.status === "ready" && body.result) lastSeen.set(`movers:${u}`, body);
  } catch (err) { if (!E.movers?.result) E.movers = { status: "error", error: err.message }; }
  renderMovers(); renderSectors(); renderBreadth();
  if (E.movers.status === "running" || E.movers.status === "refreshing") schedule(() => loadMovers(), 1500);
  else if (!E.newsLoaded) loadNews();
}
async function loadScreens() {
  const u = E.universe;
  if (!E.screens) { const last = lastSeen.get(`screens:${u}`); if (last) { E.screens = { ...last, status: "refreshing" }; renderScreens(); } }
  try {
    const body = await api(`/explore/screens?universe=${u}&horizon=10`);
    if (u !== E.universe) return;
    if (body.result || !E.screens?.result) E.screens = body;
    if (body.status === "ready" && body.result) lastSeen.set(`screens:${u}`, body);
  } catch (err) { if (!E.screens?.result) E.screens = { status: "error", error: err.message }; }
  renderScreens();
  if (E.screens.status === "running" || E.screens.status === "refreshing") schedule(loadScreens, 2000);
}
function schedule(fn, ms) { E.timers.push(setTimeout(() => { if (!$("#view-explore").hidden) fn(); }, ms)); }
function clearTimers() { E.timers.forEach(clearTimeout); E.timers = []; }
const rows = () => E.movers?.result ?? [];

// ------------------------------------------------------------------ movers
function moverList() {
  let list = rows().filter((r) => !E.sector || (r.sector || "Other") === E.sector);
  const by = (k, dir = -1) => (a, b) => ((a[k] ?? -Infinity) - (b[k] ?? -Infinity)) * dir;
  if (E.tab === "gainers") list = list.filter((r) => r.change_pct > 0).sort(by("change_pct"));
  if (E.tab === "losers") list = list.filter((r) => r.change_pct < 0).sort(by("change_pct", 1));
  if (E.tab === "volume") list = list.filter((r) => r.volume_ratio != null && r.volume_ratio >= 1.2).sort(by("volume_ratio"));
  if (E.tab === "high") list = list.filter((r) => r.from_52w_high != null && r.from_52w_high >= -3).sort(by("from_52w_high"));
  if (E.tab === "low") list = list.filter((r) => r.from_52w_low != null && r.from_52w_low <= 5).sort(by("from_52w_low", 1));
  return list;
}
function renderMovers() {
  const box = $("#exMovers"), m = E.movers;
  const tabs = `<div class="chips">${TABS.map(([k, l]) => `<button data-t="${k}" class="${k === E.tab ? "on" : ""}">${l}</button>`).join("")}</div>`;
  const head = `<div class="card-h"><div><div class="card-t">Market movers</div><div class="muted" style="font-size:12px">${esc(m?.label || "")}${m?.computed_at ? ` · updated ${esc(ago(m.computed_at))}` : ""}${E.sector ? ` · <b>${esc(E.sector)}</b> <a href="#" id="clearSector">clear</a>` : ""}</div></div>${tabs}</div>`;
  if (!m || (m.status === "running" && !m.result)) {
    const pr = m?.progress;
    box.innerHTML = head + (pr ? `<div class="progress"><i style="width:${pr.done / pr.total * 100}%"></i></div><div class="muted" style="font-size:12px;margin:6px 0">Fetching prices: ${pr.done} of ${pr.total} stocks…</div>` : "") + "<div class='skeleton' style='height:44px;margin:6px 0'></div>".repeat(6);
    wireMovers(); return;
  }
  if (m.status === "error") { box.innerHTML = head + `<div class="muted">${esc(m.error)}</div>`; wireMovers(); return; }
  const list = moverList(), shown = E.showAll ? list : list.slice(0, 8);
  const extra = (r) => E.tab === "volume" ? `<span class="pill">${r.volume_ratio.toFixed(1)}× avg vol</span>`
    : E.tab === "high" ? `<span class="pill">${pctTxt(r.from_52w_high, 1)} from high</span>` : E.tab === "low" ? `<span class="pill">${pctTxt(r.from_52w_low, 1)} from low</span>` : "";
  box.innerHTML = head + `<div class="table-wrap"><table class="movers"><tr><th>Instrument</th><th class="hide-sm">1M trend</th><th class="hide-sm">${E.tab === "gainers" || E.tab === "losers" ? "Sector" : "Signal · sector"}</th><th class="r">Price · day change</th></tr>${shown.map((r) => `<tr class="click" data-sym="${esc(r.symbol)}">
      <td><div class="cell-id">${logoHtml(r.symbol, r.name, 32)}<div><div class="cell-sym">${symHtml(r.symbol)}</div><div class="cell-name">${esc(r.name || "")}</div></div></div></td>
      <td class="hide-sm">${sparkline(r.closes, 80, 26)}</td>
      <td class="hide-sm">${extra(r)}<span class="muted" style="font-size:12px">${esc(r.sector || "")}</span></td>
      <td class="r num"><b>${money(r.price, r.currency)}</b><div class="${tone(r.change_pct)}" style="font-size:12.5px">${signedMoney(r.change, r.currency)} (${pctTxt(r.change_pct)})</div></td></tr>`).join("")
      || `<tr><td class="muted">Nothing here right now.</td></tr>`}</table></div>
    ${list.length > 8 ? `<button class="btn sm ghost" id="moreMovers" style="margin-top:8px">${E.showAll ? "Show less" : `Show all ${list.length}`}</button>` : ""}`;
  wireMovers();
}
function wireMovers() {
  const box = $("#exMovers");
  box.onclick = (e) => {
    const t = e.target.closest("[data-t]"); if (t) { E.tab = t.dataset.t; E.showAll = false; renderMovers(); return; }
    if (e.target.id === "moreMovers") { E.showAll = !E.showAll; renderMovers(); return; }
    if (e.target.id === "clearSector") { e.preventDefault(); E.sector = null; renderMovers(); renderSectors(); return; }
    const tr = e.target.closest("tr[data-sym]"); if (tr) navigate("markets", "", { symbol: tr.dataset.sym });
  };
}

// ------------------------------------------------------------------ sectors & breadth
function sectorStats() {
  const m = new Map();
  for (const r of rows()) {
    const k = r.sector || "Other", s = m.get(k) || { sector: k, n: 0, adv: 0, dec: 0, sum: 0, best: null };
    s.n++; s.sum += r.change_pct ?? 0; if (r.change_pct > 0) s.adv++; if (r.change_pct < 0) s.dec++;
    if (!s.best || (r.change_pct ?? 0) > (s.best.change_pct ?? 0)) s.best = r;
    m.set(k, s);
  }
  return [...m.values()].map((s) => ({ ...s, avg: s.sum / s.n })).sort((a, b) => b.avg - a.avg);
}
function tint(v) {
  const a = Math.min(Math.abs(v) / 2.5, 1) * 0.32 + 0.05;
  return v >= 0 ? `rgba(28,196,126,${a})` : `rgba(242,73,92,${a})`;
}
function renderSectors() {
  const box = $("#exSectors");
  if (!rows().length) { box.innerHTML = `<div class="card-t" style="margin-bottom:10px">Sectors today</div><div class="skeleton" style="height:150px"></div>`; return; }
  const st = sectorStats();
  box.innerHTML = `<div class="card-h"><span class="card-t">Sectors today</span><span class="muted" style="font-size:12px">Average move of the stocks in each sector · click to filter movers</span></div>
    <div class="heat">${st.map((s) => `<button class="tile-s ${E.sector === s.sector ? "on" : ""}" data-sec="${esc(s.sector)}" style="background:${tint(s.avg)}">
      <span class="s-name">${esc(s.sector)}</span><span class="s-chg num ${tone(s.avg)}">${pctTxt(s.avg)}</span>
      <span class="s-ad"><span class="up">▲${s.adv}</span> <span class="down">▼${s.dec}</span> · ${s.n} stocks</span></button>`).join("")}</div>`;
  box.onclick = (e) => { const b = e.target.closest("[data-sec]"); if (!b) return; E.sector = E.sector === b.dataset.sec ? null : b.dataset.sec; E.showAll = false; renderSectors(); renderMovers(); };
}
function renderBreadth() {
  const box = $("#exBreadth"), rs = rows();
  if (!rs.length) { box.innerHTML = `<div class="skeleton" style="height:90px"></div>`; return; }
  const adv = rs.filter((r) => r.change_pct > 0).length, dec = rs.filter((r) => r.change_pct < 0).length, flat = rs.length - adv - dec;
  const top = [...rs].sort((a, b) => b.change_pct - a.change_pct), best = top[0], worst = top[top.length - 1];
  const avg = rs.reduce((s, r) => s + (r.change_pct || 0), 0) / rs.length;
  box.innerHTML = `<div class="card-h"><span class="card-t">Market breadth</span><span class="muted" style="font-size:12px">${esc(E.movers.label)}</span></div>
    <div class="breadth"><i class="up-bar" style="flex:${adv}"></i><i class="flat-bar" style="flex:${flat}"></i><i class="down-bar" style="flex:${dec}"></i></div>
    <div class="range-ends num" style="margin-bottom:10px"><span class="up">${adv} advancing</span><span class="down">${dec} declining</span></div>
    <div class="stats" style="grid-template-columns:repeat(3,minmax(0,1fr))">
      <div class="stat"><div class="k">Average move</div><div class="v num ${tone(avg)}">${pctTxt(avg)}</div></div>
      <div class="stat"><div class="k">Top gainer</div><div class="v"><a href="#/markets?symbol=${encodeURIComponent(best.symbol)}" style="text-decoration:none">${esc(splitSym(best.symbol)[0])}</a> <span class="num up" style="font-size:12.5px">${pctTxt(best.change_pct, 1)}</span></div></div>
      <div class="stat"><div class="k">Top loser</div><div class="v"><a href="#/markets?symbol=${encodeURIComponent(worst.symbol)}" style="text-decoration:none">${esc(splitSym(worst.symbol)[0])}</a> <span class="num down" style="font-size:12.5px">${pctTxt(worst.change_pct, 1)}</span></div></div></div>`;
}

// ------------------------------------------------------------------ screens
function trackBadge(t) {
  if (t.verdict === "too_few_events") return `<span class="badge-t">Too few past signals (${t.events})</span>`;
  const right = `${Math.round(t.hit_rate * 100)}% right vs ${Math.round(t.base_rate * 100)}% base`;
  if (t.verdict === "beats_base_rate") return `<span class="badge-t good" title="Lower 95% bound ${Math.round(t.ci_low * 100)}% beats the base rate">${right}</span>`;
  if (t.verdict === "worse_than_base_rate") return `<span class="badge-t bad">${right}</span>`;
  return `<span class="badge-t" title="Within noise of the base rate">No proven edge · ${right}</span>`;
}
function renderScreens() {
  const box = $("#exScreens"), s = E.screens;
  const head = `<div class="card-h"><div><div class="card-t">Trading screens</div><div class="muted" style="font-size:12px">With each signal's real track record — not just a label</div></div></div>`;
  if (!s || !s.result) {
    const pr = s?.progress;
    box.innerHTML = head + (pr ? `<div class="progress"><i style="width:${pr.done / pr.total * 100}%"></i></div><div class="muted" style="font-size:12px">Back-testing ${pr.done}/${pr.total} stocks…</div>` : "") +
      (s?.status === "error" ? `<div class="muted">${esc(s.error)}</div>` : "<div class='skeleton' style='height:46px;margin:8px 0'></div>".repeat(5));
    return;
  }
  box.innerHTML = head + s.result.map((r) => `<div class="screen ${E.openScreen === r.key ? "open" : ""}" data-k="${r.key}">
      <div class="screen-h"><div><div class="screen-n">${esc(r.name)} <span class="pill ${r.bias === "bullish" ? "up" : "down"}">${r.bias === "bullish" ? "Bullish" : "Bearish"}</span></div>
        <div class="screen-d">${trackBadge(r.track)}</div></div><div class="screen-c num">${r.matches.length}<span class="faint"> now</span></div></div>
      ${E.openScreen === r.key ? `<div class="screen-body"><div class="muted" style="font-size:12.5px;margin:6px 0">${esc(r.description)} Over the last 2 years it triggered ${r.track.events} times;
        ${r.track.avg_return_pct != null ? `the average move ${r.track.horizon} sessions later was ${pctTxt(r.track.avg_return_pct)}.` : ""}</div>
        ${r.matches.length ? r.matches.map((mm) => `<a class="chip-link" href="#/markets?symbol=${encodeURIComponent(mm.symbol)}">${esc(splitSym(mm.symbol)[0])}<span class="faint">${mm.sessions_ago === 0 ? "today" : mm.sessions_ago === 1 ? "1d ago" : `${mm.sessions_ago}d ago`}</span></a>`).join("")
          : `<span class="muted" style="font-size:12.5px">No stock triggered this in the last 3 sessions.</span>`}</div>` : ""}
    </div>`).join("") +
    `<div class="fine">Hit rate = share of past signals followed by a ${s.horizon}-session move in the signalled direction, across ${esc(s.label)} over 2 years. Signals on the same dates move together, so treat the confidence as optimistic. Not investment advice.</div>`;
  box.onclick = (e) => { const el = e.target.closest(".screen"); if (!el || e.target.closest("a")) return; E.openScreen = E.openScreen === el.dataset.k ? null : el.dataset.k; renderScreens(); };
}

// ------------------------------------------------------------------ investments & news
async function renderInvest() {
  const box = $("#exInvest");
  await ensurePortfolio();
  const d = portfolioSummary();
  const g = d?.groups?.find((x) => x.currency === "INR") || d?.groups?.[0];
  if (!d || !d.transactions || !g) {
    box.innerHTML = `<div class="card-t">Your investments</div><div class="muted" style="font-size:13px;margin:8px 0 12px">Track holdings, XIRR, tax and portfolio health in one place.</div>
      <div class="actions"><a class="btn primary sm" href="#/portfolio">Add your first trade</a></div>`;
    return;
  }
  const c = g.currency;
  box.innerHTML = `<div class="card-h"><span class="card-t">Your investments</span><a class="btn sm ghost" href="#/portfolio">Open</a></div>
    <div class="num" style="font-size:24px;font-weight:800;letter-spacing:-.02em">${money(g.value, c)}</div>
    <div class="stats" style="grid-template-columns:repeat(3,minmax(0,1fr));margin-top:10px">
      <div class="stat"><div class="k">Today</div><div class="v num ${tone(g.day_change)}">${signedMoney(g.day_change, c, 0)}</div></div>
      <div class="stat"><div class="k">Total P&L</div><div class="v num ${tone(g.pnl)}">${pctTxt(g.pnl_pct, 1)}</div></div>
      <div class="stat"><div class="k">XIRR</div><div class="v num ${tone(g.xirr)}">${g.xirr == null ? "—" : pctTxt(g.xirr * 100, 1)}</div></div></div>`;
}
async function loadNews() {
  E.newsLoaded = true;
  const held = await ensurePortfolio();
  const movers = [...rows()].sort((a, b) => Math.abs(b.change_pct) - Math.abs(a.change_pct)).slice(0, 6).map((r) => r.symbol);
  const syms = [...new Set([...held, ...getWatchlist().filter((s) => !s.startsWith("^") && !s.endsWith("=X")), ...movers])].slice(0, 30);
  const box = $("#exNews");
  if (!syms.length) { box.hidden = true; return; }
  try { E.news = await api(`/news?symbols=${encodeURIComponent(syms.join(","))}&period=7d`); } catch { E.news = null; }
  const items = (E.news?.items || []).slice(0, 8);  // full-width section: 4 across on wide screens
  box.hidden = !items.length;
  const chg = (sym) => rows().find((r) => r.symbol === sym)?.change_pct ?? feed.last(sym)?.change_pct;
  box.innerHTML = `<div class="card-h"><span class="card-t">Stocks in the news</span><a class="btn sm ghost" href="#/news">All news</a></div>
    <div class="news-grid">${items.map((it) => { const s = it.symbols[0], c = chg(s); return `<a class="news-card${it.image ? " has-img" : ""}" href="${esc(articleHref(it))}">
      ${it.image ? `<div class="news-cover"><img src="${esc(it.image)}" alt="" loading="lazy" decoding="async" onerror="this.parentNode.remove()"></div>` : ""}
      <div class="news-card-h"><span class="cell-sym">${symHtml(s)}</span>${c != null ? `<span class="num ${tone(c)}" style="font-size:12.5px;font-weight:700">${pctTxt(c)}</span>` : ""}</div>
      <div class="news-title" style="font-size:13.5px">${esc(it.title)}</div><div class="news-meta">${esc(it.publisher)} · ${esc(ago(it.published_at))}</div></a>`; }).join("")}</div>`;
}

// ------------------------------------------------------------------ routing
export async function initExplore() {
  E.universes = await universeCatalog();
  E.universe = validUniverse(E.universes, E.universe);
  $("#exUniverse").onchange = (e) => {
    E.universe = e.target.value; store.set("kairo-explore-universe", E.universe);
    E.movers = E.screens = null; E.sector = null; E.openScreen = null; E.newsLoaded = false;
    renderUniverse(); clearTimers(); renderMovers(); renderSectors(); renderBreadth(); renderScreens(); loadMovers(); loadScreens();
  };
}
function renderUniverse() {
  $("#exUniverse").innerHTML = universeOptions(E.universes, E.universe);
  universeCatalog().then((cat) => { if (cat !== E.universes) { E.universes = cat; $("#exUniverse").innerHTML = universeOptions(cat, E.universe); } });
}
export function showExplore() {
  const now = new Date();
  $("#exSub").textContent = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  renderUniverse(); clearTimers();
  renderMovers(); renderSectors(); renderBreadth(); renderScreens();
  loadMovers(); loadScreens(); renderInvest();
  schedule(function tick() { loadMovers(); schedule(tick, 60000); }, 60000);
}
export function hideExplore() { clearTimers(); }
