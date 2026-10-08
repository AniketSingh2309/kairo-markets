// Trading: order ticket, positions / orders / holdings / funds, paper vs live (Upstox) mode.
import { $, esc, api, store, toast, modal, fmt, money, pctTxt, tone, splitSym } from "./core.js";

const TR = { status: null, sub: store.get("kairo-trade-sub", "positions"), data: {}, timer: 0, box: null, openSymbol: null };
const SUBS = [["positions", "Positions"], ["orders", "Orders"], ["gtt", "GTT"], ["baskets", "Baskets"], ["holdings", "Holdings"], ["funds", "Funds"]];
const isOpt = (s) => (s || "").startsWith("OPT:");
export const tradable = (sym) => isOpt(sym) || /\.(NS|BO)$/.test(sym || "");
const live = () => TR.status?.mode === "upstox";
const inr = (v) => (v == null ? "—" : money(v, "INR"));
const pnl = (v) => (v == null ? "—" : `<span class="${tone(v)}">${v >= 0 ? "+" : "−"}${fmt(Math.abs(v))}</span>`);
const nameOf = (r) => r.label && r.label !== r.symbol ? r.label : splitSym(r.symbol)[0];

export async function refreshStatus() {
  try { TR.status = await api("/trading/status"); } catch { TR.status = null; }
  renderBadge();
  return TR.status;
}
export function renderBadge() {
  const b = $("#termMode"); if (!b) return;
  b.hidden = !TR.status;
  b.className = `mode-badge ${live() ? "live" : "paper"}`;
  b.textContent = live() ? "LIVE · Upstox" : "PAPER";
  b.title = live() ? "Orders go to your Upstox account with real money" : "Practice trading with virtual money";
}

