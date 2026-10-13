// Trade journal: the paper account's round-trip trades with your notes, and what the numbers say.
import { $, esc, api, fmt, pctTxt, tone, store, toast, splitSym, logoHtml } from "./core.js";

const J = { data: null, filter: store.get("kairo-journal-filter", "all"), open: null };
const rs = (v) => (v == null ? "—" : `${v < 0 ? "−" : v > 0 ? "+" : ""}₹${Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`);
const when = (iso) => new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const hold = (m) => (m == null ? "—" : m < 60 ? `${Math.round(m)} min` : m < 1440 ? `${(m / 60).toFixed(1)} h` : `${(m / 1440).toFixed(1)} days`);
const KAI = `<svg viewBox="0 0 40 40" width="30" height="30" aria-hidden="true"><circle cx="20" cy="22" r="14" fill="var(--brand)"/><circle cx="15" cy="21" r="2" fill="#fff"/><circle cx="25" cy="21" r="2" fill="#fff"/><path d="M14.5 27c3 2.6 8 2.6 11 0" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round"/><path d="M6 9.5 20 4l14 5.5-14 5.5z" fill="var(--text)"/></svg>`;

export function initJournal() {
  $("#journalBody").addEventListener("click", onClick);
}
export async function showJournal() {
  document.title = "Trade journal · Kairo Markets";
  if (!J.data) $("#journalBody").innerHTML = `<div class="card"><div class="skeleton" style="height:240px"></div></div>`;
  try { J.data = await api("/journal"); } catch (err) { $("#journalBody").innerHTML = `<div class="card" style="color:var(--down)">${esc(err.message)}</div>`; return; }
  render();
}

function render() {
  const d = J.data, s = d.stats;
  if (!d.trades.length) {
    $("#journalBody").innerHTML = `<div class="card empty-state">${KAI}<h3>No trades yet</h3><p>Trades you make in paper mode show up here automatically: entry, exit, P&amp;L, and (with a super order) the risk you took. Tag them to find out what works for you.</p>
      <div class="actions"><a class="btn primary" href="#/terminal">Open the Terminal</a><a class="btn" href="#/learn/psych">Why keep a journal?</a></div></div>`;
    return;
  }
  const tile = (k, v, cls = "", sub = "") => `<div class="jr-tile"><div class="k">${k}</div><div class="v num ${cls}">${v}</div>${sub ? `<small class="muted">${sub}</small>` : ""}</div>`;
  const tiles = s.trades ? `<div class="jr-tiles">
      ${tile("Net P&L", rs(s.total), tone(s.total), `${s.trades} closed · ${s.open} open`)}
      ${tile("Win rate", `${fmt(s.win_rate, 0)}%`, "", `${s.wins} won · ${s.losses} lost`)}
      ${tile("Expectancy", rs(s.expectancy), tone(s.expectancy), "average per trade")}
      ${tile("Avg win / loss", `${rs(s.avg_win)} / ${rs(s.avg_loss)}`)}
      ${tile("Profit factor", s.profit_factor != null ? fmt(s.profit_factor, 2) : "—", s.profit_factor >= 1 ? "up" : "down", "gross wins ÷ gross losses")}
      ${tile("Average R", s.avg_r != null ? `${s.avg_r > 0 ? "+" : ""}${fmt(s.avg_r, 2)}R` : "—", tone(s.avg_r), `${s.with_stop} trades with a stop`)}
      ${tile("Max drawdown", rs(s.max_drawdown), s.max_drawdown < 0 ? "down" : "", `worst losing streak ${s.worst_losing_streak}`)}
      ${tile("Avg hold", `${hold(s.avg_hold_win)} / ${hold(s.avg_hold_loss)}`, "", "winners / losers")}</div>` : "";
  const ins = s.insights?.length ? `<div class="card jr-ins">${KAI}<div><b>What Kai sees in your trades</b><ul>${s.insights.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div></div>` : "";
  const table = (title, rows, empty) => `<div class="card"><div class="card-t" style="margin-bottom:8px">${title}</div>${rows.length ? `<table><tr><th></th><th class="r">Trades</th><th class="r">Win rate</th><th class="r">P&L</th></tr>
      ${rows.map((r) => `<tr><td>${esc(r.name)}</td><td class="r num">${r.trades}</td><td class="r num">${fmt(r.win_rate, 0)}%</td><td class="r num ${tone(r.pnl)}">${rs(r.pnl)}</td></tr>`).join("")}</table>` : `<div class="muted jr-sub">${empty}</div>`}</div>`;
  const list = d.trades.filter((t) => J.filter === "all" || (J.filter === "open" ? t.status === "open" : J.filter === "wins" ? t.pnl > 0 : t.status === "closed" && t.pnl <= 0));
  $("#journalBody").innerHTML = `${tiles}${ins}
    ${s.trades >= 2 ? `<div class="card"><div class="card-h"><span class="card-t">Equity curve</span><span class="muted jr-sub">cumulative P&L of closed trades</span></div><div class="jr-chart">${equityChart(s.equity)}<div class="pl-tip" hidden></div></div></div>` : ""}
    ${s.trades ? `<div class="grid2">${table("By setup", s.by_setup, "")}${table("By mistake", s.by_mistake, "Tag mistakes on your trades to see what they cost.")}</div>
      <div class="grid2">${table("By hour opened", s.by_hour, "")}${table("By stock", s.by_symbol, "")}</div>` : ""}
    <div class="card"><div class="card-h"><span class="card-t">Trades</span><div class="seg" id="jrFilter">${[["all", "All"], ["wins", "Wins"], ["losses", "Losses"], ["open", "Open"]]
      .map(([k, l]) => `<button data-f="${k}" class="${k === J.filter ? "on" : ""}">${l}</button>`).join("")}</div></div>
      ${list.map(tradeHtml).join("") || `<div class="empty" style="height:80px">Nothing here.</div>`}</div>
    <p class="muted jr-sub">${esc(d.about)}</p>`;
  wireChart();
}

