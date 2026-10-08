// Portfolio: holdings (live), tax P&L, health check, transactions, add / import.
import { $, esc, api, toast, modal, money, signedMoney, pctTxt, tone, fmt, dateTxt, symHtml, splitSym, inrShort, navigate, feed, isMf, logoHtml } from "./core.js";

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const PALETTE = ["#7c5cff", "#1cc47e", "#4ea1ff", "#e9a23b", "#f2495c", "#c27bff", "#2ec4b6", "#ff8a5b", "#9aa2b1"];
const P = { data: null, loading: false, error: null, sub: "holdings", cur: null, tax: null, taxFy: null, health: {}, txns: null };

// ------------------------------------------------------------------ data
async function load() {
  P.loading = true; render();
  try { P.data = await api("/portfolio"); P.error = null; }
  catch (err) { P.error = err.message; }
  P.loading = false;
  const curs = groups().map((g) => g.currency);
  if (!curs.includes(P.cur)) P.cur = curs.includes("INR") ? "INR" : curs[0] || null;
  render(); startLive();
}
const groups = () => P.data?.groups?.filter((g) => g.positions > 0 || g.realized_pnl) ?? [];
const positions = () => (P.data?.positions ?? []).filter((p) => !P.cur || p.currency === P.cur);

let liveWired = false;
function startLive() {
  // Holdings stay subscribed on the shared feed (no reconnect churn when switching pages).
  // Fund NAVs are daily, so only exchange-traded holdings go on the live tick feed.
  feed.require("portfolio", (P.data?.positions ?? []).map((p) => p.symbol).filter((s) => !isMf(s)));
  if (liveWired) return;
  liveWired = true;
  feed.on("tick", (d) => {
    const p = P.data?.positions.find((x) => x.symbol === d.symbol); if (!p) return;
    const dir = p.price == null ? 0 : Math.sign(d.price - p.price);
    p.price = d.price;
    if (d.previous_close) p.previous_close = d.previous_close;
    recompute(p);
    if (P.sub === "holdings" && !$("#view-portfolio").hidden) { renderSummary(); patchRow(p, dir); }
  });
}
export function hidePortfolio() { /* holdings remain on the shared feed */ }
export const portfolioSummary = () => P.data;

function recompute(p) {
  p.value = p.quantity * p.price;
  p.pnl = p.value - p.invested;
  p.pnl_pct = p.invested ? p.pnl / p.invested * 100 : null;
  if (p.previous_close) { p.day_change = p.quantity * (p.price - p.previous_close); p.day_change_pct = (p.price / p.previous_close - 1) * 100; }
  const g = P.data.groups.find((x) => x.currency === p.currency);
  const ps = P.data.positions.filter((x) => x.currency === p.currency);
  g.value = ps.reduce((s, x) => s + (x.value || 0), 0);
  g.pnl = g.value - g.invested; g.pnl_pct = g.invested ? g.pnl / g.invested * 100 : null;
  g.day_change = ps.reduce((s, x) => s + (x.day_change || 0), 0);
  ps.forEach((x) => (x.weight = g.value ? (x.value || 0) / g.value * 100 : null));
}

// ------------------------------------------------------------------ shell
function render() {
  const box = $("#portfolioBody");
  const gs = groups();
  // Data may have been loaded elsewhere (Explore's "Your investments"), so pick the currency here too.
  if (!gs.some((g) => g.currency === P.cur)) P.cur = gs.some((g) => g.currency === "INR") ? "INR" : gs[0]?.currency ?? null;
  $("#pfCurSeg").innerHTML = gs.length > 1 ? gs.map((g) => `<button data-cur="${g.currency}" class="${g.currency === P.cur ? "on" : ""}">${g.currency}</button>`).join("") : "";
  $("#pfCurSeg").hidden = gs.length < 2;
  if (P.loading && !P.data) { box.innerHTML = `<div class="summary">${"<div><div class='skeleton' style='height:46px'></div></div>".repeat(5)}</div>`; return; }
  if (P.error) { box.innerHTML = `<div class="card" style="color:var(--down)">${esc(P.error)}</div>`; return; }
  if (!P.data.transactions) { box.innerHTML = emptyState(); wireEmpty(); return; }
  box.innerHTML = `<div id="pfSummary"></div>
    <nav class="tabs" id="pfTabs">${[["holdings", "Holdings"], ["tax", "Tax P&L"], ["health", "Health check"], ["transactions", "Transactions"]]
      .map(([k, l]) => `<button data-sub="${k}" class="${k === P.sub ? "on" : ""}">${l}</button>`).join("")}</nav><div id="pfSub"></div>`;
  $("#pfTabs").onclick = (e) => { const b = e.target.closest("button"); if (b) navigate("portfolio", b.dataset.sub); };
  renderSummary(); renderSub();
}