// ------------------------------------------------------------------ order ticket
export async function openTicket({ symbol, side = "BUY", ltp = null, label = "" }) {
  if (!tradable(symbol)) { toast("Can't trade this", "Indices can't be traded. Open a stock or an option.", "err"); return; }
  if (!TR.status) await refreshStatus();
  let funds = null;
  try { funds = await api("/trading/funds"); } catch { /* shown as unknown */ }
  const opt = isOpt(symbol), name = label || splitSym(symbol)[0];
  const st = { side, product: opt ? "INTRADAY" : store.get("kairo-ticket-product", "DELIVERY"), type: "MARKET", mode: "regular", gtt: "single" };
  const px = (n, l) => `<div class="field tk-${n}"><label>${l}</label><input name="${n}" type="number" min="0" step="0.05" value=""></div>`;
  modal(`${side === "BUY" ? "Buy" : "Sell"} ${name}`, `<form class="ticket ${side === "BUY" ? "buy" : "sell"}" id="ticket">
      <div class="tk-mode ${live() ? "live" : "paper"}">${live() ? "LIVE order · real money · Upstox" : "Paper trading · virtual money"}</div>
      <div class="tk-row"><div class="seg tk-side"><button type="button" data-side="BUY">Buy</button><button type="button" data-side="SELL">Sell</button></div>
        <div class="tk-ltp">LTP <b class="num">${ltp != null ? inr(ltp) : "—"}</b></div></div>
      <div class="tk-row"><div class="seg tk-kind"><button type="button" data-m="regular">Regular</button><button type="button" data-m="gtt" ${live() ? "disabled title=\"GTT orders work in paper mode for now\"" : ""}>GTT</button></div>
        <div class="seg tk-prod"><button type="button" data-p="INTRADAY">Intraday</button><button type="button" data-p="DELIVERY">${opt ? "Carry forward" : "Delivery"}</button></div></div>
      <div class="tk-row tk-regular"><div class="seg tk-type">${["MARKET", "LIMIT", "SL", "SL-M"].map((t) => `<button type="button" data-t="${t}">${t === "MARKET" ? "Market" : t === "LIMIT" ? "Limit" : t}</button>`).join("")}</div></div>
      <div class="tk-row tk-gtt"><div class="seg tk-gttk"><button type="button" data-g="single">Single</button><button type="button" data-g="oco">OCO (target + stop-loss)</button></div></div>
      <div class="form-grid">
        <div class="field"><label>Quantity${opt ? " (units)" : ""}</label><input name="qty" type="number" min="1" step="1" value="1" required></div>
        <div class="field tk-price"><label>Price</label><input name="price" type="number" min="0" step="0.05" value="${ltp != null ? ltp : ""}"></div>
        <div class="field tk-trig"><label>Trigger price</label><input name="trigger" type="number" min="0" step="0.05" value="${ltp != null ? ltp : ""}"></div>
        ${px("gtrig", "Trigger price")}${px("glimit", "Limit price (blank = market)")}${px("strig", "Stop-loss trigger")}${px("slimit", "Stop-loss limit (blank = market)")}
      </div>
      <div class="tk-est" id="tkEst"></div>
      <label class="tk-confirm" ${live() ? "" : "hidden"}><input type="checkbox" name="confirm"> I understand this places a real order with real money through Upstox.</label>
      <div class="actions"><button type="button" class="btn tk-basket" data-basket title="Collect orders and place them together from Trade → Baskets">Add to basket</button>
        <span class="term-sp"></span><button type="button" class="btn" data-close>Cancel</button><button class="btn primary tk-go" type="submit"></button></div>
      <div class="fine tk-fine"></div>
    </form>`, (m, close) => {
    const f = m.querySelector("form");
    const num = (n) => (f[n].value === "" ? null : Number(f[n].value));
    const sync = () => {
      const gtt = st.mode === "gtt", oco = gtt && st.gtt === "oco";
      f.className = `ticket ${st.side === "BUY" ? "buy" : "sell"}`;
      m.querySelector(".modal-t").textContent = `${gtt ? "GTT · " : ""}${st.side === "BUY" ? "Buy" : "Sell"} ${name}`;
      f.querySelectorAll(".tk-side button").forEach((b) => b.classList.toggle("on", b.dataset.side === st.side));
      f.querySelectorAll(".tk-prod button").forEach((b) => b.classList.toggle("on", b.dataset.p === st.product));
      f.querySelectorAll(".tk-type button").forEach((b) => b.classList.toggle("on", b.dataset.t === st.type));
      f.querySelectorAll(".tk-kind button").forEach((b) => b.classList.toggle("on", b.dataset.m === st.mode));
      f.querySelectorAll(".tk-gttk button").forEach((b) => b.classList.toggle("on", b.dataset.g === st.gtt));
      f.querySelector(".tk-regular").hidden = gtt; f.querySelector(".tk-gtt").hidden = !gtt;
      f.querySelector(".tk-price").hidden = gtt || !["LIMIT", "SL"].includes(st.type);
      f.querySelector(".tk-trig").hidden = gtt || !["SL", "SL-M"].includes(st.type);
      f.querySelector(".tk-gtrig").hidden = f.querySelector(".tk-glimit").hidden = !gtt;
      f.querySelector(".tk-strig").hidden = f.querySelector(".tk-slimit").hidden = !oco;
      f.querySelector(".tk-gtrig label").textContent = oco ? "Target trigger" : "Trigger price";
      f.querySelector(".tk-glimit label").textContent = oco ? "Target limit (blank = market)" : "Limit price (blank = market)";
      f.querySelector(".tk-basket").hidden = gtt;
      f.querySelector(".tk-confirm").hidden = !live() || gtt;
      const qty = Number(f.qty.value) || 0;
      const p = gtt ? num("glimit") ?? num("gtrig") : ["LIMIT", "SL"].includes(st.type) ? Number(f.price.value) : st.type === "SL-M" ? Number(f.trigger.value) : ltp;
      const value = p ? qty * p : null, margin = value == null ? null : (opt || st.product === "DELIVERY" ? value : value * 0.2);
      m.querySelector("#tkEst").innerHTML = `<span>Order value <b class="num">${inr(value)}</b></span>
        <span>${opt || st.product === "DELIVERY" ? "Needs" : "Margin (5×)"} <b class="num">${st.side === "SELL" && (opt || st.product === "DELIVERY") ? "—" : inr(margin)}</b></span>
        <span>Available <b class="num">${funds?.available != null ? inr(funds.available) : "—"}</b></span>`;
      const go = f.querySelector(".tk-go");
      go.textContent = gtt ? `Create GTT` : `${live() ? "Place LIVE " : ""}${st.side === "BUY" ? "Buy" : "Sell"}`;
      go.className = `btn tk-go ${st.side === "BUY" ? "buy" : "sell"}`;
      f.querySelector(".tk-fine").textContent = gtt
        ? (oco ? "OCO: whichever of the target or the stop-loss triggers first places its order and cancels the other. Use it to protect a position you hold. "
          : "The order is placed when the price crosses the trigger. ") + "GTTs last a year and only fire during market hours. Funds are checked when they fire."
        : (opt ? "Buying an option pays the full premium. Writing (selling) one blocks 15% of the strike value as margin. "
          : "Intraday trades use 20% margin and close automatically at 3:20 pm. ") + "Charges and taxes aren't included.";
    };
    const order = () => {
      const body = { symbol, side: st.side, qty: Number(f.qty.value), order_type: st.type, product: st.product };
      if (["LIMIT", "SL"].includes(st.type)) body.price = Number(f.price.value);
      if (["SL", "SL-M"].includes(st.type)) body.trigger_price = Number(f.trigger.value);
      return body;
    };
    f.addEventListener("click", (e) => {
      const b = e.target.closest("button[type=button]"); if (!b) return;
      if (b.dataset.side) st.side = b.dataset.side;
      if (b.dataset.p) { st.product = b.dataset.p; store.set("kairo-ticket-product", st.product); }
      if (b.dataset.t) st.type = b.dataset.t;
      if (b.dataset.m) st.mode = b.dataset.m;
      if (b.dataset.g) st.gtt = b.dataset.g;
      if (b.dataset.basket != null) { addToBasket({ ...order(), label: name }); close(); return; }
      sync();
    });
    f.addEventListener("input", sync);
    f.onsubmit = async (e) => {
      e.preventDefault();
      const go = f.querySelector(".tk-go");
      if (st.mode === "gtt") {
        const body = { symbol, kind: st.gtt, side: st.side, qty: Number(f.qty.value), product: st.product, trigger: num("gtrig"), limit: num("glimit") };
        if (st.gtt === "oco") { body.stop_trigger = num("strig"); body.stop_limit = num("slimit"); }
        if (!body.trigger) { toast("Add a trigger price", "", "err"); return; }
        go.disabled = true;
        try {
          const g = await api("/trading/gtt", { method: "POST", body });
          toast("GTT created", st.gtt === "oco" ? `Target ${fmt(g.trigger)} · stop-loss ${fmt(g.stop_trigger)}` : `Triggers at ${fmt(g.trigger)}`, "ok");
          close(); refreshPanel();
        } catch (err) { toast("Couldn't create the GTT", err.message, "err"); go.disabled = false; }
        return;
      }
      if (live() && !f.confirm.checked) { toast("Confirm the live order", "Tick the box to confirm a real-money order.", "err"); return; }
      const body = { ...order(), confirm_live: live() && f.confirm.checked };
      go.disabled = true;
      try {
        const r = await api("/trading/orders", { method: "POST", body });
        if (r.mode === "upstox") toast("Order sent to Upstox", `Order id ${r.order_ids.join(", ")}`, "ok");
        else {
          const o = r.order;
          if (o.status === "rejected") { toast("Order rejected", o.message, "err"); go.disabled = false; return; }
          toast(o.status === "complete" ? `${o.side === "BUY" ? "Bought" : "Sold"} ${fmt(o.qty, 0)} ${name}` : "Order placed",
            o.status === "complete" ? `Filled at ${inr(o.fill_price)} (paper)` : `${o.order_type} order is open${o.message ? ": " + o.message : ""}`, "ok");
        }
        close(); refreshPanel();
      } catch (err) { toast("Order failed", err.message, "err"); go.disabled = false; }
    };
    sync();
  });
}

