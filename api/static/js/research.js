// Stock research on the stock page: fundamentals, financials, scorecard + peers, shareholding pattern.
import { $, esc, api, fmt, pctTxt, tone, splitSym, navigate } from "./core.js";

const R = { fund: {}, score: {}, shp: {}, fin: { metric: "revenue", freq: "quarterly" }, shpQ: 0, sym: "" };
const researchable = (s) => !!s && !s.startsWith("^") && !s.includes("=") && !/-USD$/.test(s) && !s.startsWith("MF");
const pct = (v, d = 1) => (v == null ? "—" : `${(v * 100).toFixed(d)}%`);
const x = (v, d = 2) => (v == null ? "—" : `${fmt(v, d)}x`);

function big(v, cur) {
  if (v == null) return "—";
  if (cur === "INR") {
    const cr = v / 1e7, a = Math.abs(cr);
    return `₹${a >= 100 ? Math.round(cr).toLocaleString("en-IN") : cr.toLocaleString("en-IN", { maximumFractionDigits: 2 })} Cr`;
  }
  const s = { USD: "$", EUR: "€", GBP: "£" }[cur] ?? "", a = Math.abs(v), sign = v < 0 ? "−" : "";
  const [d, u] = a >= 1e12 ? [1e12, "T"] : a >= 1e9 ? [1e9, "B"] : a >= 1e6 ? [1e6, "M"] : [1, ""];
  return `${sign}${s}${(a / d).toFixed(2)}${u}`;
}

export function loadResearch(sym) {
  R.sym = sym; R.shpQ = 0;
  const show = researchable(sym);
  for (const id of ["#fundCard", "#finCard", "#scoreCard", "#shpCard", "#peerCard"]) $(id).hidden = !show;
  if (!show) return;
  renderAll();
  const get = async (bucket, url) => {
    if (bucket[sym]?.data || bucket[sym]?.loading) return;
    bucket[sym] = { loading: true };
    try { bucket[sym] = { data: await api(url, { timeout: 45000 }) }; } catch (err) { bucket[sym] = { error: err.message, status: err.status }; }
    if (R.sym === sym) renderAll();
  };
  get(R.fund, `/fundamentals/${encodeURIComponent(sym)}`);
  get(R.score, `/fundamentals/${encodeURIComponent(sym)}/scorecard`);
  if (/\.(NS|BO)$/.test(sym)) get(R.shp, `/shareholding/${encodeURIComponent(sym)}`);
  else { $("#shpCard").hidden = true; $("#shpCard").innerHTML = ""; }
}

function renderAll() { renderFund(); renderFin(); renderScore(); renderPeers(); renderShp(); }
const skel = (h = 120) => `<div class="skeleton" style="height:${h}px"></div>`;
const failed = (msg) => `<div class="muted" style="font-size:13px">${esc(msg)}</div>`;