function tradeHtml(t) {
  const n = t.notes || {}, editing = J.open === t.key, d = J.data;
  const res = t.status === "open" ? `<span class="chip warn">Open</span>` : `<b class="num ${tone(t.pnl)}">${rs(t.pnl)}</b><small class="num ${tone(t.pnl_pct)}">${pctTxt(t.pnl_pct)}${t.r != null ? ` · ${t.r > 0 ? "+" : ""}${fmt(t.r, 1)}R` : ""}</small>`;
  return `<div class="jr-trade ${editing ? "on" : ""}" data-key="${esc(t.key)}">
    <div class="jr-row" data-edit="${esc(t.key)}">${logoHtml(t.symbol, splitSym(t.symbol)[0], 28)}
      <div class="jr-id"><b>${esc(splitSym(t.symbol)[0])}</b> <span class="pill ${t.side === "LONG" ? "up" : "down"}">${t.side}</span><span class="pill">${esc(t.product)}</span>
        <small class="muted">${when(t.opened_at)} · ${fmt(t.qty, 0)} @ ${fmt(t.entry)}${t.exit != null ? ` → ${fmt(t.exit)}` : ""}${t.hold_minutes != null ? ` · ${hold(t.hold_minutes)}` : ""}</small>
        ${n.setup || n.mistakes?.length ? `<small>${n.setup ? `<span class="pill">${esc(n.setup)}</span>` : ""}${(n.mistakes || []).map((m) => `<span class="pill down">${esc(m)}</span>`).join("")}</small>` : ""}</div>
      <div class="jr-res">${res}</div></div>
    ${editing ? `<div class="jr-edit">
      <label>Setup <select data-in="setup"><option value="">—</option>${d.setups.map((x) => `<option ${x === n.setup ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></label>
      <div class="jr-mist">${d.mistakes.map((m) => `<label><input type="checkbox" data-mist value="${esc(m)}" ${(n.mistakes || []).includes(m) ? "checked" : ""}> ${esc(m)}</label>`).join("")}</div>
      <label>How did you execute? <span class="jr-stars">${[1, 2, 3, 4, 5].map((i) => `<button type="button" data-star="${i}" class="${(n.rating || 0) >= i ? "on" : ""}" aria-label="${i} of 5">★</button>`).join("")}</span></label>
      <textarea class="input" data-in="note" rows="3" placeholder="Why did you take it? What would you do differently?">${esc(n.note || "")}</textarea>
      <div class="actions"><button class="btn primary sm" data-save="${esc(t.key)}">Save notes</button><button class="btn sm" data-edit="${esc(t.key)}">Close</button>
        ${t.stop ? `<span class="muted jr-sub">Initial stop ${fmt(t.stop)} · risk ${rs(t.risk)}</span>` : `<span class="muted jr-sub">No stop recorded (use a super order to set one).</span>`}</div></div>` : ""}</div>`;
}

function equityChart(points) {
  const W = 760, H = 180, L = 56, R = 10, T = 10, B = 22, n = points.length;
  const vals = points.map((p) => p.equity), lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), pad = (hi - lo || 1) * 0.1;
  const x = (i) => L + (i / Math.max(1, n - 1)) * (W - L - R), y = (v) => T + ((hi + pad - v) / (hi - lo + 2 * pad)) * (H - T - B);
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join("");
  const last = points[n - 1];
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Equity curve">
    <line x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" class="pl-zero"/>
    <text x="${L - 6}" y="${y(hi) + 4}" class="pl-ax" text-anchor="end">${rs(hi)}</text><text x="${L - 6}" y="${y(0) + 4}" class="pl-ax" text-anchor="end">0</text>
    ${lo < 0 ? `<text x="${L - 6}" y="${y(lo) + 4}" class="pl-ax" text-anchor="end">${rs(lo)}</text>` : ""}
    <path d="${path}" fill="none" stroke="var(--brand)" stroke-width="2"/>
    <circle cx="${x(n - 1)}" cy="${y(last.equity)}" r="4" fill="var(--brand)"/>
    <text x="${x(n - 1) - 6}" y="${y(last.equity) - 8}" class="pl-ax" text-anchor="end">${rs(last.equity)}</text>
    ${points.map((p, i) => `<rect x="${x(i) - (W - L - R) / Math.max(1, n - 1) / 2}" y="${T}" width="${(W - L - R) / Math.max(1, n - 1)}" height="${H - T - B}" fill="transparent" data-tip="${esc(`${when(p.at)} · ${rs(p.equity)}`)}"/>`).join("")}</svg>`;
}
function wireChart() {
  const wrap = document.querySelector(".jr-chart"); if (!wrap) return;
  const tip = wrap.querySelector(".pl-tip");
  wrap.onmousemove = (e) => {
    const t = e.target.closest?.("[data-tip]"); if (!t) { tip.hidden = true; return; }
    const r = wrap.getBoundingClientRect();
    tip.hidden = false; tip.textContent = t.dataset.tip;
    tip.style.left = `${Math.min(e.clientX - r.left + 12, r.width - 190)}px`; tip.style.top = `${e.clientY - r.top - 34}px`;
  };
  wrap.onmouseleave = () => (tip.hidden = true);
}

async function onClick(e) {
  const t = e.target;
  const f = t.closest("[data-f]"); if (f) { J.filter = f.dataset.f; store.set("kairo-journal-filter", J.filter); render(); return; }
  const star = t.closest("[data-star]");
  if (star) { star.parentElement.querySelectorAll("button").forEach((b) => b.classList.toggle("on", Number(b.dataset.star) <= Number(star.dataset.star))); star.parentElement.dataset.v = star.dataset.star; return; }
  const save = t.closest("[data-save]");
  if (save) {
    const box = save.closest(".jr-trade"), stars = box.querySelector(".jr-stars");
    const body = { setup: box.querySelector('[data-in="setup"]').value, note: box.querySelector('[data-in="note"]').value,
      mistakes: [...box.querySelectorAll("[data-mist]:checked")].map((x) => x.value),
      rating: Number(stars.dataset.v || stars.querySelectorAll(".on").length) || null };
    try { await api(`/journal/${encodeURIComponent(save.dataset.save)}`, { method: "PUT", body }); toast("Notes saved", "", "ok"); J.open = null; showJournal(); }
    catch (err) { toast("Couldn't save", err.message, "err"); }
    return;
  }
  const ed = t.closest("[data-edit]");
  if (ed && !t.closest(".jr-edit select, .jr-edit textarea, .jr-edit input")) { J.open = J.open === ed.dataset.edit ? null : ed.dataset.edit; render(); }
}
