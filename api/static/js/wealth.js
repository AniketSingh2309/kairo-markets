// Wealth tools on the Portfolio page: CAS import (all your mutual funds from one PDF), a tax-harvesting
// plan with one-click recording, and what Regular-plan funds cost you against their Direct plans.
import { $, esc, api, fmt, toast, modal, symHtml } from "./core.js";

const inr = (v) => (v == null ? "—" : `₹${Math.round(v).toLocaleString("en-IN")}`);

// ------------------------------------------------------------------ CAS import
export function openCasImport(onDone) {
  modal("Import mutual funds from a CAS", `<div class="fine" style="margin-bottom:10px">Your <b>Consolidated Account Statement</b> lists every mutual-fund transaction across all fund houses.
      Get the <b>detailed</b> CAS (with transactions) free from <b>camsonline.com</b> or <b>kfintech.com</b> (Investor services → Mailback / CAS), or from MF Central.
      The file stays on this computer: it's read here and never sent anywhere else.</div>
    <div class="field"><label>CAS PDF</label><input type="file" id="casFile" accept="application/pdf,.pdf"></div>
    <div class="field" style="margin-top:10px"><label>PDF password (usually your PAN in capitals)</label><input class="input" id="casPw" type="password" autocomplete="off" placeholder="ABCDE1234F"></div>
    <div id="casPreview" style="margin-top:12px"></div><div class="form-err" id="casErr"></div>
    <div class="modal-f"><button class="btn ghost" data-close>Cancel</button><button class="btn" id="casCheck">Preview</button><button class="btn primary" id="casGo" disabled>Import</button></div>`,
  (root, close) => {
    let b64 = null;
    const go = root.querySelector("#casGo");
    root.querySelector("#casFile").onchange = async (e) => {
      const f = e.target.files[0]; b64 = null; go.disabled = true;
      if (!f) return;
      if (f.size > 15e6) { $("#casErr").textContent = "That file is over 15 MB; a CAS is usually much smaller."; return; }
      const bytes = new Uint8Array(await f.arrayBuffer());
      let s = ""; for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      b64 = btoa(s);
    };
    const body = (dry) => ({ pdf_base64: b64, password: root.querySelector("#casPw").value.trim(), dry_run: dry });
    async function preview() {
      $("#casErr").textContent = ""; go.disabled = true;
      if (!b64) { $("#casErr").textContent = "Choose the CAS PDF first."; return; }
      $("#casPreview").innerHTML = `<div class="skeleton" style="height:60px"></div>`;
      try {
        const r = await api("/portfolio/import-cas", { method: "POST", body: body(true), timeout: 120000 });
        const ok = r.schemes.filter((x) => x.imported), skipped = r.schemes.filter((x) => !x.imported);
        $("#casPreview").innerHTML = `<div class="notice" style="border-color:var(--border-2);background:var(--panel-2)">
          <b>${ok.length} fund${ok.length === 1 ? "" : "s"}, ${r.rows.length} transaction${r.rows.length === 1 ? "" : "s"} ready</b>${r.already_imported ? ` · ${r.already_imported} already in your portfolio (skipped)` : ""}
          <div class="cas-list">${r.schemes.map((x) => `<div class="${x.imported ? "" : "muted"}">${x.imported ? "✓" : "✕"} ${esc(x.matched || x.name)}
            <small class="muted">${x.folio ? `folio ${esc(x.folio)} · ` : ""}${x.transactions} txns · ${fmt(x.closing_units ?? x.units_read, 3)} units${x.estimated_opening ? " · opening cost estimated" : ""}</small></div>`).join("")}</div>
          ${r.errors.length ? `<div style="margin-top:6px;color:var(--warn)">${r.errors.slice(0, 6).map(esc).join("<br>")}${r.errors.length > 6 ? `<br>…and ${r.errors.length - 6} more` : ""}</div>` : ""}
          ${skipped.length ? `<div class="fine" style="margin-top:6px">Funds that didn't reconcile are left out so nothing half-read gets imported. You can add them by hand.</div>` : ""}</div>`;
        go.disabled = !r.rows.length; go.textContent = `Import ${r.rows.length}`;
      } catch (err) { $("#casPreview").innerHTML = ""; $("#casErr").textContent = err.message; }
    }
    root.querySelector("#casCheck").onclick = preview;
    go.onclick = async () => {
      go.disabled = true;
      try { const r = await api("/portfolio/import-cas", { method: "POST", body: body(false), timeout: 120000 }); close(); toast("CAS imported", `${r.imported} transactions added`, "ok"); onDone?.(); }
      catch (err) { $("#casErr").textContent = err.message; go.disabled = false; }
    };
  });
}