// ------------------------------------------------------------------ baskets
const draft = () => store.get("kairo-basket-draft", []);
const setDraft = (list) => { store.set("kairo-basket-draft", list); if (TR.sub === "baskets") render(); };
export function addToBasket(order) {
  const list = draft();
  if (list.length >= 20) { toast("Basket is full", "A basket holds up to 20 orders.", "err"); return; }
  setDraft([...list, order]);
  toast("Added to basket", `${list.length + 1} order${list.length ? "s" : ""} in it. Open Trade → Baskets to place them together.`, "ok");
}
/** Place several orders in one go (buy legs first, like brokers do, so hedges are in before the writes). */
export async function placeBasket(orders) {
  if (!TR.status) await refreshStatus();
  const n = orders.length;
  if (live() ? !confirm(`Place ${n} LIVE orders with real money through Upstox?`) : !confirm(`Place ${n} paper order${n > 1 ? "s" : ""}?`)) return null;
  const sorted = [...orders].sort((a, b) => (a.side === b.side ? 0 : a.side === "BUY" ? -1 : 1));
  try {
    const r = await api("/trading/basket", { method: "POST", body: { orders: sorted, confirm_live: live() } });
    const bad = r.orders.filter((o) => o.status === "rejected");
    toast(bad.length ? `${n - bad.length} of ${n} orders placed` : `${n} order${n > 1 ? "s" : ""} placed`,
      bad.length ? `Rejected: ${bad[0].message}${bad.length > 1 ? ` (+${bad.length - 1} more)` : ""}` : r.mode === "paper" ? "Paper trading" : "Sent to Upstox", bad.length ? "err" : "ok");
    refreshPanel();
    return r;
  } catch (err) { toast("Basket failed", err.message, "err"); return null; }
}
export async function saveBasket(orders, name = null) {
  name = name ?? prompt("Name this basket", `Basket ${new Date().toLocaleDateString([], { day: "numeric", month: "short" })}`);
  if (!name) return null;
  try { const b = await api("/trading/baskets", { method: "POST", body: { name: name.slice(0, 60), orders } }); toast("Basket saved", `${b.name} · ${b.orders.length} orders`, "ok"); refreshPanel(); return b; }
  catch (err) { toast("Couldn't save the basket", err.message, "err"); return null; }
}

