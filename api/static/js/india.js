// Pulse: the India market data traders check every day. FII / DII flows and F&O positioning, bulk and block
// deals, the results and corporate-action calendar, IPOs, high-delivery buying and surveillance lists.
import { $, esc, api, store, fmt, pctTxt, tone, compact, logoHtml } from "./core.js";
import { getWatchlist } from "./markets.js";
import { ensurePortfolio } from "./portfolio.js";

const TABS = [["flows", "Flows & positioning"], ["deals", "Bulk & block deals"], ["calendar", "Results & actions"],
  ["ipos", "IPOs"], ["delivery", "Delivery"], ["surveillance", "Surveillance"]];
const P = { tab: store.get("kairo-pulse-tab", "flows"), data: {}, deals: "block", cal: store.get("kairo-pulse-cal", "mine"), q: "" };
const cr = (v) => (v == null ? "—" : `${v < 0 ? "−" : ""}₹${Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 0 })} cr`);
const day = (iso) => (iso ? new Date(iso + "T00:00:00").toLocaleDateString([], { day: "numeric", month: "short" }) : "—");
const stockLink = (s) => `<a href="#/markets?symbol=${encodeURIComponent(s + ".NS")}" class="pl-sym">${logoHtml(s + ".NS", s, 20)}<b>${esc(s)}</b></a>`;

export function initPulse() {
  $("#pulseTabs").innerHTML = TABS.map(([k, l]) => `<button data-t="${k}">${l}</button>`).join("");
  $("#pulseTabs").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; P.tab = b.dataset.t; store.set("kairo-pulse-tab", P.tab); show(); };
  $("#pulseBody").addEventListener("click", onClick);
  $("#pulseBody").addEventListener("input", (e) => { if (e.target.id === "plSearch") { P.q = e.target.value.trim().toUpperCase(); render(); } });
}

export function showPulse(sub) {
  if (sub && TABS.some(([k]) => k === sub)) P.tab = sub;
  document.title = "Pulse · Kairo Markets";
  show();
}

const SOURCES = { flows: ["/india/flows", "/india/participants"], deals: ["/india/deals"], calendar: ["/india/calendar?days=45"],
  ipos: ["/india/ipos"], delivery: ["/india/delivery"], surveillance: ["/india/surveillance"] };

async function show() {
  [...$("#pulseTabs").children].forEach((b) => b.classList.toggle("on", b.dataset.t === P.tab));
  const tab = P.tab;
  if (!P.data[tab]) {
    render();
    const got = await Promise.all(SOURCES[tab].map((u) => api(u).catch((err) => ({ error: err.message }))));
    if (tab === "calendar") got.push(await mySymbols());
    P.data[tab] = got;
  }
  if (tab === P.tab) render();
}
async function mySymbols() {
  const held = await ensurePortfolio().catch(() => []);
  return new Set([...held, ...getWatchlist()].filter((s) => /\.(NS|BO)$/.test(s)).map((s) => s.replace(/\.(NS|BO)$/, "")));
}

function render() {
  const box = $("#pulseBody"), d = P.data[P.tab];
  if (!d) { box.innerHTML = `<div class="card"><div class="skeleton" style="height:260px"></div></div>`; return; }
  const err = d.find((x) => x?.error);
  const body = { flows: flowsHtml, deals: dealsHtml, calendar: calendarHtml, ipos: iposHtml, delivery: deliveryHtml, surveillance: survHtml }[P.tab](...d);
  box.innerHTML = (err && d.every((x) => x?.error) ? `<div class="card" style="color:var(--down)">Couldn't load this from NSE: ${esc(err.error)}. Try again in a minute.</div>` : body);
  wireChart();
}

// ------------------------------------------------------------------ flows + participant positioning
function flowsHtml(f, p) {
  const l = f.latest || {};
  const tile = (k, x) => `<div class="pl-tile"><div class="k">${k} net</div><div class="v num ${tone(x?.net)}">${cr(x?.net)}</div>
      <div class="muted pl-sub">Bought ${cr(x?.buy)} · sold ${cr(x?.sell)}</div></div>`;
  const hist = f.history || [];
  const chart = hist.length >= 3 ? flowChart(hist) : `<div class="muted pl-note">The daily chart appears once Kairo has collected three sessions (NSE publishes one day at a time; ${hist.length} so far).</div>`;
  return `<div class="card"><div class="card-h"><span class="card-t">FII / DII cash market · ${day(l.date)}</span><span class="muted pl-sub">₹ crore, provisional</span></div>
      <div class="pl-tiles">${tile("FII / FPI", l.fii)}${tile("DII", l.dii)}</div>${chart}</div>
    ${p?.error ? "" : positioningHtml(p)}`;
}

