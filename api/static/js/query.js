// Query screener (Screener.in / Chartink style): write conditions over price, India and fundamental fields,
// run them over a universe, save them as scans, and turn a scan into an alert for new matches.
import { $, esc, api, fmt, money, pctTxt, tone, symHtml, store, universeOptions, logoHtml, toast } from "./core.js";

const Q = { text: store.get("kairo-query", "close > sma50 AND rsi < 70 AND delivery_pct > 40"), universe: store.get("kairo-query-universe", "nifty50"),
  fields: null, examples: [], job: null, timer: 0, scans: [], universes: null, help: false };

export async function initQuery(universes) {
  Q.universes = universes;
  try { const f = await api("/screener/fields"); Q.fields = f.fields; Q.examples = f.examples; } catch { Q.fields = []; }
  $("#scrQuery").addEventListener("click", onClick);
  $("#scrQuery").addEventListener("keydown", (e) => { if (e.target.id === "qText" && e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); } });
  $("#scrQuery").addEventListener("change", (e) => { if (e.target.id === "qUniverse") { Q.universe = e.target.value; store.set("kairo-query-universe", Q.universe); } });
  $("#scrQuery").addEventListener("input", (e) => { if (e.target.id === "qText") { Q.text = e.target.value; store.set("kairo-query", Q.text); } });
}

export async function showQuery() {
  render();
  try { Q.scans = (await api("/screener/scans")).items; } catch { Q.scans = []; }
  renderScans();
}

function render() {
  const groups = { technical: "Price & technicals", india: "India (NSE)", fundamental: "Fundamentals (slower: fetched per stock)" };
  const help = (Q.fields || []).reduce((acc, f) => { (acc[f.group] ||= []).push(f); return acc; }, {});
  $("#scrQuery").innerHTML = `<div class="card q-card">
      <div class="q-row"><select class="uni-select" id="qUniverse" aria-label="Stocks to scan">${universeOptions(Q.universes, Q.universe, [])}</select>
        <button class="btn primary" data-run>Run query</button><button class="btn" data-save>Save scan</button><span class="muted q-hint">Ctrl+Enter to run</span></div>
      <textarea id="qText" class="input q-text" spellcheck="false" aria-label="Query" placeholder="roe > 15 AND pe < 25 AND close > sma200">${esc(Q.text)}</textarea>
      <div class="chips q-ex">${Q.examples.map((x) => `<button data-ex="${esc(x.query)}" title="${esc(x.query)}">${esc(x.name)}</button>`).join("")}</div>
      <details class="q-help" ${Q.help ? "open" : ""}><summary>Fields you can use · AND, OR, NOT, brackets, + − × ÷ and &lt; &gt; = work</summary>
        ${Object.entries(groups).map(([g, l]) => help[g] ? `<div class="q-group"><b>${l}</b><div class="q-fields">${help[g].map((f) =>
          `<button data-field="${esc(f.name)}" title="${esc(f.about)}${f.unit ? ` (${esc(f.unit)})` : ""}"><code>${esc(f.name)}</code><small class="muted">${esc(f.about)}</small></button>`).join("")}</div></div>` : "").join("")}</details>
    </div>
    <div id="qResults"></div><div id="qScans"></div>`;
  renderResults();
}

async function run() {
  clearTimeout(Q.timer);
  Q.text = $("#qText").value.trim(); store.set("kairo-query", Q.text);
  try { Q.job = await api("/screener/query", { method: "POST", body: { query: Q.text, universe: Q.universe } }); }
  catch (err) { Q.job = { status: "error", error: err.message }; }
  renderResults(); poll();
}
async function poll() {
  if (Q.job?.status !== "running") return;
  Q.timer = setTimeout(async () => {
    try { Q.job = await api(`/screener/query/${Q.job.id}`); } catch (err) { Q.job = { status: "error", error: err.message }; }
    renderResults(); poll();
  }, 1000);
}

