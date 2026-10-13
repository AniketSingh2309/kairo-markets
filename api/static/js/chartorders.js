// Trading on the Terminal chart: open orders and super-order legs drawn as price lines, target / stop-loss
// handles you drag to modify a super order, and a "trade at a price" menu for a click on the chart.
import { $, api, fmt, toast, css } from "./core.js";
import { openTicket, tradable } from "./trading.js";

const O = { h: null, supers: [], orders: [], lines: [], timer: 0, drag: null };

/** `hooks`: { sym(), label(), ltp(), chart(), main() (the price series), visible() }. */
export function initChartOrders(hooks) {
  O.h = hooks;
  window.addEventListener("kairo:orders", () => refreshOrders());
  setInterval(() => { if (O.h.visible()) placeHandles(); }, 700);  // the price scale rescales as candles arrive
  O.timer = setInterval(() => { if (O.h.visible()) refreshOrders(); }, 5000);
}

export async function refreshOrders() {
  const sym = O.h.sym();
  if (!sym || !tradable(sym)) { O.supers = []; O.orders = []; drawOrders(); return; }
  try {
    const [s, o] = await Promise.all([api("/trading/super"), api("/trading/orders")]);
    if (sym !== O.h.sym()) return;
    O.supers = s.items.filter((x) => x.symbol === sym && ["pending", "active"].includes(x.status));
    const superEntries = new Set(O.supers.map((x) => x.status === "pending" && x.entry_price).filter(Boolean));
    O.orders = o.mode === "paper" ? o.items.filter((x) => x.symbol === sym && ["open", "triggered"].includes(x.status)
      && !(x.order_type === "LIMIT" && superEntries.has(x.price))) : [];
  } catch { return; }
  drawOrders();
}