function emptyState() {
  return `<div class="card empty-state"><svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="var(--brand)" stroke-width="1.6"><path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-6"/></svg>
    <h3>Track your portfolio</h3><p>Add trades or import your broker's tradebook to see live P&L, XIRR, an Indian capital-gains estimate and a health check.</p>
    <div class="actions"><button class="btn primary" data-act="add">Add a trade</button><button class="btn" data-act="import">Import CSV</button></div>
    <div class="fine">Supports Zerodha Console tradebook exports and a simple date, symbol, side, quantity, price CSV. Everything stays on this computer.</div></div>`;
}
function wireEmpty() { $("#portfolioBody").onclick = (e) => { const a = e.target.closest("[data-act]")?.dataset.act; if (a === "add") openAddTrade(); if (a === "import") openImport(); }; }

function renderSummary() {
  const box = $("#pfSummary"); if (!box) return;
  const g = groups().find((x) => x.currency === P.cur); if (!g) { box.innerHTML = ""; return; }
  const c = g.currency, base = P.data.base_currency;
  const total = P.data.total_value_in_base != null && groups().length > 1
    ? `<div class="fine" style="margin:8px 2px 0">All holdings ≈ <b>${inrShort(P.data.total_value_in_base)}</b> (non-${base} converted at today's rate${Object.entries(P.data.fx_rates).filter(([k]) => k !== base).map(([k, v]) => `, ${k} = ${fmt(v)}`).join("")})</div>` : "";
  box.innerHTML = `<div class="summary">
    <div><div class="k">Current value</div><div class="v num">${money(g.value, c)}</div><div class="d">${g.positions} holding${g.positions === 1 ? "" : "s"}</div></div>
    <div><div class="k">Invested</div><div class="v num">${money(g.invested, c)}</div><div class="d">cost incl. charges</div></div>
    <div><div class="k">Total P&L</div><div class="v num ${tone(g.pnl)}">${signedMoney(g.pnl, c)}</div><div class="d num ${tone(g.pnl)}">${pctTxt(g.pnl_pct)} unrealized</div></div>
    <div><div class="k">Today</div><div class="v num ${tone(g.day_change)}">${signedMoney(g.day_change, c)}</div><div class="d">since previous close</div></div>
    <div><div class="k">XIRR</div><div class="v num ${tone(g.xirr)}">${g.xirr == null ? "—" : pctTxt(g.xirr * 100, 1)}</div><div class="d num">realized ${signedMoney(g.realized_pnl, c)}</div></div>
  </div>${total}`;
}