// ------------------------------------------------------------------ tax harvesting
export async function renderHarvest(box, onRecorded) {
  if (!box) return;
  box.innerHTML = `<div class="card"><div class="skeleton" style="height:90px"></div></div>`;
  let h;
  try { h = await api("/portfolio/harvest"); } catch (err) { box.innerHTML = `<div class="card muted">Harvest plan unavailable: ${esc(err.message)}</div>`; return; }
  const gainRows = h.gains.map((g) => `<tr><td>${symHtml(g.symbol)}</td><td class="r num">${fmt(g.quantity, g.symbol.startsWith("MF") ? 3 : 0)}</td><td class="r num">${fmt(g.price)}</td>
      <td class="r num up">${inr(g.gain)}</td><td class="r num">${inr(g.future_tax_saved)}</td></tr>`).join("");
  const lossRows = h.losses.map((g) => `<tr><td>${symHtml(g.symbol)}</td><td class="r num">${fmt(g.quantity, g.symbol.startsWith("MF") ? 3 : 0)}</td><td class="r num">${fmt(g.price)}</td>
      <td class="r num down">${inr(g.loss)}</td><td class="r num">${g.short_term_loss ? inr(g.short_term_loss) : "—"}</td></tr>`).join("");
  box.innerHTML = `<div class="card"><div class="card-h"><span class="card-t">Tax harvesting plan · ${esc(h.fy)}</span>
      <span class="muted hv-sub">${inr(h.exemption_remaining)} of this year's LTCG exemption still unused</span></div>
    ${h.gains.length ? `<p class="hv-sub"><b>Book ${inr(h.gain_booked)} of long-term gains tax-free</b>: sell these units and buy them straight back. Your cost goes up, so you'll pay about
      <b>${inr(h.future_tax_saved)}</b> less tax when you finally sell.</p>
      <div class="table-wrap"><table><tr><th>Holding</th><th class="r">Sell &amp; rebuy</th><th class="r">At about</th><th class="r">Gain booked</th><th class="r">Future tax saved</th></tr>${gainRows}</table></div>
      <div class="actions" style="margin-top:10px"><button class="btn primary" data-hv-record>I've done these trades: record them</button></div>`
      : `<p class="muted hv-sub">${h.exemption_remaining > 0 ? "No long-term gains to harvest right now (the oldest units of each holding are either short-term or in loss)." : "This year's exemption is used up."}</p>`}
    ${h.losses.length ? `<div class="rs-sub">Losses you could book against this year's gains</div>
      <div class="table-wrap"><table><tr><th>Holding</th><th class="r">Units</th><th class="r">At about</th><th class="r">Loss</th><th class="r">of which short-term</th></tr>${lossRows}</table></div>
      <p class="hv-sub">Booking these could save about <b>${inr(h.loss_tax_saved)}</b> in tax this year.</p>` : ""}
    <p class="fine">${esc(h.note)}</p></div>`;
  box.querySelector("[data-hv-record]")?.addEventListener("click", async () => {
    if (!confirm("Record the sale and same-day buy-back for each holding above? Only do this after placing the trades at your broker.")) return;
    try {
      const r = await api("/portfolio/harvest/record", { method: "POST", body: { items: h.gains.map((g) => ({ symbol: g.symbol, quantity: g.quantity, price: g.price })) } });
      toast("Harvest recorded", `${r.recorded} transactions added; your cost basis is updated.`, "ok"); onRecorded?.();
    } catch (err) { toast("Couldn't record", err.message, "err"); }
  });
}

// ------------------------------------------------------------------ Regular vs Direct
export async function renderDirectSavings(box) {
  if (!box) return;
  let d;
  try { d = await api("/portfolio/direct-savings"); } catch { box.innerHTML = ""; return; }
  if (!d.items.length) { box.innerHTML = ""; return; }
  const rows = d.items.map((x) => `<tr><td>${symHtml(x.symbol)}<div class="muted hv-sub">${esc(x.name)}</div></td><td class="r num">${inr(x.value)}</td>
      <td class="r num">${x.gap ? `${fmt(x.gap.regular_cagr, 2)}% → <b class="up">${fmt(x.gap.direct_cagr, 2)}%</b>` : "—"}</td>
      <td class="r num">${x.gap ? `<b>${fmt(x.gap.gap, 2)}%</b> a year` : "—"}</td><td class="r num">${x.lost_so_far != null ? inr(x.lost_so_far) : "—"}</td>
      <td class="r num down">${x.next_years_cost != null ? inr(x.next_years_cost) : "—"}</td>
      <td>${x.direct ? `<a class="btn sm" href="#/funds/${encodeURIComponent(x.direct.code)}">Direct plan</a>` : `<span class="muted hv-sub">no Direct plan found</span>`}</td></tr>`).join("");
  box.innerHTML = `<div class="card"><div class="card-h"><span class="card-t">Regular plans are costing you</span>
      ${d.next_years_cost ? `<span class="chip bad">about ${inr(d.next_years_cost)} over 10 years</span>` : ""}</div>
    <p class="hv-sub">You hold ${d.items.length} fund${d.items.length > 1 ? "s" : ""} in the <b>Regular</b> plan, which pays a distributor commission out of your returns every year.
      The <b>Direct</b> plan of the same fund, same manager, same portfolio, has returned this much more:</p>
    <div class="table-wrap"><table><tr><th>Fund</th><th class="r">Value</th><th class="r">Return (Regular → Direct)</th><th class="r">Gap</th><th class="r">Lost so far</th><th class="r">Next 10 years</th><th></th></tr>${rows}</table></div>
    <p class="fine">${esc(d.note)}</p></div>`;
}