// ------------------------------------------------------------------ positions / orders / holdings / funds
export function showTradePanel(box, openSymbol) {
  TR.box = box; TR.openSymbol = openSymbol;
  if (!TR.status) refreshStatus().then(render);
  render(); load();
}
export function hideTradePanel() { TR.box = null; clearTimeout(TR.timer); }
function refreshPanel() { if (TR.box) load(); }

async function load() {
  clearTimeout(TR.timer);
  if (!TR.box) return;
  const sub = TR.sub;
  try { TR.data[sub] = await api(`/trading/${sub}`); }
  catch (err) { TR.data[sub] = { error: err.message }; }
  if (sub !== TR.sub || !TR.box) return;
  render();
  TR.timer = setTimeout(load, sub === "funds" ? 10000 : 3000);
}

function render() {
  const box = TR.box; if (!box) return;
  const head = `<div class="tp-subs">${SUBS.map(([k, l]) => `<button data-sub="${k}" class="${k === TR.sub ? "on" : ""}">${l}</button>`).join("")}</div>`;
  const d = TR.data[TR.sub];
  let body = `<div class="skeleton" style="height:140px;margin-top:8px"></div>`;
  if (d?.error) body = `<div class="muted" style="padding:12px 2px">${esc(d.error)}</div>`;
  else if (d) body = { positions: positionsHtml, orders: ordersHtml, gtt: gttHtml, baskets: basketsHtml, holdings: holdingsHtml, funds: fundsHtml }[TR.sub](d);
  box.innerHTML = head + `<div class="tp-body">${body}</div>`;
  box.onclick = onClick;
}