// ------------------------------------------------------------------ holdings
function holdingRow(p) {
  const c = p.currency;
  return `<tr class="click" data-sym="${esc(p.symbol)}">
    <td>${isMf(p.symbol) ? `<div class="cell-sym" style="white-space:normal">${esc(p.name || p.symbol)}</div><div class="cell-name">Mutual fund · ${esc(p.symbol.slice(2))}</div>`
      : `<div class="cell-id">${logoHtml(p.symbol, p.name, 32)}<div><div class="cell-sym">${symHtml(p.symbol)}</div><div class="cell-name">${esc(p.name || "")}</div></div></div>`}</td>
    <td class="r num">${fmt(p.quantity, p.quantity % 1 ? 4 : 0)}</td>
    <td class="r num hide-sm">${money(p.avg_cost, c)}</td>
    <td class="r num" data-f="price">${money(p.price, c)}</td>
    <td class="r num" data-f="value">${money(p.value, c)}</td>
    <td class="r num" data-f="pnl"><span class="${tone(p.pnl)}">${signedMoney(p.pnl, c)}</span><div class="cell-name ${tone(p.pnl)}" style="text-align:right">${pctTxt(p.pnl_pct)}</div></td>
    <td class="r num hide-sm" data-f="day"><span class="${tone(p.day_change)}">${pctTxt(p.day_change_pct)}</span></td>
    <td class="hide-sm" data-f="weight"><span class="wbar"><i style="width:${Math.min(100, p.weight || 0)}%"></i></span><span class="num">${p.weight == null ? "—" : p.weight.toFixed(1) + "%"}</span></td>
    <td class="r num hide-sm ${tone(p.xirr)}">${p.xirr == null ? "—" : pctTxt(p.xirr * 100, 1)}</td></tr>`;
}
function patchRow(p, dir) {
  const tr = document.querySelector(`#pfHoldings tr[data-sym="${CSS.escape(p.symbol)}"]`); if (!tr) return;
  const tmp = document.createElement("tbody"); tmp.innerHTML = holdingRow(p);
  const fresh = tmp.firstElementChild;
  ["price", "value", "pnl", "day", "weight"].forEach((f) => { const a = tr.querySelector(`[data-f="${f}"]`), b = fresh.querySelector(`[data-f="${f}"]`); if (a && b) a.innerHTML = b.innerHTML; });
  const cell = tr.querySelector('[data-f="price"]');
  if (dir) { cell.classList.remove("flash-up", "flash-down"); void cell.offsetWidth; cell.classList.add(dir > 0 ? "flash-up" : "flash-down"); setTimeout(() => cell.classList.remove("flash-up", "flash-down"), 600); }
}
function renderHoldings() {
  const ps = positions();
  const box = $("#pfSub");
  const issues = P.data.issues.length ? `<div class="notice" style="margin-bottom:14px"><b>Check these trades:</b> ${P.data.issues.map((i) => esc(i.message)).join(" · ")}</div>` : "";
  const missing = P.data.missing_quotes.length ? `<div class="fine">No live price for ${P.data.missing_quotes.map(esc).join(", ")} — shown without a value.</div>` : "";
  const alloc = ps.filter((p) => p.weight).map((p, i) => `<i title="${esc(p.symbol)} ${p.weight.toFixed(1)}%" style="width:${p.weight}%;background:${PALETTE[i % PALETTE.length]}"></i>`).join("");
  const legend = ps.filter((p) => p.weight).slice(0, 8).map((p, i) => `<span class="pill" style="border-left:3px solid ${PALETTE[i % PALETTE.length]}">${esc(splitSym(p.symbol)[0])} ${p.weight.toFixed(0)}%</span>`).join("");
  const closed = P.data.closed.filter((c) => !P.cur || c.currency === P.cur);
  box.innerHTML = issues + `<div class="card">
    <div class="card-h"><span class="card-t">Holdings</span><span class="muted" style="font-size:12px">Live prices · click a row for its chart</span></div>
    ${alloc ? `<div class="alloc">${alloc}</div><div style="margin-bottom:10px">${legend}</div>` : ""}
    <div class="table-wrap"><table id="pfHoldings"><tr><th>Instrument</th><th class="r">Qty</th><th class="r hide-sm">Avg cost</th><th class="r">LTP</th><th class="r">Value</th><th class="r">P&L</th><th class="r hide-sm">Day</th><th class="hide-sm">Weight</th><th class="r hide-sm">XIRR</th></tr>
    ${ps.map(holdingRow).join("") || `<tr><td colspan="9" class="muted">No open positions in ${esc(P.cur)}.</td></tr>`}</table></div>${missing}</div>` +
    (closed.length ? `<div class="card"><details><summary>Closed positions (${closed.length})</summary><div class="table-wrap" style="margin-top:8px"><table>
      <tr><th>Instrument</th><th class="r">Realized P&L</th></tr>${closed.map((c) => `<tr><td class="cell-sym">${symHtml(c.symbol)}</td><td class="r num ${tone(c.realized_pnl)}">${signedMoney(c.realized_pnl, c.currency)}</td></tr>`).join("")}
      </table></div></details></div>` : "");
  $("#pfHoldings").onclick = (e) => { const tr = e.target.closest("tr[data-sym]"); if (tr) navigate("markets", "", { symbol: tr.dataset.sym }); };
}

