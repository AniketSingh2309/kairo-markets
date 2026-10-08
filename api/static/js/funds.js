// Mutual funds: search, category explorer, calculators, fund detail, compare.
import { $, esc, api, store, fmt, money, pctTxt, tone, dateTxt, css, toast, navigate, inrShort } from "./core.js";
import { openAddTrade } from "./portfolio.js";
import { openAlertDialog } from "./alerts.js";

const ASSET_ORDER = ["Equity", "Hybrid", "Index & ETF", "Debt", "FoF", "Solution", "Other"];
const LINE_COLORS = ["#7c5cff", "#1cc47e", "#4ea1ff", "#e9a23b"];
const F = {
  categories: [], asset: "Equity", cat: store.get("kairo-fund-cat", "Equity Scheme - Flexi Cap Fund"), period: "3Y", plan: "Direct",
  q: "", results: null, top: null, detail: null, range: "3Y", sip: { amount: 10000, years: 5, step: 0 }, sipRes: null,
  compare: store.get("kairo-compare", []), calc: { mode: "sip", amount: 10000, years: 10, rate: 12, step: 10 },
};
let searchTimer = 0;
const saveCompare = () => store.set("kairo-compare", F.compare);
const pct = (v, d = 2) => (v == null ? "—" : pctTxt(v, d));
const quartileCls = (q) => (q === 1 ? "up" : q === 4 ? "down" : "");

// ============================================================ shared line chart
function lineChart(box, series, { height = 300, rebased = false, labels = [] } = {}) {
  const W = box.clientWidth || 600, H = height, M = { l: 8, r: 64, t: 14, b: 24 };
  const all = series.flatMap((s) => s.points.map((p) => p[1]));
  if (!all.length) { box.innerHTML = `<div class="empty" style="height:${H}px">No data</div>`; return; }
  const t0 = Math.min(...series.map((s) => s.points[0][0])), t1 = Math.max(...series.map((s) => s.points[s.points.length - 1][0]));
  let lo = Math.min(...all), hi = Math.max(...all); const pad = (hi - lo || hi * 0.01) * 0.08;
  lo = all.every((v) => v >= 0) ? Math.max(0, lo - pad) : lo - pad; hi += pad;  // NAVs / growth of 100 can't go below 0
  const x = (t) => M.l + (t - t0) / Math.max(1, t1 - t0) * (W - M.l - M.r), y = (v) => M.t + (1 - (v - lo) / (hi - lo)) * (H - M.t - M.b);
  let g = "";
  for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4; g += `<line x1="${M.l}" x2="${W - M.r}" y1="${y(v)}" y2="${y(v)}" stroke="${css("--grid")}"/><text x="${W - M.r + 8}" y="${y(v) + 4}" font-size="11" fill="${css("--axis")}">${fmt(v, rebased ? 0 : 2)}</text>`; }
  for (let k = 0; k <= 4; k++) { const t = t0 + (t1 - t0) * k / 4; g += `<text x="${x(t)}" y="${H - 6}" font-size="11" fill="${css("--axis")}" text-anchor="${k === 0 ? "start" : k === 4 ? "end" : "middle"}">${new Date(t).toLocaleDateString([], { month: "short", year: "2-digit" })}</text>`; }
  series.forEach((s, i) => {
    const d = s.points.map((p, j) => `${j ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
    if (series.length === 1) {
      const last = s.points[s.points.length - 1], first = s.points[0];
      const col = last[1] >= first[1] ? css("--up") : css("--down");
      g += `<defs><linearGradient id="fg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".22"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
        <path d="${d}L${x(last[0])},${H - M.b}L${x(first[0])},${H - M.b}Z" fill="url(#fg)"/><path d="${d}" fill="none" stroke="${col}" stroke-width="1.8"/>`;
    } else g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="1.8"/>`;
  });
  box.innerHTML = `<svg width="${W}" height="${H}" style="display:block">${g}<g id="xh"></g><rect x="${M.l}" y="0" width="${W - M.l - M.r}" height="${H - M.b}" fill="transparent"/></svg>`;
  const svg = box.querySelector("svg"), xh = svg.querySelector("#xh");
  svg.querySelector("rect").addEventListener("mousemove", (e) => {
    const mx = e.clientX - svg.getBoundingClientRect().left;
    const t = t0 + (mx - M.l) / (W - M.l - M.r) * (t1 - t0);
    let out = `<line x1="${mx}" x2="${mx}" y1="${M.t}" y2="${H - M.b}" stroke="${css("--muted")}" stroke-dasharray="3,3"/>`;
    const rows = series.map((s, i) => {
      let best = s.points[0]; for (const p of s.points) { if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p; }
      out += `<circle cx="${x(best[0])}" cy="${y(best[1])}" r="3.5" fill="${series.length > 1 ? s.color : css("--text")}"/>`;
      return `${labels[i] ? esc(labels[i]) + ": " : ""}${fmt(best[1], rebased ? 1 : 4)}`;
    });
    const bx = Math.min(Math.max(mx + 8, M.l), W - M.r - 170);
    out += `<rect x="${bx}" y="${M.t}" width="168" height="${18 + rows.length * 15}" rx="6" fill="${css("--panel-2")}" stroke="${css("--border-2")}"/>
      <text x="${bx + 8}" y="${M.t + 14}" font-size="11" fill="${css("--muted")}">${new Date(t).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })}</text>` +
      rows.map((r, i) => `<text x="${bx + 8}" y="${M.t + 29 + i * 15}" font-size="11.5" font-weight="600" fill="${series.length > 1 ? series[i].color : css("--text")}">${r.slice(0, 26)}</text>`).join("");
    xh.innerHTML = out;
  });
  svg.querySelector("rect").addEventListener("mouseleave", () => (xh.innerHTML = ""));
}