function positionsHtml(d) {
  const rows = d.items || [];
  if (!rows.length) return `<div class="tp-empty">No positions today. Use Buy / Sell to place an order.</div>`;
  const total = rows.reduce((a, r) => a + (r.pnl || 0), 0);
  return `<div class="tp-total">Today's P&L <b class="num">${pnl(total)}</b></div>` + rows.map((r) => `
    <div class="tp-pos" data-sym="${esc(r.symbol)}">
      <div class="tp-l"><b>${esc(nameOf(r))}</b><small><span class="pill">${esc(r.product)}</span> ${r.qty ? `${r.qty > 0 ? "" : "Short "}${fmt(Math.abs(r.qty), 0)} @ ${fmt(r.avg)}` : "Closed"}</small></div>
      <div class="tp-r num">${pnl(r.pnl)}<small>LTP ${r.ltp != null ? fmt(r.ltp) : "—"}</small></div>
      ${r.qty && !live() ? `<button class="btn sm tp-exit" data-exit="${esc(r.symbol)}" data-product="${esc(r.product)}">Exit</button>` : ""}
    </div>`).join("");
}
function ordersHtml(d) {
  const rows = d.items || [];
  if (!rows.length) return `<div class="tp-empty">No orders yet.</div>`;
  return rows.map((o) => {
    const at = o.created_at ? new Date(o.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
    const px = o.order_type === "MARKET" ? "Market" : `${o.order_type}${o.price ? " " + fmt(o.price) : ""}${o.trigger_price ? " · trig " + fmt(o.trigger_price) : ""}`;
    const open = ["open", "triggered", "trigger pending", "pending"].includes(o.status);
    return `<div class="tp-ord" data-sym="${esc(o.symbol)}">
      <div class="tp-l"><b><span class="pill ${o.side === "BUY" ? "up" : "down"}">${o.side}</span> ${esc(nameOf(o))}</b>
        <small>${fmt(o.qty, 0)} · ${esc(px)} · ${esc(o.product || "")} · ${at}</small>${o.message ? `<small class="muted">${esc(o.message)}</small>` : ""}</div>
      <div class="tp-r"><span class="st st-${esc((o.status || "").replace(/\s+/g, "-"))}">${esc(o.status)}</span>${o.fill_price ? `<small class="num">@ ${fmt(o.fill_price)}</small>` : ""}</div>
      ${open ? `<button class="btn sm" data-cancel="${esc(o.id)}">Cancel</button>` : ""}</div>`;
  }).join("");
}
function gttHtml(d) {
  if (live()) return `<div class="tp-empty">GTT orders work in paper mode for now. Switch to Paper in Funds to use them.</div>`;
  const rows = d.items || [];
  if (!rows.length) return `<div class="tp-empty">No GTTs. In the order ticket choose <b>GTT</b> to set a trigger (or a target + stop-loss) that waits up to a year.</div>`;
  const active = rows.filter((g) => g.status === "active"), done = rows.filter((g) => g.status !== "active").slice(0, 20);
  const line = (g) => {
    const lim = (v) => (v ? ` → limit ${fmt(v)}` : " → market");
    const what = g.kind === "oco" ? `Target ${fmt(g.trigger)}${lim(g.limit)} · Stop ${fmt(g.stop_trigger)}${lim(g.stop_limit)}` : `Trigger ${fmt(g.trigger)}${lim(g.limit)}`;
    return `<div class="tp-ord" data-sym="${esc(g.symbol)}">
      <div class="tp-l"><b><span class="pill ${g.side === "BUY" ? "up" : "down"}">${g.side}</span> ${esc(nameOf(g))}${g.kind === "oco" ? ` <span class="pill">OCO</span>` : ""}</b>
        <small>${fmt(g.qty, 0)} · ${esc(what)} · ${esc(g.product)}</small>${g.message ? `<small class="muted">${esc(g.message)}</small>` : ""}</div>
      <div class="tp-r"><span class="st st-${esc(g.status)}">${esc(g.status)}</span>${g.status === "active" ? `<small class="muted">till ${new Date(g.expires_at).toLocaleDateString([], { day: "numeric", month: "short", year: "2-digit" })}</small>` : ""}</div>
      ${g.status === "active" ? `<button class="btn sm" data-cgtt="${g.id}">Cancel</button>` : ""}</div>`;
  };
  return (active.length ? `<div class="tp-sub">Active</div>${active.map(line).join("")}` : "") + (done.length ? `<div class="tp-sub">History</div>${done.map(line).join("")}` : "");
}
function basketsHtml(d) {
  const list = draft();
  const oline = (o, i, del) => `<div class="tp-ord bk-o"><div class="tp-l"><b><span class="pill ${o.side === "BUY" ? "up" : "down"}">${o.side}</span> ${esc(nameOf(o))}</b>
      <small>${fmt(o.qty, 0)} · ${o.order_type === "MARKET" || !o.order_type ? "Market" : `${esc(o.order_type)}${o.price ? " " + fmt(o.price) : ""}${o.trigger_price ? " · trig " + fmt(o.trigger_price) : ""}`} · ${esc(o.product || "DELIVERY")}</small></div>
      ${del ? `<button class="btn sm" data-drop="${i}" aria-label="Remove">✕</button>` : ""}</div>`;
  const cur = `<div class="tp-sub">Current basket</div>` + (list.length
    ? list.map((o, i) => oline(o, i, true)).join("") + `<div class="bk-act"><button class="btn sm primary" data-bplace>Place all ${list.length}</button>
        <button class="btn sm" data-bsave>Save</button><button class="btn sm" data-bclear>Clear</button></div>`
    : `<div class="tp-empty">Empty. Use <b>Add to basket</b> in the order ticket, or build an options strategy in the full option chain.</div>`);
  const saved = d.items || [];
  return cur + `<div class="tp-sub">Saved baskets</div>` + (saved.length ? saved.map((b) => `<div class="bk">
      <div class="bk-h"><b>${esc(b.name)}</b><small class="muted">${b.orders.length} order${b.orders.length === 1 ? "" : "s"}</small>
        <span class="term-sp"></span><button class="btn sm primary" data-splace="${b.id}">Place</button><button class="btn sm" data-sload="${b.id}" title="Copy into the current basket to edit">Edit</button>
        <button class="btn sm" data-sdel="${b.id}" aria-label="Delete">✕</button></div>
      ${b.orders.map((o, i) => oline(o, i, false)).join("")}</div>`).join("") : `<div class="tp-empty">None saved yet.</div>`);
}
function holdingsHtml(d) {
  const rows = d.items || [];
  if (!rows.length) return `<div class="tp-empty">No holdings. Delivery buys become holdings the next day.</div>`;
  const inv = rows.reduce((a, r) => a + (r.invested || 0), 0), val = rows.reduce((a, r) => a + (r.value || 0), 0);
  return `<div class="tp-total">Invested <b class="num">${inr(inv)}</b> · Current <b class="num">${inr(val)}</b> <b class="num">${pnl(val - inv)}</b></div>` + rows.map((r) => `
    <div class="tp-pos" data-sym="${esc(r.symbol)}"><div class="tp-l"><b>${esc(nameOf(r))}</b><small>${fmt(r.qty, 0)} @ ${fmt(r.avg)}</small></div>
      <div class="tp-r num">${pnl(r.pnl)}<small>LTP ${r.ltp != null ? fmt(r.ltp) : "—"}${r.day_change != null ? ` · today ${r.day_change >= 0 ? "+" : "−"}${fmt(Math.abs(r.day_change))}` : ""}</small></div></div>`).join("");
}
function fundsHtml(d) {
  const s = TR.status || {}, up = s.upstox || {};
  const row = (k, v) => `<div class="tp-row"><span class="muted">${k}</span><b class="num">${v}</b></div>`;
  const money_ = live()
    ? row("Available margin", inr(d.available)) + row("Used margin", inr(d.margin_used)) + row("Pay-in today", inr(d.payin))
    : row("Account value", inr(d.account_value)) + row("Available", inr(d.available)) + row("Cash", inr(d.cash)) + row("Intraday margin used", inr(d.margin_used))
      + row("Held for open orders", inr(d.reserved)) + row("Realised P&L", pnl(d.realised)) + row("Unrealised P&L", pnl(d.unrealised)) + row("Starting cash", inr(d.start_cash));
  const broker = !up.configured
    ? `<p class="fine">To trade with real money, create a free app at <b>account.upstox.com/developer/apps</b> with the redirect URL
       <code>http://127.0.0.1:8000/broker/upstox/callback</code>, then put its key and secret in <code>.env</code> as UPSTOX_API_KEY and UPSTOX_API_SECRET and restart Kairo.</p>`
    : !up.connected
      ? `<p class="fine">Upstox needs a fresh login every day (its tokens expire at 3:30 am).</p><a class="btn primary" href="/broker/upstox/login">Connect Upstox</a>`
      : `<div class="tp-row"><span class="muted">Connected</span><b>${esc(up.user || "Upstox")}</b></div>
         <div class="tp-row"><span class="muted">Session ends</span><b>${up.expires_at ? new Date(up.expires_at).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—"}</b></div>
         <div class="seg tp-modes"><button data-mode="paper" class="${live() ? "" : "on"}">Paper</button><button data-mode="upstox" class="${live() ? "on" : ""}">Live</button></div>
         <button class="btn sm" data-logout style="margin-top:8px">Disconnect</button>`;
  return money_ + (live() ? "" : `<button class="btn sm" data-reset style="margin-top:12px">Reset paper account</button>`)
    + `<div class="tp-sub">Real trading · Upstox</div>${broker}`;
}

async function onClick(e) {
  const t = e.target;
  const sub = t.closest("[data-sub]");
  if (sub) { TR.sub = sub.dataset.sub; store.set("kairo-trade-sub", TR.sub); render(); load(); return; }
  const ex = t.closest("[data-exit]");
  if (ex) {
    try { const r = await api("/trading/exit", { method: "POST", body: { symbol: ex.dataset.exit, product: ex.dataset.product } });
      toast("Position closed", `${r.order.side === "BUY" ? "Bought" : "Sold"} at ${inr(r.order.fill_price)} (paper)`, "ok"); }
    catch (err) { toast("Couldn't exit", err.message, "err"); }
    load(); return;
  }
  const cancel = t.closest("[data-cancel]");
  if (cancel) {
    try { await api(`/trading/orders/${encodeURIComponent(cancel.dataset.cancel)}`, { method: "DELETE" }); toast("Order cancelled", "", "ok"); }
    catch (err) { toast("Couldn't cancel", err.message, "err"); }
    load(); return;
  }
  const cg = t.closest("[data-cgtt]");
  if (cg) {
    try { await api(`/trading/gtt/${cg.dataset.cgtt}`, { method: "DELETE" }); toast("GTT cancelled", "", "ok"); }
    catch (err) { toast("Couldn't cancel", err.message, "err"); }
    load(); return;
  }
  const drop = t.closest("[data-drop]");
  if (drop) { const l = draft(); l.splice(Number(drop.dataset.drop), 1); setDraft(l); return; }
  if (t.closest("[data-bclear]")) { if (confirm("Clear the current basket?")) setDraft([]); return; }
  if (t.closest("[data-bplace]")) { if (await placeBasket(draft())) setDraft([]); return; }
  if (t.closest("[data-bsave]")) { if (await saveBasket(draft())) setDraft([]); return; }
  const saved = (attr) => { const el = t.closest(`[${attr}]`); return el && (TR.data.baskets?.items || []).find((b) => String(b.id) === el.getAttribute(attr)); };
  let b;
  if ((b = saved("data-splace"))) { placeBasket(b.orders); return; }
  if ((b = saved("data-sload"))) { setDraft(b.orders.slice()); toast("Copied to the current basket", "Edit it, then Save to keep it as a new basket.", "ok"); return; }
  if ((b = saved("data-sdel"))) {
    if (!confirm(`Delete the basket "${b.name}"?`)) return;
    try { await api(`/trading/baskets/${b.id}`, { method: "DELETE" }); } catch (err) { toast("Couldn't delete", err.message, "err"); }
    load(); return;
  }
  if (t.closest("[data-reset]")) {
    const v = prompt("Start the paper account again with how much virtual cash (₹)?", String(TR.status?.paper?.start_cash || 1000000));
    if (v == null) return;
    try { await api("/trading/paper/reset", { method: "POST", body: { start_cash: Number(v) } }); toast("Paper account reset", "", "ok"); await refreshStatus(); }
    catch (err) { toast("Couldn't reset", err.message, "err"); }
    load(); return;
  }
  const mode = t.closest("[data-mode]");
  if (mode && mode.dataset.mode !== TR.status?.mode) {
    if (mode.dataset.mode === "upstox" && !confirm("Switch to LIVE trading? Orders will go to your Upstox account and use real money.")) return;
    try { await api("/trading/mode", { method: "POST", body: { mode: mode.dataset.mode } }); await refreshStatus(); toast(mode.dataset.mode === "upstox" ? "Live trading on" : "Paper trading on", "", "ok"); }
    catch (err) { toast("Couldn't switch", err.message, "err"); }
    TR.data = {}; load(); return;
  }
  if (t.closest("[data-logout]")) {
    try { await api("/broker/upstox/logout", { method: "POST" }); await refreshStatus(); toast("Upstox disconnected", "Back to paper trading.", "ok"); } catch { /* ignore */ }
    TR.data = {}; load(); return;
  }
  const row = t.closest("[data-sym]");
  if (row && TR.openSymbol && !t.closest("button")) TR.openSymbol(row.dataset.sym);
}