function flowChart(hist) {
  const W = 640, H = 200, padL = 52, padB = 22, padT = 10, n = hist.length;
  const max = Math.max(1, ...hist.flatMap((h) => [Math.abs(h.fii ?? 0), Math.abs(h.dii ?? 0)]));
  const y0 = padT + (H - padT - padB) / 2, sy = (H - padT - padB) / 2 / max, band = (W - padL - 8) / n, bw = Math.max(3, Math.min(14, band / 2 - 3));
  const bar = (x, v, cls, h, who) => {
    if (v == null) return "";
    const hgt = Math.max(1, Math.abs(v) * sy), top = v >= 0 ? y0 - hgt : y0, r = Math.min(4, hgt);
    // 4px rounded data end, square at the baseline
    const path = v >= 0 ? `M${x},${y0}V${top + r}Q${x},${top} ${x + r},${top}H${x + bw - r}Q${x + bw},${top} ${x + bw},${top + r}V${y0}Z`
      : `M${x},${y0}V${top + hgt - r}Q${x},${top + hgt} ${x + r},${top + hgt}H${x + bw - r}Q${x + bw},${top + hgt} ${x + bw},${top + hgt - r}V${y0}Z`;
    return `<path class="${cls}" d="${path}"/><rect class="pl-hit" x="${x - 1}" y="${padT}" width="${bw + 2}" height="${H - padT - padB}" data-tip="${esc(`${day(h.date)} · ${who} ${cr(v)}`)}"/>`;
  };
  const ticks = [max, max / 2, 0, -max / 2, -max].map((v) => `<line x1="${padL}" x2="${W}" y1="${y0 - v * sy}" y2="${y0 - v * sy}" class="${v === 0 ? "pl-zero" : "pl-grid"}"/>
      <text x="${padL - 6}" y="${y0 - v * sy + 4}" class="pl-ax" text-anchor="end">${compact(Math.round(v))}</text>`).join("");
  const bars = hist.map((h, i) => { const x = padL + i * band + band / 2 - bw - 1; return bar(x, h.fii, "pl-fii", h, "FII") + bar(x + bw + 2, h.dii, "pl-dii", h, "DII"); }).join("");
  const labels = hist.map((h, i) => (i % Math.ceil(n / 8) === 0 || i === n - 1 ? `<text x="${padL + i * band + band / 2}" y="${H - 6}" class="pl-ax" text-anchor="middle">${day(h.date)}</text>` : "")).join("");
  const table = `<details class="pl-table"><summary>Show as a table</summary><table><tr><th>Date</th><th class="r">FII net</th><th class="r">DII net</th></tr>
    ${[...hist].reverse().map((h) => `<tr><td>${day(h.date)}</td><td class="r num ${tone(h.fii)}">${cr(h.fii)}</td><td class="r num ${tone(h.dii)}">${cr(h.dii)}</td></tr>`).join("")}</table></details>`;
  return `<div class="pl-legend"><span><i class="pl-fii-k"></i>FII / FPI</span><span><i class="pl-dii-k"></i>DII</span></div>
    <div class="pl-chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="FII and DII net flows by day">${ticks}${bars}${labels}</svg><div class="pl-tip" hidden></div></div>${table}`;
}

function positioningHtml(p) {
  const last = p?.days?.[0], prev = p?.days?.[1];
  if (!last) return "";
  const row = (who) => {
    const x = last.participants[who]; if (!x) return "";
    const was = prev?.participants?.[who]?.fut_long_pct, lp = x.fut_long_pct ?? 0;
    return `<div class="pl-pos"><div class="pl-who">${who}</div>
      <div class="pl-meter" title="${esc(`${who} index futures: ${fmt(x.fut_long, 0)} long, ${fmt(x.fut_short, 0)} short contracts`)}">
        <span class="pl-long" style="width:${lp}%"></span><span class="pl-short" style="width:${100 - lp}%"></span></div>
      <div class="pl-pct num"><b>${lp.toFixed(1)}%</b> long${was != null ? ` <span class="muted">(${lp >= was ? "+" : "−"}${Math.abs(lp - was).toFixed(1)} pts)</span>` : ""}</div>
      <div class="pl-net num ${tone(x.fut_net)}">${x.fut_net >= 0 ? "+" : "−"}${compact(Math.abs(x.fut_net))} net</div></div>`;
  };
  const fii = last.participants.FII?.fut_long_pct;
  const read = fii == null ? "" : fii < 25 ? "FIIs are heavily short index futures: historically a crowded bearish bet, which can fuel sharp short-covering rallies."
    : fii < 40 ? "FIIs lean short on index futures." : fii > 70 ? "FIIs are heavily long index futures: a crowded bullish bet." : fii > 55 ? "FIIs lean long on index futures." : "FII index-futures positioning is balanced.";
  return `<div class="card"><div class="card-h"><span class="card-t">Who is long, who is short · index futures · ${day(last.date)}</span>
      <span class="pl-legend"><span><i class="pl-long-k"></i>Long</span><span><i class="pl-short-k"></i>Short</span></span></div>
    ${["FII", "DII", "Pro", "Client"].map(row).join("")}
    <p class="pl-read">${esc(read)}</p><p class="muted pl-sub">${esc(p.about)}</p></div>`;
}