// ============================================================ home
async function loadCategories() {
  if (F.categories.length) return;
  try { F.categories = await api("/funds/categories"); } catch (err) { toast("Couldn't load fund categories", err.message, "err"); }
  const c = F.categories.find((x) => x.category === F.cat);
  if (c) F.asset = c.asset_class;
}
async function loadTop() {
  const key = `${F.cat}|${F.period}|${F.plan}`;
  F.top = { key, loading: true };
  renderTop();
  try {
    const body = await api(`/funds/top?category=${encodeURIComponent(F.cat)}&period=${F.period}&plan=${F.plan}&limit=30`);
    if (F.top.key === key) F.top = { key, ...body };
  } catch (err) { F.top = { key, error: err.message }; }
  renderTop();
}
function renderHome() {
  const box = $("#fundsBody");
  box.innerHTML = `<div class="funds-search"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input id="fundQ" placeholder="Search 14,000+ mutual funds — e.g. parag flexi, hdfc mid cap, nifty 50 index" value="${esc(F.q)}" autocomplete="off"></div>
    <div id="fundResults"></div>
    <div class="ex-grid" id="fundHomeGrid">
      <div class="ex-main"><div class="card" id="fundTop"></div></div>
      <div class="ex-rail"><div class="card" id="fundCalc"></div></div>
    </div>`;
  $("#fundQ").oninput = (e) => { F.q = e.target.value; clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 220); };
  if (F.q) runSearch();
  renderTop(); renderCalc();
}
async function runSearch() {
  const q = F.q.trim(), box = $("#fundResults");
  $("#fundHomeGrid").hidden = !!q;
  if (!q) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="card"><div class="skeleton" style="height:120px"></div></div>`;
  try { F.results = await api(`/funds/search?q=${encodeURIComponent(q)}&limit=40`); } catch (err) { box.innerHTML = `<div class="card muted">${esc(err.message)}</div>`; return; }
  if (q !== F.q.trim()) return;
  const r = F.results;
  box.innerHTML = `<div class="card"><div class="card-h"><span class="card-t">${r.total} fund${r.total === 1 ? "" : "s"} match</span><span class="muted" style="font-size:12px">Direct plans, growth option</span></div>
    ${r.items.length ? fundTable(r.items, { showRank: false }) : `<div class="muted">No direct-growth funds match “${esc(q)}”.</div>`}</div>`;
  wireFundTable(box);
}
function fundTable(items, { showRank }) {
  return `<div class="table-wrap"><table class="fund-table"><tr>${showRank ? "<th>#</th>" : ""}<th>Fund</th><th class="r">1Y</th><th class="r">3Y CAGR</th><th class="r hide-sm">5Y CAGR</th><th class="r hide-sm">NAV</th><th></th></tr>
    ${items.map((f) => `<tr class="click" data-code="${esc(f.code)}">${showRank ? `<td class="num muted">${f.rank}</td>` : ""}
      <td><div class="cell-sym" style="white-space:normal">${esc(f.name)}</div><div class="cell-name">${esc(f.amc || "")} · ${esc(f.sub_category || "")}</div></td>
      <td class="r num ${tone(f.returns["1Y"])}">${pct(f.returns["1Y"])}</td><td class="r num ${tone(f.returns["3Y"])}">${pct(f.returns["3Y"])}</td>
      <td class="r num hide-sm ${tone(f.returns["5Y"])}">${pct(f.returns["5Y"])}</td><td class="r num hide-sm">${fmt(f.nav, 2)}</td>
      <td class="r"><button class="btn sm ghost" data-cmp="${esc(f.code)}" title="Add to compare">${F.compare.includes(f.code) ? "✓" : "+"} Compare</button></td></tr>`).join("")}</table></div>`;
}
function wireFundTable(root) {
  root.onclick = (e) => {
    const c = e.target.closest("[data-cmp]");
    if (c) { e.stopPropagation(); toggleCompare(c.dataset.cmp); c.textContent = `${F.compare.includes(c.dataset.cmp) ? "✓" : "+"} Compare`; return; }
    const tr = e.target.closest("tr[data-code]"); if (tr) navigate("funds", tr.dataset.code);
  };
}
function toggleCompare(code) {
  if (F.compare.includes(code)) F.compare = F.compare.filter((c) => c !== code);
  else if (F.compare.length >= 4) { toast("Compare holds up to 4 funds", "Remove one first."); return; }
  else F.compare = [...F.compare, code];
  saveCompare(); renderCompareBtn();
}
function renderCompareBtn() {
  const b = $("#compareBtn");
  b.hidden = F.compare.length < 2;
  b.textContent = `Compare ${F.compare.length} funds`;
}
function renderTop() {
  const box = $("#fundTop"); if (!box) return;
  const assets = ASSET_ORDER.filter((a) => F.categories.some((c) => c.asset_class === a));
  const cats = F.categories.filter((c) => c.asset_class === F.asset);
  const sub = F.categories.find((c) => c.category === F.cat);
  let html = `<div class="card-h"><div><div class="card-t">Top funds by category</div><div class="muted" style="font-size:12px">Ranked on point-to-point returns from official AMFI NAVs</div></div>
      <div class="actions"><div class="seg" id="fPeriod">${["1Y", "3Y", "5Y"].map((p) => `<button data-p="${p}" class="${p === F.period ? "on" : ""}">${p}</button>`).join("")}</div>
      <div class="seg" id="fPlan">${["Direct", "Regular"].map((p) => `<button data-pl="${p}" class="${p === F.plan ? "on" : ""}">${p}</button>`).join("")}</div></div></div>
    <div class="seg" id="fAsset" style="margin-bottom:10px">${assets.map((a) => `<button data-a="${esc(a)}" class="${a === F.asset ? "on" : ""}">${esc(a)}</button>`).join("")}</div>
    <div class="chips" id="fCats" style="margin-bottom:12px">${cats.slice(0, 18).map((c) => `<button data-c="${esc(c.category)}" class="${c.category === F.cat ? "on" : ""}">${esc(c.sub_category)} <span class="faint">${c.funds}</span></button>`).join("")}</div>`;
  const t = F.top;
  if (!t || t.loading) html += "<div class='skeleton' style='height:44px;margin:6px 0'></div>".repeat(6) + `<div class="fine">First load downloads three AMFI NAV snapshots to rank every fund — a few seconds.</div>`;
  else if (t.error) html += `<div class="muted">${esc(t.error)}</div>`;
  else html += `<div class="muted" style="font-size:12.5px;margin-bottom:8px">${esc(sub?.sub_category || F.cat)} · ${t.with_history} of ${t.funds} funds have ${F.period} of history · category median <b class="num ${tone(t.median)}">${pct(t.median)}</b></div>` +
    (t.items.length ? fundTable(t.items, { showRank: true }) : `<div class="muted">No fund in this category has ${F.period} of history yet.</div>`);
  box.innerHTML = html;
  box.querySelector("#fPeriod").onclick = (e) => { const b = e.target.closest("button"); if (b) { F.period = b.dataset.p; loadTop(); } };
  box.querySelector("#fPlan").onclick = (e) => { const b = e.target.closest("button"); if (b) { F.plan = b.dataset.pl; loadTop(); } };
  box.querySelector("#fAsset").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    F.asset = b.dataset.a; const first = F.categories.find((c) => c.asset_class === F.asset);
    if (first) { F.cat = first.category; store.set("kairo-fund-cat", F.cat); loadTop(); }
  };
  box.querySelector("#fCats").onclick = (e) => { const b = e.target.closest("button"); if (b) { F.cat = b.dataset.c; store.set("kairo-fund-cat", F.cat); loadTop(); } };
  const tbl = box.querySelector(".table-wrap"); if (tbl) wireFundTable(tbl);
}

// ------------------------------------------------------------ calculators (projection, not history)
function project({ mode, amount, years, rate, step }) {
  const r = rate / 100 / 12, months = Math.round(years * 12);
  let invested = 0, value = 0, monthly = amount;
  const rows = [];
  if (mode === "lumpsum") {
    invested = amount;
    for (let y = 1; y <= years; y++) rows.push([y, amount, amount * (1 + rate / 100) ** y]);
    value = amount * (1 + rate / 100) ** years;
  } else {
    for (let m = 1; m <= months; m++) {
      if (mode === "stepup" && m > 1 && (m - 1) % 12 === 0) monthly *= 1 + step / 100;
      value = (value + monthly) * (1 + r); invested += monthly;
      if (m % 12 === 0) rows.push([m / 12, invested, value]);
    }
  }
  return { invested, value, rows };
}
function renderCalc() {
  const box = $("#fundCalc"); if (!box) return;
  const c = F.calc, res = project(c), gains = res.value - res.invested;
  const field = (k, label, min, max, stepv, suffix = "") => `<div class="calc-f"><div class="calc-l"><span>${label}</span><b class="num">${k === "amount" ? money(c[k], "INR", 0) : c[k] + suffix}</b></div>
    <input type="range" data-k="${k}" min="${min}" max="${max}" step="${stepv}" value="${c[k]}"></div>`;
  const maxV = Math.max(...res.rows.map((r) => r[2]), 1);
  box.innerHTML = `<div class="card-h"><span class="card-t">Calculator</span><div class="seg" id="calcMode">${[["sip", "SIP"], ["stepup", "Step-up SIP"], ["lumpsum", "Lumpsum"]].map(([k, l]) => `<button data-m="${k}" class="${k === c.mode ? "on" : ""}">${l}</button>`).join("")}</div></div>
    ${field("amount", c.mode === "lumpsum" ? "Investment" : "Monthly SIP", c.mode === "lumpsum" ? 10000 : 500, c.mode === "lumpsum" ? 5000000 : 200000, c.mode === "lumpsum" ? 10000 : 500)}
    ${field("years", "Years", 1, 40, 1, " yrs")}${field("rate", "Expected return (p.a.)", 1, 30, 0.5, "%")}${c.mode === "stepup" ? field("step", "Yearly step-up", 0, 50, 1, "%") : ""}
    <div class="calc-out"><div><div class="k">Invested</div><div class="v num">${inrShort(res.invested)}</div></div><div><div class="k">Est. gains</div><div class="v num up">${inrShort(gains)}</div></div><div><div class="k">Total value</div><div class="v num">${inrShort(res.value)}</div></div></div>
    <div class="calc-bars">${res.rows.filter((_, i, a) => a.length <= 20 || i % Math.ceil(a.length / 20) === 0 || i === a.length - 1).map((r) => `<div title="Year ${r[0]}: ${inrShort(r[2])}"><i style="height:${r[2] / maxV * 100}%"></i><b style="height:${r[1] / maxV * 100}%"></b></div>`).join("")}</div>
    <div class="fine" style="margin-top:6px"><span class="calc-key inv"></span> invested <span class="calc-key val"></span> value · A projection at a constant return — real returns vary year to year. For a fund's actual history use its SIP back-test.</div>`;
  box.querySelector("#calcMode").onclick = (e) => { const b = e.target.closest("button"); if (b) { c.mode = b.dataset.m; if (c.mode === "lumpsum" && c.amount < 10000) c.amount = 100000; if (c.mode !== "lumpsum" && c.amount > 200000) c.amount = 10000; renderCalc(); } };
  box.querySelectorAll("input[type=range]").forEach((inp) => (inp.oninput = () => { c[inp.dataset.k] = Number(inp.value); renderCalc(); box.querySelector(`input[data-k="${inp.dataset.k}"]`)?.focus(); }));
}