// ------------------------------------------------------------------ tax
async function loadTax(fy) {
  $("#pfSub").innerHTML = `<div class="tiles">${"<div class='tile'><div class='skeleton' style='height:60px'></div></div>".repeat(4)}</div>`;
  try { P.tax = await api(`/portfolio/tax${fy ? `?fy=${encodeURIComponent(fy)}` : ""}`); P.taxFy = P.tax.fy; }
  catch (err) { $("#pfSub").innerHTML = `<div class="card" style="color:var(--down)">${esc(err.message)}</div>`; return; }
  renderTax();
}
function renderTax() {
  const t = P.tax, rep = t.report, ind = rep.indian, plan = t.planning;
  const inr = (v) => money(v, "INR", 0);
  const fySel = `<select class="input" id="fySel" style="width:auto;height:32px">${t.available_fys.map((f) => `<option ${f === t.fy ? "selected" : ""}>${f}</option>`).join("")}</select>`;
  let h = `<div class="card-h" style="margin-bottom:12px"><div><div class="card-t">Capital gains estimate · ${esc(t.fy)}</div><div class="muted" style="font-size:12.5px">FIFO lots · resident individual · Indian listed equity, crypto and foreign shares</div></div>${fySel}</div>`;
  h += `<div class="tiles">
    <div class="tile"><div class="k">Short-term gains (net)</div><div class="v num">${ind ? inr(ind.net_stcg) : "—"}</div><div class="d">${ind ? `gains ${inr(ind.stcg)} · losses ${inr(ind.stcl)}` : "No Indian equity sales"}</div></div>
    <div class="tile"><div class="k">Long-term gains (net)</div><div class="v num">${ind ? inr(ind.net_ltcg) : "—"}</div><div class="d">${ind ? `gains ${inr(ind.ltcg)} · losses ${inr(ind.ltcl)}` : "&nbsp;"}</div></div>
    <div class="tile"><div class="k">LTCG exemption used</div><div class="v num">${ind ? inr(ind.exemption_used) : inr(0)}</div><div class="d">of ${inr(ind?.exemption_limit ?? plan.exemption_limit)} this year</div></div>
    <div class="tile"><div class="k">Estimated tax</div><div class="v num">${inr(rep.estimated_total_tax_inr)}</div><div class="d">incl. 4% cess</div></div></div>`;
  if (ind && (ind.st_buckets.length || ind.lt_buckets.length)) {
    const rows = [...ind.st_buckets.map((b) => ["Short-term (sec 111A)", b]), ...ind.lt_buckets.map((b) => ["Long-term (sec 112A)", b])];
    h += `<div class="card" style="margin-top:14px"><div class="card-h"><span class="card-t">Indian equity · tax computation</span></div><div class="table-wrap"><table>
      <tr><th>Type</th><th class="r">Rate</th><th class="r">Gains</th><th class="r">After set-off</th><th class="r">Exempt</th><th class="r">Taxable</th><th class="r">Tax</th></tr>
      ${rows.map(([l, b]) => `<tr><td>${l}</td><td class="r num">${(b.rate * 100).toFixed(1)}%</td><td class="r num">${inr(b.gains)}</td><td class="r num">${inr(b.after_setoff)}</td><td class="r num">${inr(b.exempt)}</td><td class="r num">${inr(b.taxable)}</td><td class="r num">${inr(b.tax)}</td></tr>`).join("")}
      <tr><td colspan="6" class="muted">Health & education cess (4%)</td><td class="r num">${inr(ind.cess)}</td></tr>
      <tr><td colspan="6"><b>Total</b></td><td class="r num"><b>${inr(ind.total_tax)}</b></td></tr></table></div></div>`;
  }
  rep.vda.forEach((v) => (h += `<div class="card"><div class="card-h"><span class="card-t">Crypto (VDA) · ${esc(v.currency)}</span></div>
    <div class="stats"><div class="stat"><div class="k">Gains</div><div class="v num">${money(v.gains, v.currency)}</div></div><div class="stat"><div class="k">Losses (not set off)</div><div class="v num">${money(v.losses_ignored, v.currency)}</div></div>
    <div class="stat"><div class="k">Tax @30% + cess</div><div class="v num">${v.total_tax == null ? "Convert to INR first" : money(v.total_tax, "INR", 0)}</div></div></div></div>`));
  rep.foreign.forEach((f) => (h += `<div class="card"><div class="card-h"><span class="card-t">Foreign shares · ${esc(f.currency)}</span></div>
    <div class="stats"><div class="stat"><div class="k">Short-term gains</div><div class="v num">${money(f.stcg, f.currency)}</div></div><div class="stat"><div class="k">Long-term gains</div><div class="v num">${money(f.ltcg, f.currency)}</div></div></div><div class="fine">${esc(f.note)}</div></div>`));
  if (t.fy === plan.fy) {
    h += `<div class="grid2" style="margin-top:14px"><div class="card"><div class="card-h"><span class="card-t">LTCG exemption left this year</span><span class="num" style="font-weight:800;font-size:18px">${inr(plan.exemption_remaining)}</span></div>
      <div class="muted" style="font-size:13px;margin-bottom:8px">Long-term gains up to this amount are tax-free in ${esc(plan.fy)}. Holdings with unrealized long-term gains:</div>
      ${plan.harvest_ideas.length ? plan.harvest_ideas.slice(0, 6).map((i) => `<div class="flag" style="grid-template-columns:1fr auto"><span class="cell-sym">${symHtml(i.symbol)} <span class="muted" style="font-weight:400">· ${fmt(i.quantity, 0)} sh</span></span><span class="num up">${inr(i.unrealized_gain)}</span></div>`).join("") : `<div class="muted">None right now.</div>`}</div>
      <div class="card"><div class="card-h"><span class="card-t">Turning long-term within 30 days</span></div>
      ${plan.turning_long_term_soon.length ? plan.turning_long_term_soon.map((s) => `<div class="flag" style="grid-template-columns:1fr auto"><span class="cell-sym">${symHtml(s.symbol)} <span class="muted" style="font-weight:400">· ${fmt(s.quantity, 0)} sh on ${esc(dateTxt(s.turns_long_term_on))}</span></span><span class="num">${s.days_left}d</span></div>`).join("")
        : `<div class="muted" style="font-size:13px">No lots cross the 12-month mark in the next 30 days. Selling a lot before then is taxed as short-term.</div>`}</div></div>`;
  }
  if (rep.lines.length) {
    h += `<div class="card"><details><summary>Realized trades in ${esc(t.fy)} (${rep.lines.length})</summary><div class="table-wrap" style="margin-top:8px"><table>
      <tr><th>Instrument</th><th>Type</th><th class="r">Qty</th><th>Bought</th><th>Sold</th><th class="r">Held</th><th class="r">Cost</th><th class="r">Proceeds</th><th class="r">Gain</th></tr>
      ${rep.lines.map((l) => `<tr><td class="cell-sym">${symHtml(l.symbol)}</td><td><span class="pill">${l.term === "vda" ? "VDA" : l.term === "long" ? "LTCG" : "STCG"}</span></td><td class="r num">${fmt(l.quantity, 0)}</td>
        <td class="num">${esc(l.buy_date)}</td><td class="num">${esc(l.sell_date)}</td><td class="r num">${l.holding_days}d</td><td class="r num">${money(l.cost, l.currency)}</td><td class="r num">${money(l.proceeds, l.currency)}</td><td class="r num ${tone(l.gain)}">${signedMoney(l.gain, l.currency)}</td></tr>`).join("")}
      </table></div></details></div>`;
  }
  h += `<div class="fine">${rep.notes.map(esc).join("<br>")}</div>`;
  $("#pfSub").innerHTML = h;
  $("#fySel").onchange = (e) => loadTax(e.target.value);
}