// changes get a sign (+3.1%); levels don't (delivery 84.5%)
const signedPct = (k) => k.startsWith("chg_") || k.startsWith("from_") || k.endsWith("_growth");
const FMT = { "%": (v, k) => (signedPct(k) ? pctTxt(v, 1) : `${fmt(v, 1)}%`), "₹ cr": (v) => `₹${Math.round(v).toLocaleString("en-IN")} cr`, "×": (v) => `${fmt(v, 2)}×`, "yes/no": (v) => (v ? "Yes" : "No") };
function renderResults() {
  const box = $("#qResults"); if (!box) return;
  const j = Q.job;
  if (!j) { box.innerHTML = ""; return; }
  if (j.status === "error" || j.status === "failed") { box.innerHTML = `<div class="card q-err">${esc(j.error)}</div>`; return; }
  const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
  const head = `<div class="card-h"><span class="card-t">${j.status === "running" ? `Scanning ${esc(j.label)}… ${j.done}/${j.total}` : `${j.rows.length} match${j.rows.length === 1 ? "" : "es"} in ${esc(j.label)}`}</span>
      <span class="muted q-hint">${esc(j.query)}</span></div>
    ${j.status === "running" ? `<div class="ac-bar"><span style="width:${pct}%"></span></div>${j.slow ? `<p class="muted q-hint">Fundamentals are fetched per stock, so the first run takes longer; later runs use the cache.</p>` : ""}` : ""}`;
  if (j.status === "running") { box.innerHTML = `<div class="card">${head}</div>`; return; }
  const meta = Object.fromEntries((Q.fields || []).map((f) => [f.name, f]));
  const cols = j.fields.filter((f) => !["close", "price", "chg_1d"].includes(f));
  const val = (k, v) => (v == null ? "—" : (FMT[meta[k]?.unit] || ((x) => (typeof x === "number" ? fmt(x, 2) : esc(x))))(v, k));
  box.innerHTML = `<div class="card">${head}${j.rows.length ? `<div class="table-wrap"><table><tr><th>Instrument</th><th class="r">Price</th><th class="r">1D</th>${cols.map((c) => `<th class="r" title="${esc(meta[c]?.about || "")}">${esc(c)}</th>`).join("")}</tr>
    ${j.rows.map((r) => `<tr class="click" data-sym="${esc(r.symbol)}"><td><div class="cell-id">${logoHtml(r.symbol, r.name, 26)}<div><div class="cell-sym">${symHtml(r.symbol)}</div><div class="cell-name">${esc(r.name || "")}</div></div></div></td>
      <td class="r num">${money(r.close, r.currency)}</td><td class="r num ${tone(r.chg_1d)}">${pctTxt(r.chg_1d)}</td>${cols.map((c) => `<td class="r num">${val(c, r[c])}</td>`).join("")}</tr>`).join("")}</table></div>`
    : `<div class="empty" style="height:100px">No stock matches every condition right now.</div>`}
    ${Object.keys(j.errors || {}).length ? `<p class="muted q-hint">${Object.keys(j.errors).length} stocks had no data and were skipped.</p>` : ""}</div>`;
}

function renderScans() {
  const box = $("#qScans"); if (!box) return;
  if (!Q.scans.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="card"><div class="card-h"><span class="card-t">Saved scans</span><span class="muted q-hint">Alerts re-run every 15 minutes in market hours and tell you about new matches.</span></div>
    ${Q.scans.map((s) => `<div class="q-scan"><div><b>${esc(s.name)}</b><code>${esc(s.query)}</code><small class="muted">${esc(s.universe)}${s.last_run ? ` · last run ${new Date(s.last_run).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}, ${s.last_symbols.length} matching` : ""}</small></div>
      <label class="q-alert"><input type="checkbox" data-alert="${s.id}" ${s.alert ? "checked" : ""}> Alert me</label>
      <button class="btn sm" data-load="${s.id}">Run</button><button class="btn sm" data-del="${s.id}" aria-label="Delete scan">✕</button></div>`).join("")}</div>`;
}

async function onClick(e) {
  const t = e.target;
  if (t.closest("[data-run]")) { run(); return; }
  const ex = t.closest("[data-ex]"); if (ex) { Q.text = ex.dataset.ex; $("#qText").value = Q.text; store.set("kairo-query", Q.text); run(); return; }
  const fld = t.closest("[data-field]");
  if (fld) { const ta = $("#qText"), pos = ta.selectionStart ?? ta.value.length; ta.value = ta.value.slice(0, pos) + fld.dataset.field + " " + ta.value.slice(pos); Q.text = ta.value; ta.focus(); return; }
  if (t.closest(".q-help summary")) { Q.help = !Q.help; return; }
  if (t.closest("[data-save]")) {
    const name = prompt("Name this scan", Q.examples.find((x) => x.query === $("#qText").value.trim())?.name || "My scan"); if (!name) return;
    const alert = confirm("Alert you when new stocks start matching this scan? (re-checked every 15 minutes in market hours)");
    try { await api("/screener/scans", { method: "POST", body: { name, universe: Q.universe, query: $("#qText").value.trim(), alert } }); toast("Scan saved", alert ? "You'll be alerted about new matches." : name, "ok"); showQuery(); }
    catch (err) { toast("Couldn't save the scan", err.message, "err"); }
    return;
  }
  const ld = t.closest("[data-load]");
  if (ld) { const s = Q.scans.find((x) => String(x.id) === ld.dataset.load); if (s) { Q.text = s.query; Q.universe = s.universe; render(); renderScans(); run(); } return; }
  const del = t.closest("[data-del]");
  if (del && confirm("Delete this scan?")) { try { await api(`/screener/scans/${del.dataset.del}`, { method: "DELETE" }); } catch (err) { toast("Couldn't delete", err.message, "err"); } showQuery(); return; }
  const al = t.closest("[data-alert]");
  if (al) {
    const s = Q.scans.find((x) => String(x.id) === al.dataset.alert); if (!s) return;
    try { await api(`/screener/scans?scan_id=${s.id}`, { method: "POST", body: { name: s.name, universe: s.universe, query: s.query, alert: al.checked } }); toast(al.checked ? "Scan alert on" : "Scan alert off", s.name, "ok"); }
    catch (err) { toast("Couldn't update", err.message, "err"); al.checked = !al.checked; }
    return;
  }
  const row = t.closest("[data-sym]"); if (row) location.hash = `#/markets?symbol=${encodeURIComponent(row.dataset.sym)}`;
}