// ------------------------------------------------------------------ deals
function dealsHtml(d) {
  if (d.error) return "";
  const list = d[P.deals] || [];
  const seg = `<div class="seg" id="plDealSeg">${[["block", `Block (${d.block.length})`], ["bulk", `Bulk (${d.bulk.length})`], ["short", `Short (${d.short.length})`]]
    .map(([k, l]) => `<button data-deal="${k}" class="${k === P.deals ? "on" : ""}">${l}</button>`).join("")}</div>`;
  const rows = [...list].sort((a, b) => (b.value_cr ?? 0) - (a.value_cr ?? 0)).map((x) => `<tr>
      <td>${stockLink(x.symbol)}</td><td>${x.side ? `<span class="pill ${x.side === "BUY" ? "up" : "down"}">${esc(x.side)}</span>` : ""}${esc(x.client || "—")}</td>
      <td class="r num">${fmt(x.qty, 0)}</td><td class="r num">${fmt(x.price)}</td><td class="r num"><b>${x.value_cr != null ? cr(x.value_cr) : "—"}</b></td></tr>`).join("");
  return `<div class="card"><div class="card-h"><span class="card-t">Large deals · ${day(d.as_on)}</span>${seg}</div>
    <p class="muted pl-sub">${{ block: "Block deals: single trades of ₹10 crore or more, done in a separate window. Usually institutions.",
      bulk: "Bulk deals: one client trading over 0.5% of a company's shares in a day.", short: "Short deals: shares sold without being held (to be bought back)." }[P.deals]}</p>
    ${list.length ? `<div class="table-wrap"><table><tr><th>Stock</th><th>Client</th><th class="r">Quantity</th><th class="r">Price</th><th class="r">Value</th></tr>${rows}</table></div>`
      : `<div class="empty" style="height:120px">No ${P.deals} deals in the last session.</div>`}</div>`;
}

// ------------------------------------------------------------------ calendar
function calendarHtml(c, mine) {
  if (c.error) return "";
  const filter = (x) => (P.cal === "mine" ? mine.has(x.symbol) : P.cal === "results" ? x.type === "meeting" && x.results : P.cal === "dividends" ? x.kind === "dividend" : true);
  const items = c.items.filter(filter);
  const seg = `<div class="seg" id="plCalSeg">${[["mine", `My stocks`], ["results", "Results"], ["dividends", "Dividends"], ["all", "Everything"]]
    .map(([k, l]) => `<button data-cal="${k}" class="${k === P.cal ? "on" : ""}">${l}</button>`).join("")}</div>`;
  let last = "", html = "";
  for (const x of items.slice(0, 300)) {
    if (x.date !== last) { html += `<div class="pl-day">${new Date(x.date + "T00:00:00").toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })}</div>`; last = x.date; }
    const what = x.type === "meeting" ? `<span class="pill ${x.results ? "up" : ""}">${x.results ? "RESULTS" : "BOARD MEETING"}</span> ${esc(x.purpose)}`
      : `<span class="pill">${esc(x.kind.toUpperCase())}</span> ${esc(x.subject)}${x.record_date ? ` <span class="muted">· record ${day(x.record_date)}</span>` : ""}`;
    html += `<div class="pl-ev">${stockLink(x.symbol)}<span class="pl-what">${what}</span></div>`;
  }
  const hint = P.cal === "mine" && !mine.size ? "Add stocks to your watchlist or portfolio to see their events here." : `No events in the next 45 days${P.cal === "mine" ? " for your stocks" : ""}.`;
  return `<div class="card"><div class="card-h"><span class="card-t">Next 45 days</span>${seg}</div>
    <p class="muted pl-sub">Results days move stocks the most; ex-dividend days drop the price by about the dividend. Corporate actions are shown on their ex-date.</p>
    ${items.length ? html : `<div class="empty" style="height:120px">${hint}</div>`}</div>`;
}