// ------------------------------------------------------------------ health
async function loadHealth() {
  const cur = P.cur || "INR";
  if (P.health[cur]) return renderHealth(P.health[cur]);
  $("#pfSub").innerHTML = `<div class="card"><div class="muted" style="margin-bottom:10px">Checking diversification, sectors and one year of price history…</div>${"<div class='skeleton' style='height:14px;margin:8px 0'></div>".repeat(5)}</div>`;
  try { P.health[cur] = await api(`/portfolio/health?currency=${cur}`); renderHealth(P.health[cur]); }
  catch (err) { $("#pfSub").innerHTML = `<div class="card empty" style="height:120px">${esc(err.message)}</div>`; }
}
function renderHealth(r) {
  const ringCol = r.score >= 85 ? "var(--up)" : r.score >= 55 ? "var(--warn)" : "var(--down)";
  const C = 2 * Math.PI * 34, m = r.metrics;
  const metric = (k, v) => `<div class="stat"><div class="k">${k}</div><div class="v num">${v}</div></div>`;
  $("#pfSub").innerHTML = `<div class="grid2">
    <div class="card"><div class="score"><svg width="88" height="88" viewBox="0 0 88 88"><circle cx="44" cy="44" r="34" fill="none" stroke="var(--panel-2)" stroke-width="9"/>
      <circle cx="44" cy="44" r="34" fill="none" stroke="${ringCol}" stroke-width="9" stroke-linecap="round" stroke-dasharray="${C * r.score / 100} ${C}" transform="rotate(-90 44 44)"/>
      <text x="44" y="50" text-anchor="middle" font-size="22" font-weight="800" fill="var(--text)">${r.grade}</text></svg>
      <div><div class="score-n">${r.score}<span class="muted" style="font-size:15px;font-weight:600"> / 100</span></div>
      <div class="muted" style="font-size:13px">${plural(r.checks.filter((c) => c.status === "bad").length, "issue")} · ${plural(r.checks.filter((c) => c.status === "warn").length, "warning")} · ${plural(r.metrics.holdings, `${esc(r.currency)} holding`)}</div></div></div>
      <div class="stats" style="margin-top:16px">${metric("Volatility", m.volatility_pct == null ? "—" : m.volatility_pct.toFixed(1) + "%")}${metric("Beta", m.beta == null ? "—" : m.beta.toFixed(2))}
      ${metric("Worst fall (1Y)", m.max_drawdown_pct == null ? "—" : m.max_drawdown_pct.toFixed(1) + "%")}${metric("Effective holdings", m.effective_holdings ?? "—")}</div></div>
    <div class="card"><div class="card-h"><span class="card-t">Sector mix</span></div><div class="bars">${r.sectors.map((s) => `<div class="b"><span title="${esc(s.symbols.join(", "))}">${esc(s.sector)}</span><div class="track"><i style="width:${s.weight}%"></i></div><span class="num r">${s.weight.toFixed(1)}%</span></div>`).join("")}</div></div></div>
    <div class="card"><div class="card-h"><span class="card-t">Checks</span><span class="muted" style="font-size:12px">Each issue costs 20 points, each warning 8</span></div>
    ${r.checks.map((c) => `<div class="check"><span class="dot ${c.status}"></span><div><div class="t">${esc(c.title)}</div><div class="d">${esc(c.detail)}</div></div><div class="v num">${esc(c.value)}</div></div>`).join("")}</div>
    <div class="fine">${r.notes.map(esc).join("<br>")}</div>`;
}