/** Price lines for open orders and super legs (redrawn after every chart rebuild). */
export function drawOrders() {
  const main = O.h?.main();
  O.lines.forEach((l) => { try { main?.removePriceLine(l); } catch { /* chart rebuilt */ } });
  O.lines = [];
  if (!main) { placeHandles(); return; }
  const line = (price, color, title, style = 0) => O.lines.push(main.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
  for (const x of O.supers) {
    if (x.status === "pending" && x.entry_price) line(x.entry_price, css("--brand"), `ENTRY ${x.side} ${fmt(x.qty, 0)}`, 1);
    line(x.target, css("--up"), "TARGET", 2);
    line(x.stop, css("--down"), x.trail ? "TRAIL SL" : "STOP", 2);
  }
  for (const o of O.orders) {
    const px = o.order_type === "LIMIT" ? o.price : o.trigger_price ?? o.price;
    if (px) line(px, o.side === "BUY" ? css("--up") : css("--down"), `${o.side} ${o.order_type} ${fmt(o.qty, 0)}`, 1);
  }
  placeHandles();
}

// ------------------------------------------------------------------ draggable target / stop handles
function layer() {
  let el = $("#termOrd");
  if (!el) {
    el = document.createElement("div"); el.id = "termOrd"; el.className = "term-ord";
    document.querySelector("#view-terminal .term-main")?.appendChild(el);
    el.addEventListener("pointerdown", startDrag);
  }
  return el;
}
function placeHandles() {
  const el = layer(), main = O.h?.main(), chart = O.h?.chart(), box = $("#termChart");
  if (!main || !chart || !box || O.drag) return;
  const right = (chart.priceScale("right").width() || 60) + 6;
  const legs = O.supers.flatMap((x) => [{ x, leg: "target", price: x.target }, { x, leg: "stop", price: x.stop }]);
  el.innerHTML = legs.map(({ x, leg, price }) => {
    const y = main.priceToCoordinate(price);
    if (y == null || y < 0 || y > box.clientHeight) return "";
    return `<button class="ord-h ${leg}" style="top:${box.offsetTop + y}px;right:${right}px" data-id="${x.id}" data-leg="${leg}"
      title="Drag to move the ${leg === "stop" ? "stop-loss" : "target"}">${leg === "stop" ? (x.trail ? "Trail SL" : "SL") : "TGT"} ${fmt(price)} <i>⇕</i></button>`;
  }).join("");
}
function startDrag(e) {
  const b = e.target.closest(".ord-h"); if (!b) return;
  e.preventDefault();
  const x = O.supers.find((s) => String(s.id) === b.dataset.id); if (!x) return;
  const main = O.h.main(), box = $("#termChart"), leg = b.dataset.leg;
  O.drag = { x, leg, price: x[leg], b };
  b.setPointerCapture(e.pointerId);
  b.classList.add("dragging");
  const move = (ev) => {
    const y = ev.clientY - box.getBoundingClientRect().top, p = main.coordinateToPrice(y);
    if (p == null) return;
    O.drag.price = Math.round(p * 20) / 20;
    b.style.top = `${box.offsetTop + y}px`;
    b.firstChild.textContent = `${leg === "stop" ? (x.trail ? "Trail SL" : "SL") : "TGT"} ${fmt(O.drag.price)} `;
  };
  const up = async () => {
    b.removeEventListener("pointermove", move); b.removeEventListener("pointerup", up); b.removeEventListener("pointercancel", up);
    const { price } = O.drag; O.drag = null;
    if (Math.abs(price - x[leg]) < 0.04) { placeHandles(); return; }
    try {
      const r = await api(`/trading/super/${x.id}`, { method: "PATCH", body: { [leg]: price } });
      toast(`${leg === "stop" ? "Stop-loss" : "Target"} moved`, `${fmt(x[leg])} → ${fmt(r[leg])}`, "ok");
    } catch (err) { toast(`Couldn't move the ${leg === "stop" ? "stop-loss" : "target"}`, err.message, "err"); }
    refreshOrders();
  };
  b.addEventListener("pointermove", move); b.addEventListener("pointerup", up); b.addEventListener("pointercancel", up);
}

// ------------------------------------------------------------------ trade at a price (click on the chart)
export function tradeAtPrice(price, x, y) {
  const sym = O.h.sym(), ltp = O.h.ltp();
  let menu = $("#termTradeMenu");
  if (!menu) {
    menu = document.createElement("div"); menu.id = "termTradeMenu"; menu.className = "term-menu term-trademenu";
    document.querySelector("#view-terminal .term-main")?.appendChild(menu);
  }
  const p = Math.round(price * 20) / 20, above = ltp != null && p > ltp, can = tradable(sym);
  const opt = (k, label, hint) => `<button data-k="${k}"><b>${label}</b><small class="muted">${hint}</small></button>`;
  menu.innerHTML = `<div class="ttm-h">At <b class="num">${fmt(p)}</b>${ltp != null ? ` <span class="muted">· LTP ${fmt(ltp)}</span>` : ""}</div>
    ${can ? (above
      ? opt("buy-stop", `Buy stop @ ${fmt(p)}`, "Buys if price rises to here (breakout)") + opt("sell-limit", `Sell limit @ ${fmt(p)}`, "Sells if price rises to here")
      : opt("buy-limit", `Buy limit @ ${fmt(p)}`, "Buys if price dips to here") + opt("sell-stop", `Sell stop @ ${fmt(p)}`, "Sells if price falls to here (stop-loss)"))
      + opt("super", `Super buy @ ${fmt(p)}`, "Limit entry with a target and stop-loss") : `<div class="muted ttm-note">Indices can't be traded; alerts still work.</div>`}
    ${opt("alert", `Alert when price ${above ? "rises to" : "falls to"} ${fmt(p)}`, "In-app and desktop notification")}`;
  menu.hidden = false;
  const host = menu.parentElement;
  menu.style.left = `${Math.min(x + 12, host.clientWidth - 250)}px`;
  menu.style.top = `${Math.min(y + 12, host.clientHeight - menu.offsetHeight - 40)}px`;
  const close = () => { menu.hidden = true; document.removeEventListener("pointerdown", outside, true); };
  const outside = (e) => { if (!menu.contains(e.target)) close(); };
  setTimeout(() => document.addEventListener("pointerdown", outside, true), 0);
  menu.onclick = async (e) => {
    const k = e.target.closest("button")?.dataset.k; if (!k) return;
    close();
    const base = { symbol: sym, ltp, label: O.h.label() };
    if (k === "buy-limit") openTicket({ ...base, side: "BUY", preset: { type: "LIMIT", price: p } });
    if (k === "sell-limit") openTicket({ ...base, side: "SELL", preset: { type: "LIMIT", price: p } });
    if (k === "buy-stop") openTicket({ ...base, side: "BUY", preset: { type: "SL-M", trigger: p } });
    if (k === "sell-stop") openTicket({ ...base, side: "SELL", preset: { type: "SL-M", trigger: p } });
    if (k === "super") {
      const risk = Math.max(p * 0.01, ltp != null ? Math.abs(ltp - p) * 0.5 : 0) || p * 0.01;
      openTicket({ ...base, side: "BUY", preset: { mode: "super", type: "LIMIT", price: p, stop: p - risk, target: p + 2 * risk } });
    }
    if (k === "alert") {
      try {
        await api("/alerts", { method: "POST", body: { symbol: sym, kind: above ? "price_above" : "price_below", threshold: p, note: "Set from the chart" } });
        toast("Alert set", `${O.h.label()} ${above ? "rises to" : "falls to"} ${fmt(p)}`, "ok");
      } catch (err) { toast("Couldn't set the alert", err.message, "err"); }
    }
  };
}