// ------------------------------------------------------------------ IPOs
function iposHtml(d) {
  if (d.error) return "";
  const row = (x, mode) => `<tr><td><b>${esc(x.company || x.symbol)}</b><div class="muted pl-sub">${esc(x.symbol || "")}${x.series && x.series !== "EQ" ? ` · <span class="pill">${esc(x.series)}</span>` : ""}</div></td>
      <td>${esc(x.price || "—")}</td><td>${day(x.start)} – ${day(x.end)}</td>
      <td class="r num">${mode === "open" ? (x.subscribed_x != null ? `<b>${x.subscribed_x.toFixed(2)}×</b>` : "—") : mode === "listed" ? day(x.listing_date) : esc(x.status || "")}</td></tr>`;
  const sect = (title, list, mode, last) => `<div class="card"><div class="card-t" style="margin-bottom:10px">${title}</div>
    ${list.length ? `<div class="table-wrap"><table><tr><th>Company</th><th>Price band</th><th>Bidding</th><th class="r">${last}</th></tr>${list.map((x) => row(x, mode)).join("")}</table></div>`
      : `<div class="empty" style="height:80px">None right now.</div>`}</div>`;
  return sect("Open now", d.open, "open", "Subscribed") + sect("Upcoming", d.upcoming, "up", "Status") + sect("Listed in the last 60 days", d.listed, "listed", "Listed on")
    + `<p class="muted pl-sub">Subscription is the number of times the shares on offer have been bid for, across all categories. NSE's list includes bond issues (series DEBT).</p>`;
}

// ------------------------------------------------------------------ delivery
function deliveryHtml(d) {
  if (d.error) return "";
  const rows = d.items.map((x) => `<tr><td>${stockLink(x.symbol)}</td><td class="r num ${tone(x.change_pct)}">${pctTxt(x.change_pct)}</td>
      <td class="r num"><b>${x.delivery_pct?.toFixed(1)}%</b></td><td class="r num">${x.avg_delivery_pct != null ? x.avg_delivery_pct.toFixed(1) + "%" : "—"}</td>
      <td class="r num"><b>${x.delivery_x?.toFixed(1)}×</b></td><td class="r num">${cr(x.turnover_cr)}</td></tr>`).join("");
  return `<div class="card"><div class="card-h"><span class="card-t">High-delivery buying · ${day(d.date)}</span></div>
    <p class="muted pl-sub">${esc(d.about)} Shown: stocks up on the day, with delivery above 50% and at least 1.5× their usual delivered quantity, turnover over ₹5 crore.</p>
    ${d.items.length ? `<div class="table-wrap"><table><tr><th>Stock</th><th class="r">Day</th><th class="r">Delivery</th><th class="r">Usual</th><th class="r">Delivered vs usual</th><th class="r">Turnover</th></tr>${rows}</table></div>`
      : `<div class="empty" style="height:120px">No stock met the bar in the last session.</div>`}</div>`;
}

// ------------------------------------------------------------------ surveillance
function survHtml(d) {
  if (d.error) return "";
  const items = d.items.filter((x) => !P.q || x.symbol.includes(P.q));
  const rows = items.slice(0, 400).map((x) => `<tr><td>${stockLink(x.symbol)}</td><td>${x.fo_ban ? `<span class="pill down">F&O BAN</span>` : ""}${x.gsm ? `<span class="pill down">GSM</span>` : ""}${x.asm ? `<span class="pill">ASM</span>` : ""}</td>
      <td class="muted pl-sub">${esc([x.asm, x.gsm].filter(Boolean).join(" · ") || x.fo_ban || "")}</td></tr>`).join("");
  return `<div class="card"><div class="card-h"><span class="card-t">Stocks under exchange surveillance</span><input class="input" id="plSearch" placeholder="Find a symbol" value="${esc(P.q)}" style="max-width:200px"></div>
    <p class="muted pl-sub">ASM / GSM are exchange measures for unusual price moves or weak fundamentals: higher margins, trade-for-trade settlement or limited trading.
      F&O ban (${d.fo_ban?.symbols?.length ?? 0} today): no new futures and options positions. Kairo warns you in the order ticket for these stocks.</p>
    <div class="table-wrap"><table><tr><th>Stock</th><th>Lists</th><th>Detail</th></tr>${rows}</table></div></div>`;
}

// ------------------------------------------------------------------ interaction
function onClick(e) {
  const deal = e.target.closest("[data-deal]"); if (deal) { P.deals = deal.dataset.deal; render(); return; }
  const cal = e.target.closest("[data-cal]"); if (cal) { P.cal = cal.dataset.cal; store.set("kairo-pulse-cal", P.cal); render(); }
}
function wireChart() {
  const wrap = document.querySelector(".pl-chart"); if (!wrap) return;
  const tip = wrap.querySelector(".pl-tip");
  wrap.onmousemove = (e) => {
    const t = e.target.closest?.("[data-tip]");
    if (!t) { tip.hidden = true; return; }
    const r = wrap.getBoundingClientRect();
    tip.hidden = false; tip.textContent = t.dataset.tip;
    tip.style.left = `${Math.min(e.clientX - r.left + 12, r.width - 170)}px`; tip.style.top = `${e.clientY - r.top - 34}px`;
  };
  wrap.onmouseleave = () => (tip.hidden = true);
}