// ------------------------------------------------------------------ transactions
async function loadTxns() {
  try { P.txns = await api("/portfolio/transactions"); } catch (err) { $("#pfSub").innerHTML = `<div class="card" style="color:var(--down)">${esc(err.message)}</div>`; return; }
  $("#pfSub").innerHTML = `<div class="card"><div class="card-h"><span class="card-t">Transactions</span><button class="btn sm ghost danger" id="clearAll">Delete all</button></div>
    <div class="table-wrap"><table id="txTable"><tr><th>Date</th><th>Instrument</th><th>Side</th><th class="r">Qty</th><th class="r">Price</th><th class="r hide-sm">Charges</th><th class="r">Amount</th><th class="hide-sm">Source</th><th></th></tr>
    ${P.txns.map((t) => `<tr><td class="num">${esc(t.trade_date)}</td><td class="cell-sym">${symHtml(t.symbol)}</td><td><span class="pill ${t.side === "buy" ? "up" : "down"}">${t.side.toUpperCase()}</span></td>
      <td class="r num">${fmt(t.quantity, t.quantity % 1 ? 4 : 0)}</td><td class="r num">${money(t.price, t.currency)}</td><td class="r num hide-sm">${money(t.fees, t.currency)}</td>
      <td class="r num">${money(t.quantity * t.price, t.currency)}</td><td class="muted hide-sm">${esc(t.source)}</td><td class="r"><button class="btn sm ghost danger" data-del="${t.id}" aria-label="Delete">✕</button></td></tr>`).join("")}
    </table></div></div>`;
  $("#txTable").onclick = async (e) => {
    const b = e.target.closest("[data-del]"); if (!b) return;
    try { await api(`/portfolio/transactions/${b.dataset.del}`, { method: "DELETE" }); toast("Trade deleted"); await reloadAll(); } catch (err) { toast("Couldn't delete", err.message, "err"); }
  };
  $("#clearAll").onclick = () => modal("Delete all transactions?", `<p class="muted">This removes all ${P.txns.length} trades from this computer. It can't be undone.</p>
    <div class="modal-f"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="confirmClear" style="background:var(--down);border-color:var(--down)">Delete all</button></div>`,
    (root, close) => { root.querySelector("#confirmClear").onclick = async () => { await api("/portfolio/transactions?confirm=true", { method: "DELETE" }); close(); toast("All trades deleted"); reloadAll(); }; });
}

