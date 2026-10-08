// Alerts: create dialog, alerts page, and the always-on trigger listener (toast + desktop notification).
import { $, esc, api, toast, modal, money, fmt, ago, symHtml, splitSym, store, navigate } from "./core.js";

const KINDS = [
  ["price_above", "Price rises to or above"], ["price_below", "Price falls to or below"],
  ["day_change_above", "Day change at or above (%)"], ["day_change_below", "Day change at or below (%)"],
];
const KIND_LABEL = Object.fromEntries(KINDS);
let alerts = [], engine = null, unseen = store.get("kairo-alerts-unseen", 0);

function setBadge(n) {
  unseen = n; store.set("kairo-alerts-unseen", n);
  const b = $("#alertCount"); if (!b) return;
  b.hidden = !n; b.textContent = n > 9 ? "9+" : n;
}

export function openAlertDialog(symbol = "", price = null, currency = "") {
  const body = `<form id="alertForm">
    <div class="form-grid">
      <div class="field"><label>Symbol</label><input name="symbol" value="${esc(symbol)}" placeholder="RELIANCE.NS" required autocomplete="off"></div>
      <div class="field"><label>Condition</label><select name="kind">${KINDS.map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select></div>
      <div class="field"><label>Value</label><input name="threshold" type="number" step="any" required value="${price ? (price * 1.02).toFixed(2) : ""}"></div>
      <div class="field"><label>Note (optional)</label><input name="note" maxlength="140" placeholder="e.g. breakout level"></div>
    </div>
    <div class="fine" id="alertHint">${price ? `Current price ${money(price, currency)}. Alerts fire once, then wait to be re-armed.` : "Alerts fire once, then wait to be re-armed."}</div>
    <div class="form-err" id="alertErr"></div>
    <div class="modal-f"><button type="button" class="btn ghost" data-close>Cancel</button><button class="btn primary">Create alert</button></div></form>`;
  modal("New price alert", body, (root, close) => {
    const form = root.querySelector("form");
    form.kind.onchange = () => {
      const pct = form.kind.value.startsWith("day_change");
      form.threshold.value = pct ? (form.kind.value.endsWith("above") ? "3" : "-3") : price ? (price * (form.kind.value === "price_above" ? 1.02 : 0.98)).toFixed(2) : "";
    };
    form.onsubmit = async (e) => {
      e.preventDefault();
      const btn = form.querySelector("button.primary"); btn.disabled = true;
      try {
        await api("/alerts", { method: "POST", body: { symbol: form.symbol.value.trim().toUpperCase(), kind: form.kind.value,
          threshold: Number(form.threshold.value), note: form.note.value.trim() } });
        close(); toast("Alert created", `${form.symbol.value.toUpperCase()} — ${KIND_LABEL[form.kind.value].toLowerCase()} ${form.threshold.value}`);
        refreshAlerts();
      } catch (err) { $("#alertErr").textContent = err.message; btn.disabled = false; }
    };
  });
}

async function refreshAlerts() {
  try {
    const body = await api("/alerts");
    alerts = body.alerts; engine = body.engine;
  } catch { /* keep last state */ }
  if (!$("#view-alerts").hidden) renderAlerts();
}

function row(a) {
  const unit = a.kind.startsWith("day_change") ? "%" : "";
  const fired = a.status === "triggered";
  return `<tr>
    <td><a class="cell-sym" href="#/markets?symbol=${encodeURIComponent(a.symbol)}" style="text-decoration:none">${symHtml(a.symbol)}</a>${a.note ? `<div class="cell-name">${esc(a.note)}</div>` : ""}</td>
    <td>${esc(KIND_LABEL[a.kind].replace(" (%)", ""))} <b class="num">${fmt(a.threshold)}${unit}</b></td>
    <td>${fired ? `<span class="chip warn">Triggered</span><div class="cell-name num">${esc(ago(a.triggered_at))} at ${fmt(a.triggered_price)}</div>`
                : `<span class="chip good">Active</span><div class="cell-name">since ${esc(ago(a.created_at))}</div>`}</td>
    <td class="r">${fired ? `<button class="btn sm" data-rearm="${a.id}">Re-arm</button>` : ""} <button class="btn sm ghost danger" data-del="${a.id}">Delete</button></td></tr>`;
}