// ============================================================ detail
async function loadDetail(code) {
  $("#fundsBody").innerHTML = `<div class="card"><div class="skeleton" style="height:60px;margin-bottom:12px"></div><div class="skeleton" style="height:300px"></div></div>`;
  try { F.detail = await api(`/funds/${encodeURIComponent(code)}`); } catch (err) {
    $("#fundsBody").innerHTML = `<div class="card empty-state"><h3>Fund not found</h3><p>${esc(err.message)}</p><div class="actions"><a class="btn" href="#/funds">Back to funds</a></div></div>`; return;
  }
  document.title = `${F.detail.name} · Kairo Markets`;
  renderDetail(); loadSip();
}
function navSeries(range) {
  const pts = F.detail.nav_series.map(([d, n]) => [Date.parse(d), n]);
  const yrs = { "1M": 1 / 12, "6M": 0.5, "1Y": 1, "3Y": 3, "5Y": 5, ALL: 100 }[range];
  const cut = pts[pts.length - 1][0] - yrs * 365.25 * 864e5;
  return pts.filter((p) => p[0] >= cut);
}
function renderDetail() {
  const d = F.detail, r = d.returns, rk = Object.fromEntries(d.ranks.map((x) => [x.period, x])), risk = d.risk;
  const series = d.nav_series, last = series[series.length - 1];
  const chg = d.day_change_pct;
  const pts = navSeries(F.range), rangeRet = pts.length > 1 ? (pts[pts.length - 1][1] / pts[0][1] - 1) * 100 : null;
  const taxLabel = { equity: "Taxed like equity", debt: "Debt: slab rate (sec 50AA)", other: "24-month rule, 12.5% LTCG" }[d.classification.tax_kind];
  const roll = (k) => d.rolling[k];
  $("#fundsBody").innerHTML = `
    <div class="inst" style="margin-bottom:18px"><div style="min-width:0">
      <div class="cell-name" style="max-width:none;font-size:13px">${esc(d.amc || "")}</div>
      <h1 class="page-title" style="font-size:22px;margin:2px 0 8px">${esc(d.name)}</h1>
      <div class="inst-id"><span class="chip">${esc(d.sub_category)}</span><span class="chip">${esc(d.plan || "")} · ${esc(d.option || "")}</span>
        <span class="chip ${d.classification.tax_kind === "equity" ? "good" : ""}" title="${d.classification.tax_inferred ? "Inferred from the AMFI category / fund name — verify" : "From the AMFI category"}">${esc(taxLabel)}${d.classification.tax_inferred ? " *" : ""}</span></div>
      <div class="quote"><span class="big-px num">₹${fmt(last[1], 4)}</span><span class="big-chg num ${tone(chg)}">${pct(chg)} 1D</span></div>
      <div class="asof">NAV as of ${esc(dateTxt(last[0]))} · AMFI scheme code ${esc(d.code)}</div></div>
      <div class="actions"><button class="btn" id="fAlert">Set alert</button><button class="btn" id="fCmp">${F.compare.includes(d.code) ? "✓ In compare" : "+ Compare"}</button><button class="btn primary" id="fAdd">Add to portfolio</button></div></div>
    <div class="card"><div class="card-h"><div><span class="card-t">NAV</span> <span class="num ${tone(rangeRet)}" style="font-weight:700;margin-left:8px">${pct(rangeRet)}</span> <span class="muted" style="font-size:12px">over ${F.range === "ALL" ? "fund life" : F.range}</span></div>
      <div class="seg" id="fRange">${["1M", "6M", "1Y", "3Y", "5Y", "ALL"].map((x) => `<button data-r="${x}" class="${x === F.range ? "on" : ""}">${x}</button>`).join("")}</div></div><div id="navChart"></div></div>
    <div class="grid2">
      <div class="card"><div class="card-h"><span class="card-t">Returns</span><span class="muted" style="font-size:12px">Up to 1Y absolute · 3Y+ annualised (CAGR)</span></div>
        <div class="table-wrap"><table><tr><th>Period</th><th class="r">Return</th><th class="r">Category rank</th><th class="r hide-sm">Category median</th></tr>
        ${Object.entries(r.periods).map(([k, v]) => `<tr><td>${k}</td><td class="r num ${tone(v)}"><b>${pct(v)}</b></td>
          <td class="r">${rk[k] ? `<span class="pill ${quartileCls(rk[k].quartile)}">Q${rk[k].quartile}</span> <span class="num">#${rk[k].rank} of ${rk[k].of}</span>` : `<span class="faint">—</span>`}</td>
          <td class="r num hide-sm">${rk[k] ? pct(rk[k].category_median) : "—"}</td></tr>`).join("")}
        <tr><td>Since launch</td><td class="r num ${tone(r.since_inception_cagr)}"><b>${pct(r.since_inception_cagr)}</b></td><td class="r faint" colspan="2">launched ${esc(dateTxt(r.inception_date))}</td></tr></table></div></div>
      <div class="card"><div class="card-h"><span class="card-t">Risk</span><span class="muted" style="font-size:12px">From daily NAVs</span></div>
        <div class="stats" style="grid-template-columns:repeat(2,minmax(0,1fr))">
          <div class="stat"><div class="k">Volatility (3Y, annualised)</div><div class="v num">${risk.volatility_pct == null ? "—" : risk.volatility_pct.toFixed(1) + "%"}</div></div>
          <div class="stat"><div class="k">Worst fall ever</div><div class="v num down">${risk.max_drawdown_pct.toFixed(1)}%</div><div class="cell-name">${esc(dateTxt(risk.max_drawdown_peak))} → ${esc(dateTxt(risk.max_drawdown_trough))}${risk.recovered_on ? ` · recovered ${esc(dateTxt(risk.recovered_on))}` : " · not yet recovered"}</div></div>
          <div class="stat"><div class="k">Below its all-time high</div><div class="v num ${risk.current_drawdown_pct < 0 ? "down" : ""}">${risk.current_drawdown_pct.toFixed(1)}%</div></div>
          <div class="stat"><div class="k">Best / worst calendar year</div><div class="v num"><span class="up">${pct(risk.best_year_pct, 1)}</span> / <span class="down">${pct(risk.worst_year_pct, 1)}</span></div></div></div></div></div>
    <div class="card"><div class="card-h"><div><div class="card-t">Rolling returns</div><div class="muted" style="font-size:12px">Every holding period of that length in the last 10 years — what investors actually experienced, not one lucky start date</div></div></div>
      ${["1Y", "3Y", "5Y"].map((k) => { const x = roll(k); if (!x) return `<div class="roll"><b>${k}</b><span class="muted">Not enough history</span></div>`;
        const lo = Math.min(x.min, 0), hi = Math.max(x.max, 1), sc = (v) => ((v - lo) / (hi - lo)) * 100;
        return `<div class="roll"><b>${k}</b><div class="roll-track"><i class="zero" style="left:${sc(0)}%"></i><i class="iqr" style="left:${sc(x.p25)}%;width:${sc(x.p75) - sc(x.p25)}%"></i><i class="med" style="left:${sc(x.median)}%"></i></div>
          <div class="roll-v num">median <b>${pct(x.median, 1)}</b> · range ${pct(x.min, 1)} to ${pct(x.max, 1)} · positive <b>${x.pct_positive}%</b> of the time · above 10% <b>${x.pct_above_10}%</b></div></div>`; }).join("")}
      <div class="fine">Bar shows the middle 50% of outcomes, the line marks the median, the faint tick marks 0%.</div></div>
    <div class="card"><div class="card-h"><div><div class="card-t">SIP back-test on real NAVs</div><div class="muted" style="font-size:12px">What a monthly SIP in this fund actually turned into</div></div></div>
      <form class="sip-form" id="sipForm"><label>Monthly <input type="number" name="amount" min="100" step="100" value="${F.sip.amount}"></label>
        <label>Years <input type="number" name="years" min="1" max="30" value="${F.sip.years}"></label>
        <label>Yearly step-up % <input type="number" name="step" min="0" max="50" value="${F.sip.step}"></label><button class="btn primary">Run</button></form>
      <div id="sipOut"></div></div>
    ${d.other_plans.length ? `<div class="card"><div class="card-t" style="margin-bottom:8px">Other plans of this fund</div>${d.other_plans.map((o) => `<a class="chip-link" href="#/funds/${esc(o.code)}">${esc(o.plan || "")} · ${esc(o.option || "")}<span class="faint">NAV ${fmt(o.nav, 2)}</span></a>`).join("")}</div>` : ""}
    <div class="fine">${esc(d.data_note)} ${d.classification.tax_inferred ? "* Tax treatment inferred from the category/name — check the fund's equity allocation. " : ""}Past performance doesn't guarantee future returns.</div>`;
  lineChart($("#navChart"), [{ points: pts }], { height: 300 });
  $("#fRange").onclick = (e) => { const b = e.target.closest("button"); if (b) { F.range = b.dataset.r; renderDetail(); renderSip(); } };
  $("#fCmp").onclick = () => { toggleCompare(d.code); $("#fCmp").textContent = F.compare.includes(d.code) ? "✓ In compare" : "+ Compare"; };
  $("#fAdd").onclick = () => openAddTrade({ symbol: `MF${d.code}`, price: last[1], label: d.name, units: true });
  $("#fAlert").onclick = () => openAlertDialog(`MF${d.code}`, last[1], "INR");
  $("#sipForm").onsubmit = (e) => { e.preventDefault(); const f = e.target; F.sip = { amount: Number(f.amount.value), years: Number(f.years.value), step: Number(f.step.value) }; loadSip(); };
}
async function loadSip() {
  const d = F.detail; if (!d) return;
  $("#sipOut").innerHTML = `<div class="skeleton" style="height:80px"></div>`;
  try { F.sipRes = await api(`/funds/${d.code}/sip?amount=${F.sip.amount}&years=${F.sip.years}&step_up=${F.sip.step}`); }
  catch (err) { F.sipRes = { error: err.message }; }
  renderSip();
}
function renderSip() {
  const box = $("#sipOut"), s = F.sipRes; if (!box || !s) return;
  if (s.error) { box.innerHTML = `<div class="muted">${esc(s.error)}</div>`; return; }
  box.innerHTML = `<div class="calc-out" style="margin-top:12px"><div><div class="k">Invested (${s.instalments} instalments)</div><div class="v num">${inrShort(s.invested)}</div></div>
      <div><div class="k">Worth today</div><div class="v num">${inrShort(s.value)}</div></div><div><div class="k">Gain</div><div class="v num ${tone(s.gain)}">${inrShort(s.gain)} <span style="font-size:12px">(${pct(s.absolute_return_pct, 1)})</span></div></div>
      <div><div class="k">XIRR</div><div class="v num ${tone(s.xirr_pct)}">${pct(s.xirr_pct)}</div></div></div>
    <div id="sipChart" style="margin-top:10px"></div><div class="fine">From ${esc(dateTxt(s.start))} to ${esc(dateTxt(s.end))}, buying on the same date each month (next NAV day if a holiday). ${s.note ? esc(s.note) : ""}</div>`;
  lineChart($("#sipChart"), [{ points: s.curve.map((c) => [Date.parse(c[0]), c[1]]), color: css("--faint") }, { points: s.curve.map((c) => [Date.parse(c[0]), c[2]]), color: css("--brand") }],
    { height: 200, labels: ["Invested", "Value"] });
}