// ------------------------------------------------------------------ dialogs
export function openAddTrade(prefill = {}) {
  const today = new Date().toISOString().slice(0, 10);
  const fund = prefill.units || isMf(prefill.symbol);
  modal(prefill.label ? `Add ${prefill.label}` : "Add a trade", `<form id="tradeForm"><div class="form-grid">
    <div class="field wide"><label>Side</label><div class="side-toggle"><button type="button" class="buy on" data-side="buy">Buy</button><button type="button" class="sell" data-side="sell">Sell</button></div></div>
    <div class="field"><label>${fund ? "Fund (AMFI code)" : "Symbol"}</label><input name="symbol" placeholder="RELIANCE.NS or MF122639" required autocomplete="off" value="${esc(prefill.symbol || "")}"></div>
    <div class="field"><label>Trade date</label><input name="trade_date" type="date" value="${today}" max="${today}" required></div>
    <div class="field"><label>${fund ? "Units" : "Quantity"}</label><input name="quantity" type="number" step="any" min="0" required></div>
    <div class="field"><label>${fund ? "NAV per unit" : "Price per share"}</label><input name="price" type="number" step="any" min="0" required value="${prefill.price ?? ""}"></div>
    <div class="field"><label>Charges (brokerage, taxes)</label><input name="fees" type="number" step="any" min="0" value="0"></div>
    <div class="field"><label>Note</label><input name="note" maxlength="200"></div></div>
    <div class="fine">${fund ? "Use the NAV on your purchase date and the units from your statement (stamp duty can go in charges)."
      : "NSE stocks end in .NS and BSE in .BO (e.g. TCS.NS). US stocks use their ticker (AAPL). Mutual funds: MF + AMFI code (e.g. MF122639)."}</div>
    <div class="form-err" id="tradeErr"></div>
    <div class="modal-f"><button type="button" class="btn ghost" data-close>Cancel</button><button class="btn primary">Save trade</button></div></form>`,
  (root, close) => {
    const form = root.querySelector("form"); let side = "buy";
    root.querySelectorAll("[data-side]").forEach((b) => (b.onclick = () => { side = b.dataset.side; root.querySelectorAll("[data-side]").forEach((x) => x.classList.toggle("on", x === b)); }));
    form.onsubmit = async (e) => {
      e.preventDefault();
      const btn = form.querySelector("button.primary"); btn.disabled = true;
      try {
        await api("/portfolio/transactions", { method: "POST", body: { symbol: form.symbol.value.trim().toUpperCase(), side, quantity: Number(form.quantity.value),
          price: Number(form.price.value), fees: Number(form.fees.value || 0), trade_date: form.trade_date.value, note: form.note.value.trim() } });
        close(); toast("Trade saved", `${side.toUpperCase()} ${form.quantity.value} ${form.symbol.value.toUpperCase()}`); reloadAll();
      } catch (err) { $("#tradeErr").textContent = err.message; btn.disabled = false; }
    };
  });
}