function renderAlerts() {
  const box = $("#alertsBody");
  const perm = "Notification" in window ? Notification.permission : "unsupported";
  $("#notifyBtn").hidden = perm !== "default";
  const engineTxt = engine ? `Engine ${engine.stream === "connected" ? "watching the live stream" : "polling snapshots"}${engine.last_check ? ` · checked ${ago(engine.last_check)}` : ""}` : "";
  $("#alertsSub").textContent = `Checked by the server on every tick while Kairo is running. ${engineTxt}`;
  if (!alerts.length) {
    box.innerHTML = `<div class="card empty-state"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--brand)" stroke-width="1.6"><path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>
      <h3>No alerts yet</h3><p>Get notified when a stock crosses a price or moves sharply in a day.</p>
      <div class="actions"><button class="btn primary" id="emptyNewAlert">New alert</button></div></div>`;
    $("#emptyNewAlert").onclick = () => openAlertDialog();
    return;
  }
  const active = alerts.filter((a) => a.status === "active"), fired = alerts.filter((a) => a.status === "triggered");
  const table = (list) => `<div class="table-wrap"><table><tr><th>Symbol</th><th>Condition</th><th>Status</th><th></th></tr>${list.map(row).join("")}</table></div>`;
  box.innerHTML = (fired.length ? `<div class="card"><div class="card-h"><span class="card-t">Triggered</span><span class="muted" style="font-size:12px">${fired.length}</span></div>${table(fired)}</div>` : "") +
    `<div class="card"><div class="card-h"><span class="card-t">Active</span><span class="muted" style="font-size:12px">${active.length}</span></div>${active.length ? table(active) : `<div class="muted">No active alerts.</div>`}</div>`;
  box.onclick = async (e) => {
    const del = e.target.closest("[data-del]"), rearm = e.target.closest("[data-rearm]");
    try {
      if (del) await api(`/alerts/${del.dataset.del}`, { method: "DELETE" });
      if (rearm) await api(`/alerts/${rearm.dataset.rearm}/rearm`, { method: "POST" });
    } catch (err) { toast("Couldn't update alert", err.message, "err"); }
    if (del || rearm) refreshAlerts();
  };
}

export function showAlerts() {
  setBadge(0);
  renderAlerts();
  refreshAlerts();
}

export function initAlerts() {
  $("#newAlertBtn").onclick = () => openAlertDialog();
  $("#notifyBtn").onclick = async () => { try { await Notification.requestPermission(); } catch { /* ignore */ } renderAlerts(); };
  setBadge(unseen);
  refreshAlerts();
  // One alerts stream for all Kairo tabs: whichever tab holds the lock keeps the connection open (even
  // in the background, so desktop notifications still arrive) and relays each alert to the other tabs.
  const triggered = (d, leader) => {
    const a = d.alert, title = `${splitSym(a.symbol)[0]} alert`;
    toast(title, d.message, "alert");
    if (leader && "Notification" in window && Notification.permission === "granted") {
      try { const n = new Notification(title, { body: d.message, tag: `kairo-alert-${a.id}` }); n.onclick = () => { window.focus(); navigate("markets", "", { symbol: a.symbol }); }; } catch { /* ignore */ }
    }
    if ($("#view-alerts").hidden) setBadge(unseen + 1);
    refreshAlerts();
  };
  const listen = (onEvent) => new EventSource("/alerts/stream").addEventListener("triggered", (e) => onEvent(JSON.parse(e.data)));
  if (navigator.locks && "BroadcastChannel" in window) {
    const chan = new BroadcastChannel("kairo-alerts");
    chan.onmessage = (e) => triggered(e.data, false);
    navigator.locks.request("kairo-alerts-stream", () => new Promise(() => {
      listen((d) => { chan.postMessage(d); triggered(d, true); });  // held until this tab closes
    }));
  } else {
    listen((d) => triggered(d, true));
  }
}