// ============================================================ compare
async function loadCompare(codes) {
  const box = $("#fundsBody");
  if (codes.length < 2) {
    box.innerHTML = `<div class="card empty-state"><h3>Pick at least two funds</h3><p>Use “+ Compare” on any fund to add it here (up to 4).</p><div class="actions"><a class="btn primary" href="#/funds">Browse funds</a></div></div>`; return;
  }
  box.innerHTML = `<div class="card"><div class="skeleton" style="height:340px"></div></div>`;
  let d;
  try { d = await api(`/funds/compare?codes=${codes.join(",")}`); } catch (err) { box.innerHTML = `<div class="card muted">${esc(err.message)}</div>`; return; }
  const fs = d.funds;
  const row = (label, get, better = "high") => {
    const vals = fs.map(get), nums = vals.filter((v) => v != null), best = nums.length ? (better === "high" ? Math.max(...nums) : Math.min(...nums)) : null;
    return `<tr><td>${label}</td>${vals.map((v) => `<td class="r num ${v != null && v === best && better !== "none" ? "best" : ""}">${v == null ? "—" : typeof v === "number" ? pct(v) : esc(v)}</td>`).join("")}</tr>`;
  };
  box.innerHTML = `<div class="card"><div class="card-h"><div><div class="card-t">Growth of ₹100</div><div class="muted" style="font-size:12px">From ${esc(dateTxt(d.start))}, when all ${fs.length} funds had NAVs</div></div>
      <div class="legend">${fs.map((f, i) => `<span><i style="background:${LINE_COLORS[i]}"></i>${esc(f.name.slice(0, 32))}</span>`).join("")}</div></div><div id="cmpChart"></div></div>
    <div class="card"><div class="table-wrap"><table class="cmp"><tr><th></th>${fs.map((f, i) => `<th class="r" style="color:${LINE_COLORS[i]};text-transform:none;letter-spacing:0;white-space:normal">
        <a href="#/funds/${esc(f.code)}" style="text-decoration:none">${esc(f.name)}</a><div class="cell-name" style="margin-left:auto">${esc(f.plan || "")} · <button class="btn sm ghost" data-rm="${esc(f.code)}">Remove</button></div></th>`).join("")}</tr>
      ${row("Category", (f) => f.sub_category, "none")}
      ${row("1Y return", (f) => f.returns.periods["1Y"])}${row("3Y CAGR", (f) => f.returns.periods["3Y"])}${row("5Y CAGR", (f) => f.returns.periods["5Y"])}
      ${row("Since launch CAGR", (f) => f.returns.since_inception_cagr)}${row("Volatility (3Y)", (f) => f.risk.volatility_pct, "low")}
      ${row("Worst fall ever", (f) => f.risk.max_drawdown_pct)}${row("Median 3Y rolling return", (f) => f.rolling_3y?.median ?? null)}
      ${row("3Y periods above 10%", (f) => f.rolling_3y?.pct_above_10 ?? null)}</table></div>
      <div class="fine">Best value in each row is highlighted. Volatility: lower is better; worst fall: closer to 0 is better.</div></div>`;
  lineChart($("#cmpChart"), fs.map((f, i) => ({ points: f.growth.map(([t, v]) => [Date.parse(t), v]), color: LINE_COLORS[i] })), { height: 320, rebased: true, labels: fs.map((f) => f.name.split(" ").slice(0, 3).join(" ")) });
  box.querySelectorAll("[data-rm]").forEach((b) => (b.onclick = () => { F.compare = F.compare.filter((c) => c !== b.dataset.rm); saveCompare(); navigate("funds", "compare", { codes: F.compare.join(",") }); }));
}

// ============================================================ routing
export function initFunds() {
  $("#compareBtn").onclick = () => navigate("funds", "compare", { codes: F.compare.join(",") });
}
export async function showFunds(sub, params) {
  renderCompareBtn();
  const title = $("#fundsTitle"), back = $("#fundsBack");
  if (sub === "compare") {
    title.hidden = false; $("#view-funds .page-sub").hidden = true;
    title.textContent = "Compare funds"; back.hidden = false;
    const codes = (params.get("codes") || F.compare.join(",")).split(",").filter(Boolean);
    loadCompare(codes); return;
  }
  const subline = $("#view-funds .page-sub");
  if (sub) { title.hidden = subline.hidden = true; back.hidden = false; loadDetail(sub); return; }
  title.hidden = subline.hidden = false;
  title.textContent = "Mutual funds"; back.hidden = true;
  document.title = "Mutual funds · Kairo Markets";
  await loadCategories();
  renderHome();
  if (!F.top || F.top.key !== `${F.cat}|${F.period}|${F.plan}`) loadTop(); else renderTop();
}
export async function searchFunds(q) {
  try { return (await api(`/funds/search?q=${encodeURIComponent(q)}&limit=6`)).items; } catch { return []; }
}