// ------------------------------------------------------------------ fundamentals
function renderFund() {
  const box = $("#fundCard"), f = R.fund[R.sym], s = R.score[R.sym]?.data;
  const head = `<div class="card-h"><span class="card-t">Fundamentals</span><span class="muted rs-src">Yahoo Finance · TTM</span></div>`;
  if (!f || f.loading) { box.innerHTML = head + skel(); return; }
  if (f.error) { box.hidden = f.status === 404; box.innerHTML = head + failed("Fundamentals couldn't be loaded right now."); return; }
  const d = f.data, cur = d.currency, med = s?.medians || {};
  const tile = (k, v, sub = "") => `<div class="fd-tile"><div class="k">${k}</div><div class="v num">${v}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
  const vs = (m) => (med[m] != null ? `Industry ${m === "pe" || m === "pb" ? x(med[m], 1) : pct(med[m])}` : "");
  box.innerHTML = head + `<div class="fd-grid">
    ${tile("Market cap", big(d.market_cap, cur))}
    ${tile("P/E", d.pe != null ? x(d.pe, 1) : d.eps != null && d.eps < 0 ? "Loss-making" : "—", vs("pe"))}
    ${tile("P/B", x(d.pb, 2), vs("pb"))}
    ${tile("ROE", pct(d.roe), vs("roe"))}
    ${tile("Net margin", pct(d.net_margin), vs("net_margin"))}
    ${tile("Operating margin", pct(d.operating_margin))}
    ${tile("Debt / equity", d.debt_to_equity != null ? x(d.debt_to_equity, 2) : "—", d.debt_to_equity == null && /bank/i.test(d.industry || "") ? "Not meaningful for banks" : "")}
    ${tile("EPS (TTM)", d.eps != null ? fmt(d.eps, 2) : "—")}
    ${tile("Book value / share", d.book_value != null ? fmt(d.book_value, 2) : "—")}
    ${tile("Dividend yield", pct(d.dividend_yield, 2))}
    ${tile("Revenue growth", d.revenue_growth != null ? `<span class="${tone(d.revenue_growth)}">${pctTxt(d.revenue_growth * 100, 1)}</span>` : "—", "Latest quarter, year on year")}
    ${tile("Beta", d.beta != null ? fmt(d.beta, 2) : "—", "Volatility vs the market")}
  </div>`;
}

// ------------------------------------------------------------------ financials
const FIN = [["revenue", "Revenue"], ["net_income", "Net profit"], ["operating_income", "Operating profit"], ["equity", "Net worth"]];
function periodLabel(iso, freq, cur) {
  const d = new Date(iso + "T00:00:00");
  if (freq === "annual") return cur === "INR" && d.getMonth() === 2 ? `FY${String(d.getFullYear()).slice(-2)}` : String(d.getFullYear());
  return d.toLocaleDateString([], { month: "short", year: "2-digit" });
}
function renderFin() {
  const box = $("#finCard"), f = R.fund[R.sym];
  const tabs = `<div class="fin-tabs"><div class="seg">${FIN.map(([k, l]) => `<button data-fm="${k}" class="${k === R.fin.metric ? "on" : ""}">${l}</button>`).join("")}</div>
    <div class="seg"><button data-ff="quarterly" class="${R.fin.freq === "quarterly" ? "on" : ""}">Quarterly</button><button data-ff="annual" class="${R.fin.freq === "annual" ? "on" : ""}">Yearly</button></div></div>`;
  const head = `<div class="card-h"><span class="card-t">Financials</span>${tabs}</div>`;
  if (!f || f.loading) { box.innerHTML = head + skel(180); wireFin(); return; }
  if (f.error) { box.hidden = true; return; }
  const d = f.data, rows = (d[R.fin.freq] || []).filter((r) => r[R.fin.metric] != null);
  if (!rows.length) { box.innerHTML = head + failed("No reported figures for this view."); wireFin(); return; }
  const vals = rows.map((r) => r[R.fin.metric]), max = Math.max(...vals.map(Math.abs), 1);
  const bars = rows.map((r, i) => {
    const v = r[R.fin.metric], prev = i > 0 ? vals[i - 1] : null, h = Math.max(2, (Math.abs(v) / max) * 100);
    const g = prev ? (v - prev) / Math.abs(prev) : null;
    return `<div class="fin-col"><div class="fin-val num">${big(v, d.currency)}</div>
      <div class="fin-bar-wrap"><div class="fin-bar ${v < 0 ? "neg" : ""}" style="height:${h}%"></div></div>
      <div class="fin-lbl">${periodLabel(r.period_end, R.fin.freq, d.currency)}</div>
      <div class="fin-g num ${tone(g)}">${g != null ? pctTxt(g * 100, 1) : ""}</div></div>`;
  }).join("");
  box.innerHTML = head + `<div class="fin-chart">${bars}</div>
    <div class="fine">${R.fin.freq === "quarterly" ? "Change vs the previous quarter" : "Change vs the previous year"} · as reported, ${esc(d.currency || "")}</div>`;
  wireFin();
}
function wireFin() {
  $("#finCard").onclick = (e) => {
    const m = e.target.closest("[data-fm]"), q = e.target.closest("[data-ff]");
    if (m) R.fin.metric = m.dataset.fm; if (q) R.fin.freq = q.dataset.ff;
    if (m || q) renderFin();
  };
}

// ------------------------------------------------------------------ scorecard + peers
const METRIC = { year_change: ["1Y return", pct], pe: ["P/E", (v) => x(v, 1)], pb: ["P/B", (v) => x(v, 2)], roe: ["ROE", pct], net_margin: ["Net margin", pct],
  revenue_growth: ["Revenue growth", pct], earnings_growth: ["Earnings growth", pct], beta: ["Beta", (v) => (v == null ? "—" : fmt(v, 2))], debt_to_equity: ["Debt / equity", (v) => x(v, 2)] };
function renderScore() {
  const box = $("#scoreCard"), s = R.score[R.sym];
  const head = (sub = "") => `<div class="card-h"><span class="card-t">Scorecard</span><span class="muted rs-src">${sub}</span></div>`;
  if (!s || s.loading) { box.innerHTML = head("Comparing with industry peers…") + skel(200); return; }
  if (s.error) { box.hidden = s.status === 404; box.innerHTML = head() + failed("The scorecard couldn't be built right now."); return; }
  const d = s.data;
  if (!d.peer_count) { box.innerHTML = head() + failed("No comparable companies found for this stock, so there's nothing to score against."); return; }
  box.innerHTML = head(`vs ${d.peer_count} peers · ${esc(d.group || "")}`) + d.dimensions.map((dim) => `
    <div class="sc-row">
      <div class="sc-top"><b>${dim.label}</b>${dim.score == null ? `<span class="muted">Not enough data</span>` : `<span class="sc-pill ${dim.tone}">${esc(dim.verdict)}</span>`}</div>
      <div class="sc-bar"><i class="${dim.tone || ""}" style="width:${dim.score ?? 0}%"></i></div>
      <div class="sc-metrics">${dim.metrics.map((m) => `<span>${METRIC[m.key][0]} <b class="num">${METRIC[m.key][1](m.value)}</b>${m.median != null ? ` <span class="muted">vs ${METRIC[m.key][1](m.median)}</span>` : ""}</span>`).join("")}</div>
    </div>`).join("") + `<div class="fine">Scores are percentiles among the stock and its peers (closest in size within the same industry); higher is better, and for valuation and risk that means cheaper and safer. Not investment advice.</div>`;
}
function renderPeers() {
  const box = $("#peerCard"), s = R.score[R.sym];
  if (!s || s.loading) { box.innerHTML = `<div class="card-h"><span class="card-t">Peers</span></div>` + skel(160); return; }
  if (!s.data?.peers?.length) { box.hidden = true; return; }
  box.hidden = false;
  const d = s.data, cur = d.me.currency;
  const row = (r, me = false) => `<tr class="${me ? "me" : "click"}" ${me ? "" : `data-sym="${esc(r.symbol)}"`}>
    <td><b>${esc(splitSym(r.symbol)[0])}</b><div class="cell-name">${esc(r.name || "")}</div></td>
    <td class="r num">${big(r.market_cap, r.currency || cur)}</td><td class="r num">${r.pe != null && r.pe > 0 ? fmt(r.pe, 1) : "—"}</td>
    <td class="r num">${r.pb != null ? fmt(r.pb, 2) : "—"}</td><td class="r num">${pct(r.roe)}</td><td class="r num">${pct(r.net_margin)}</td>
    <td class="r num ${tone(r.year_change)}">${r.year_change != null ? pctTxt(r.year_change * 100, 1) : "—"}</td></tr>`;
  box.innerHTML = `<div class="card-h"><span class="card-t">Peers</span><span class="muted rs-src">${esc(d.group || "")}</span></div>
    <div class="table-wrap"><table class="peer-table"><tr><th>Company</th><th class="r">Market cap</th><th class="r">P/E</th><th class="r">P/B</th><th class="r">ROE</th><th class="r">Net margin</th><th class="r">1Y</th></tr>
    ${row(d.me, true)}${d.peers.map((r) => row(r)).join("")}</table></div>`;
  box.onclick = (e) => { const tr = e.target.closest("tr[data-sym]"); if (tr) navigate("markets", "", { symbol: tr.dataset.sym }); };
}

// ------------------------------------------------------------------ shareholding
const SHP = [["promoters", "Promoters", "#7c5cff"], ["fii", "Foreign institutions (FII)", "#4ea1ff"], ["mutual_funds", "Mutual funds", "#2bb3c0"],
  ["other_dii", "Other domestic institutions", "#e9a23b"], ["retail_and_others", "Retail & others", "#8892a6"]];
function renderShp() {
  const box = $("#shpCard"), s = R.shp[R.sym];
  if (!/\.(NS|BO)$/.test(R.sym)) { box.hidden = true; return; }
  const head = `<div class="card-h"><span class="card-t">Shareholding pattern</span><span class="muted rs-src">NSE filings</span></div>`;
  if (!s || s.loading) { box.innerHTML = head + skel(160); return; }
  if (s.error || !s.data?.quarters?.length) { box.hidden = true; return; }
  const qs = s.data.quarters, q = qs[R.shpQ] || qs[0], prev = qs[R.shpQ + 1];
  const quarter = (iso) => new Date(iso + "T00:00:00").toLocaleDateString([], { month: "short", year: "numeric" });
  const groups = SHP.filter(([k]) => q[k] != null);
  const fallback = !groups.some(([k]) => k !== "promoters");  // only the promoter / public split was filed
  const rows = fallback ? [["promoters", "Promoters", "#7c5cff"], ["public", "Public", "#8892a6"]] : groups;
  box.innerHTML = head + `<div class="shp-qs">${qs.map((x, i) => `<button data-q="${i}" class="${i === R.shpQ ? "on" : ""}">${quarter(x.quarter_end)}</button>`).join("")}</div>
    <div class="shp-stack">${rows.map(([k, , c]) => `<i style="width:${q[k] || 0}%;background:${c}" title="${k}"></i>`).join("")}</div>
    ${rows.map(([k, l, c]) => {
      const ch = prev && prev[k] != null && q[k] != null ? q[k] - prev[k] : null;
      return `<div class="shp-row"><span><i style="background:${c}"></i>${l}</span><b class="num">${fmt(q[k] ?? 0, 2)}%</b>
        <span class="shp-ch num ${ch == null || Math.abs(ch) < 0.005 ? "muted" : tone(ch)}">${ch == null ? "" : Math.abs(ch) < 0.005 ? "no change" : `${ch > 0 ? "▲" : "▼"} ${fmt(Math.abs(ch), 2)}`}</span></div>`;
    }).join("")}
    <div class="fine">Change vs the previous quarter. Mutual funds are part of domestic institutions; "other domestic institutions" are banks, insurers, pension funds and the like.</div>`;
  box.onclick = (e) => { const b = e.target.closest("[data-q]"); if (b) { R.shpQ = Number(b.dataset.q); renderShp(); } };
}