export function openImport() {
  const sample = "date,symbol,side,quantity,price,fees\n2025-01-15,RELIANCE.NS,buy,10,1250.50,20\n2025-03-02,TCS.NS,buy,5,3900,15\n2025-08-20,RELIANCE.NS,sell,4,1420,12";
  modal("Import trades from CSV", `<div class="field"><label>Choose a file — Zerodha tradebook or the simple format below</label><input type="file" id="csvFile" accept=".csv,text/csv"></div>
    <div class="field" style="margin-top:10px"><label>…or paste CSV</label><textarea id="csvText" placeholder="${esc(sample)}"></textarea></div>
    <div class="fine">Columns: <b>date, symbol, side, quantity, price</b> [, fees, exchange]. Zerodha's tradebook is detected automatically (equity rows only).</div>
    <div id="csvPreview" style="margin-top:12px"></div><div class="form-err" id="csvErr"></div>
    <div class="modal-f"><button class="btn ghost" data-close>Cancel</button><button class="btn" id="csvCheck">Preview</button><button class="btn primary" id="csvGo" disabled>Import</button></div>`,
  (root, close) => {
    const text = root.querySelector("#csvText"), go = root.querySelector("#csvGo");
    root.querySelector("#csvFile").onchange = async (e) => { const f = e.target.files[0]; if (f) { text.value = await f.text(); preview(); } };
    text.oninput = () => (go.disabled = true);
    async function preview() {
      $("#csvErr").textContent = ""; go.disabled = true;
      if (!text.value.trim()) return;
      $("#csvPreview").innerHTML = `<div class="skeleton" style="height:40px"></div>`;
      try {
        const r = await api("/portfolio/import", { method: "POST", body: { csv: text.value, dry_run: true } });
        $("#csvPreview").innerHTML = `<div class="notice" style="border-color:var(--border-2);background:var(--panel-2)"><b>${r.rows.length} trade${r.rows.length === 1 ? "" : "s"} ready</b> · format: ${esc(r.format)}${r.skipped ? ` · ${r.skipped} non-equity rows skipped` : ""}
          ${r.errors.length ? `<div style="margin-top:6px;color:var(--warn)">${r.errors.slice(0, 6).map((e) => `${e.line ? `Line ${e.line}: ` : ""}${esc(e.message)}`).join("<br>")}${r.errors.length > 6 ? `<br>…and ${r.errors.length - 6} more` : ""}</div>` : ""}</div>`;
        go.disabled = !r.rows.length; go.textContent = `Import ${r.rows.length}`;
      } catch (err) { $("#csvPreview").innerHTML = ""; $("#csvErr").textContent = err.message; }
    }
    root.querySelector("#csvCheck").onclick = preview;
    go.onclick = async () => {
      go.disabled = true;
      try { const r = await api("/portfolio/import", { method: "POST", body: { csv: text.value } }); close(); toast("Import complete", `${r.imported} trades added`); reloadAll(); }
      catch (err) { $("#csvErr").textContent = err.message; go.disabled = false; }
    };
  });
}

// ------------------------------------------------------------------ routing
async function reloadAll() { P.tax = null; P.health = {}; P.txns = null; await load(); }
function renderSub() {
  if (!$("#pfSub")) return;
  if (P.sub === "holdings") renderHoldings();
  else if (P.sub === "tax") loadTax(P.taxFy);
  else if (P.sub === "health") loadHealth();
  else if (P.sub === "transactions") loadTxns();
}
export function initPortfolio() {
  $("#addTradeBtn").onclick = openAddTrade;
  $("#importBtn").onclick = openImport;
  $("#pfCurSeg").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; P.cur = b.dataset.cur; render(); };
}
export function showPortfolio(sub) {
  P.sub = ["holdings", "tax", "health", "transactions"].includes(sub) ? sub : "holdings";
  if (!P.data) load(); else { render(); startLive(); }
}
export const heldSymbols = () => (P.data?.positions ?? []).map((p) => p.symbol);
export async function ensurePortfolio() { if (!P.data) { try { P.data = await api("/portfolio"); startLive(); } catch { /* ignore */ } } return heldSymbols(); }
