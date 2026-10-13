// Kai, the live trading teacher. When switched on, Kai reads the Terminal chart on every tick (coach.js)
// and coaches the user through it:
//  * a one-line read in the chart's bottom bar, updated every candle (bias, nearest support/resistance);
//  * pop-up tips pinned to the candle where a pattern or event just happened, each with what to do next;
//  * a full plan card (what I see, what to do next, odds check), on demand or when the read changes;
//  * when the market is closed, a recap of the last session and a plan for the next one.
// Tips disappear when the user clicks OK. Rules only (no model), so every tip describes the chart on screen.
import { esc, store, fmt, api } from "./core.js";
import { analyse } from "./coach.js";
import { openTicket, tradable } from "./trading.js";

const GAP_MS = 45_000;                // quiet time after a tip is closed
const RULE_COOLDOWN_MS = 10 * 60_000; // the same kind of event on the same symbol
const LESSON_GAP_MS = 3 * 60_000;     // idle time before an evergreen lesson
const CHECK_MS = 2_500;               // live ticks are read at most this often
const CACHE_MS = 10 * 60_000;

const K = {
  on: store.get("kairo-kai", false), api: null, el: null, dot: null, open: null, sym: "", read: null,
  lastClosed: 0, lastCheck: 0, fired: new Map(), seen: new Set(store.get("kairo-kai-seen", [])),
  planned: new Set(), bias: new Map(), cache: new Map(),
};

const AVATAR = `<svg viewBox="0 0 40 40" width="34" height="34" aria-hidden="true">
  <circle cx="20" cy="22" r="14" fill="var(--brand)"/>
  <circle cx="15" cy="21" r="2" fill="#fff"/><circle cx="25" cy="21" r="2" fill="#fff"/>
  <path d="M14.5 27c3 2.6 8 2.6 11 0" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round"/>
  <path d="M6 9.5 20 4l14 5.5-14 5.5z" fill="var(--text)"/><path d="M31 11v6" stroke="var(--text)" stroke-width="1.6"/>
  <circle cx="31" cy="18" r="1.6" fill="var(--warn)"/></svg>`;

const indian = (sym) => /\.(NS|BO)$/.test(sym) || sym.startsWith("OPT:") || /^\^(NSE|BSESN|CNX)/.test(sym);
const isOpt = (sym) => sym.startsWith("OPT:");
const f = (v) => fmt(v);
// Kai's own tone classes: the app's global .up / .down would colour every bit of text inside the card
const toneOf = (dir) => (dir > 0 || dir === "up" ? "is-up" : dir < 0 || dir === "down" ? "is-down" : "");

// ------------------------------------------------------------------ background data (daily context, odds, capital)
function cached(key, load) {
  const hit = K.cache.get(key);
  if (hit && (hit.value !== undefined || Date.now() - hit.at < 30_000) && Date.now() - hit.at < CACHE_MS) return hit.value;
  K.cache.set(key, { at: Date.now(), value: undefined });
  const done = (value) => { K.cache.set(key, { at: Date.now(), value }); setTimeout(() => consider(true), 0); };  // re-read with the new data
  load().then(done, () => done(null));
  return undefined;
}
// One year of daily candles (the stock page's cached 1Y chart): enough for the daily trend and 52-week range,
// and much lighter than the Terminal's full-history 1D series.
const dailyBars = (sym) => cached(`d:${sym}`, async () => {
  const d = await api(`/chart/${encodeURIComponent(sym)}?range=1Y`);
  return d.bars.map((b) => ({ time: Math.floor(Date.parse(b.t) / 1000), open: b.o, high: b.h, low: b.l, close: b.c, vol: b.v || 0 }));
});
const forecast = (sym) => cached(`f:${sym}`, async () => (await api(`/forecast/${encodeURIComponent(sym)}?horizon_days=5`)).forecast);
const indiaFlags = (sym) => cached(`i:${sym}`, async () => (await api(`/india/stock/${encodeURIComponent(sym)}`)).flags);
const capital = () => cached("funds", async () => { const x = await api("/trading/funds"); return x.available ?? x.cash ?? null; });

// ------------------------------------------------------------------ evergreen tips & tricks (each shown once, ever)
const LESSONS = [
  ["l-candle", "How to read a candle", "Each candle is one time slot. The body runs from open to close: green if it closed higher, red if lower. Wicks show the extremes. Long wicks mean price went there and was rejected."],
  ["l-volume", "Price says what, volume says how much", "The bars at the bottom are volume. A move on rising volume has more participants behind it; a breakout on thin volume fails more often."],
  ["l-levels", "Support and resistance", "Prices often stall where they turned before. Kai draws the nearest levels on your chart (the dashed lines). Try the horizontal line tool on the left to mark your own."],
  ["l-trend", "Trade with the trend", "Higher highs and higher lows mean an uptrend; buy dips, not spikes. Lower highs and lower lows mean a downtrend; don't buy just because it looks cheap."],
  ["l-risk", "Plan the exit before the entry", "Before every trade: entry, stop-loss, target. Size the trade so hitting the stop costs about 1% of your capital. You can be wrong half the time and still do fine if losses stay small."],
  ["l-timeframe", "Zoom out before you zoom in", "Check the 1D chart for the big trend and key levels, then use 5m or 15m to time an entry. A huge move on 1-minute candles can be a ripple on the daily."],
  ["l-index", "Check the market first", "Most stocks move with the index. Buying a stock while NIFTY is falling hard means swimming against the current. Glance at the index strip before you trade."],
  ["l-average", "Never average down a losing trade", "Adding to a loser to lower your average price turns a small, planned loss into a big, unplanned one. If the stop is hit, exit and reassess."],
  ["l-stops", "Stops go where the idea is wrong", "Put a stop just beyond the level that proves you wrong (under support for a buy), not at a round rupee amount you're comfortable losing. Then size the trade to fit it."],
  ["l-confirm", "Wait for the candle to close", "A pattern isn't real until its candle closes. Breakouts that look great mid-candle often reverse before the close. Patience filters out most false signals."],
  ["l-fewer", "Fewer, better trades", "Overtrading is the most common beginner mistake. Skip setups where reward isn't at least twice the risk. Doing nothing is a valid decision."],
  ["l-journal", "Keep a journal", "Write down why you entered, where your stop was and how it ended. After 20 trades you'll see which setups actually work for you."],
  ["l-gaps", "Gaps and the open", "When a stock opens far from yesterday's close (a gap), the first minutes are chaotic. Many gaps partly fill. Let the opening range form before acting."],
  ["l-news", "Results and news days", "On results or big-news days, candles get huge and patterns stop working. Size down, or stay out until the dust settles."],
].map(([id, title, body]) => ({ id, title, body, lesson: true }));

const EVENTS = {
  intro: () => ({ id: "intro", title: "Hi, I'm Kai, your live trading teacher", body: "I read this chart on every tick: trend, support and resistance, 30+ candlestick and chart patterns, momentum and volume. My running read sits in the bar under the chart; click it any time for the full plan. When something happens, I'll pop up with what it means and what to do next. Click OK to close a tip." }),
  index: () => ({ id: "index", once: true, title: "You're watching an index", body: "An index like NIFTY 50 is a basket of large companies, a summary of the whole market. You can't buy it directly: trade a stock, or options on the index from the option chain. Its direction is still the best context for any stock trade." }),
  "1m": () => ({ id: "1m", once: true, title: "1-minute candles are noisy", body: "On 1-minute candles, random wiggles look like signals and most patterns fail. While learning, 5m or 15m candles give a calmer picture. Kai's read is more reliable there too." }),
};

// ------------------------------------------------------------------ rendering
function ensureEl() {
  if (K.el) return;
  K.el = document.createElement("div");
  K.el.className = "kai-tip"; K.el.hidden = true;
  K.el.setAttribute("role", "status"); K.el.setAttribute("aria-live", "polite");
  K.dot = document.createElement("div"); K.dot.className = "kai-dot"; K.dot.hidden = true;
  K.el.addEventListener("click", (e) => {
    if (e.target.closest(".kai-ok")) close();
    else if (e.target.closest(".kai-plan")) { close(); showPlan(true); }
    else if (e.target.closest(".kai-trade")) { const t = K.open; close(); tradePlan(t); }
    else if (e.target.closest(".kai-show")) { K.api.showIndicator(e.target.closest(".kai-show").dataset.k); close(); }
  });
}
function host(float) {
  const main = document.querySelector("#view-terminal .term-main");
  const parent = float || !main ? document.body : main;
  if (K.el.parentElement !== parent) parent.appendChild(K.el);
  if (main && K.dot.parentElement !== main) main.appendChild(K.dot);
}
const chip = (label, dir) => `<span class="kai-chip ${toneOf(dir)}">${esc(label)}</span>`;
const IND_NAMES = { vwap: "VWAP", vol: "volume", rsi: "RSI", macd: "MACD", ema20: "EMA 20", sma50: "SMA 50", bb: "Bollinger Bands" };

function show(tip) {
  ensureEl();
  K.open = tip;
  const c = K.api.ctx();
  const showBtn = tip.show && c && !c.ind?.[tip.show] ? `<button class="btn sm ghost kai-show" data-k="${tip.show}">Show ${IND_NAMES[tip.show]}</button>` : "";
  const planBtn = tip.planLink && K.read ? `<button class="btn sm ghost kai-plan">Full plan</button>` : "";
  const tradeBtn = tip.trade ? `<button class="btn sm kai-trade" title="Opens a super order with this plan's entry, stop-loss, target and size. You review it before placing.">Trade this plan</button>` : "";
  K.el.className = `kai-tip ${tip.wide ? "wide" : ""} ${toneOf(tip.dir)} ${tip.float ? "float" : ""}`;
  K.el.innerHTML = `<div class="kai-head">${AVATAR}<div><b>Kai</b><small class="muted">${esc(tip.sub || "Live teacher")}</small></div></div>
    <div class="kai-title">${tip.chip ? chip(tip.chip, tip.dir) : ""}${esc(tip.title)}</div>
    <div class="kai-body">${tip.html ?? `<p>${esc(tip.body)}</p>`}</div>
    <div class="kai-act">${showBtn}${planBtn}${tradeBtn}<button class="btn sm primary kai-ok">OK</button></div>
    <div class="kai-fine">${tip.lesson ? "Tip" : "A lesson and a scenario, not investment advice. Free data can be delayed."}</div>`;
  host(tip.float);
  K.el.hidden = false;
  window.dispatchEvent(new CustomEvent("kai:tip", { detail: { id: tip.id, lesson: !!tip.lesson } }));
  K.el.scrollTop = 0;
  place();
}
function close() {
  if (!K.open) return;
  K.open = null; K.lastClosed = Date.now();
  if (K.el) K.el.hidden = true;
  if (K.dot) K.dot.hidden = true;
  setTimeout(() => consider(true), 4_200);  // pick up anything that was held back while the tip was open
}

/** Pin the tip next to its candle; dock it in the corner when the candle is off screen, for wide cards, or on phones. */
export function kaiPlace() { place(); }
function place() {
  const t = K.open, el = K.el;
  if (!t || !el || el.hidden) return;
  const main = el.parentElement, p = !t.float && !t.wide && t.at ? K.api.point(t.at.time, t.at.price) : null;
  const W = main.clientWidth, H = main.clientHeight, narrow = window.matchMedia("(max-width: 860px)").matches;
  if (t.float || !p || narrow || p.x < 0 || p.x > W || p.y < 0 || p.y > H) {
    el.classList.toggle("dock", !t.float); el.style.left = el.style.top = ""; K.dot.hidden = true; return;
  }
  el.classList.remove("dock");
  K.dot.hidden = false; K.dot.className = `kai-dot ${toneOf(t.dir)}`;
  K.dot.style.left = `${p.x}px`; K.dot.style.top = `${p.y}px`;
  const w = el.offsetWidth, h = el.offsetHeight, gap = 16;
  let x = p.x - w - gap;
  if (x < 8) x = Math.min(p.x + gap, W - w - 8);
  const y = Math.max(36, Math.min(p.y - h / 2, H - h - 44));
  el.style.left = `${Math.max(8, x)}px`; el.style.top = `${y}px`;
}

// ------------------------------------------------------------------ the strip, levels on the chart, the plan card
function renderStrip() {
  const s = document.getElementById("kaiStrip");
  if (!s) return;
  const r = K.read;
  s.hidden = !K.on;
  if (!K.on) return;
  if (!r) { s.innerHTML = `<b>Kai</b> <span class="muted">reading the chart…</span>`; return; }
  const S1 = r.levels.supports[0], R1 = r.levels.resistances[0];
  s.className = `kai-strip ${toneOf(r.bias.dir)}`;
  s.innerHTML = `<b>Kai</b><span class="kai-strip-bias">${esc(r.bias.label)}</span>
    ${S1 ? `<span class="muted">S ${f(S1.price)}</span>` : ""}${R1 ? `<span class="muted">R ${f(R1.price)}</span>` : ""}
    <u>${r.open ? "What now?" : "Next session plan"}</u>`;
  s.title = r.plan.headline;
}
function drawLevels(r) {
  const L = [];
  r.levels.supports.slice(0, 2).forEach((l, i) => L.push({ price: l.price, kind: "support", label: `S${i + 1}` }));
  r.levels.resistances.slice(0, 2).forEach((l, i) => L.push({ price: l.price, kind: "resistance", label: `R${i + 1}` }));
  const p = r.plan;
  if (p.side && p.entry != null) {
    if (Math.abs(p.entry - r.price) > r.atr * 0.2) L.push({ price: p.entry, kind: "entry", label: "entry zone" });
    L.push({ price: p.stop, kind: "stop", label: "stop" }, { price: p.target, kind: "target", label: "target" });
  }
  K.api.setLevels(L);
}

function oddsHtml(fc) {
  if (!fc) return "";
  const m = fc.backtest?.models?.find((x) => x.model === fc.model_used) ?? fc.backtest?.models?.[0];
  const hr = m?.hit_rate != null ? Math.round(m.hit_rate * 100) : null, base = fc.backtest?.baseline_hit_rate != null ? Math.round(fc.backtest.baseline_hit_rate * 100) : null;
  const dir = fc.direction === "up" ? "leans up" : fc.direction === "down" ? "leans down" : "shows no clear edge";
  return `<div class="kai-sec">Odds check (5 days, backtested)</div>
    <p>Kai's statistical model ${dir} (${Math.round(fc.probability_up * 100)}% chance of closing higher in 5 sessions).
    ${hr != null ? `In its walk-forward backtest it was right ${hr}% of the time${base != null ? `, against ${base}% for a naive guess` : ""}. ` : ""}
    Confidence: <b>${esc(fc.confidence)}</b>. ${fc.confidence === "low" ? "Treat the plan as a scenario to manage, not a prediction." : ""}</p>`;
}

function planHtml(r, fc) {
  const p = r.plan, see = [];
  see.push(`Structure: ${r.trend.label}.`);
  if (r.daily) see.push(`Daily chart: ${r.daily.label}${r.daily.fromHigh != null ? `, ${Math.abs(r.daily.fromHigh).toFixed(1)}% below its 52-week high` : ""}.`);
  const mom = [];
  if (r.rsi != null) mom.push(`RSI ${r.rsi.toFixed(0)}`);
  if (r.macdHist != null) mom.push(`MACD ${r.macdHist > 0 ? "positive" : "negative"}`);
  if (r.vwap != null) mom.push(`${r.price > r.vwap ? "above" : "below"} VWAP ${f(r.vwap)}`);
  if (r.volX != null) mom.push(`volume ${r.volX.toFixed(1)}× normal`);
  if (mom.length) see.push(`Momentum: ${mom.join(", ")}.`);
  const pats = r.events.filter((e) => e.kind !== "indicator").slice(0, 3);
  if (pats.length) see.push(`Now on the chart: ${pats.map((e) => e.name).join(", ")}.`);
  const why = r.bias.reasons.slice(0, 4).map((x) => x.text).join(", ");
  const lvl = (l, k) => `<li><span class="kai-lv ${k}">${k === "support" ? "S" : "R"}</span> <b class="num">${f(l.price)}</b> <span class="muted">${esc(l.label)}</span></li>`;
  const levels = [...r.levels.resistances.slice(0, 2).reverse().map((l) => lvl(l, "resistance")),
    `<li class="kai-now">Price now <b class="num">${f(r.price)}</b></li>`, ...r.levels.supports.slice(0, 2).map((l) => lvl(l, "support"))].join("");
  const nums = p.side ? `<div class="kai-nums">
      <div><small>${p.side === "long" ? "Buy zone" : "Sell zone"}</small><b class="num">${f(p.entry)}</b></div>
      <div><small>Stop</small><b class="num down">${f(p.stop)}</b></div>
      <div><small>Target</small><b class="num up">${f(p.target)}</b></div>
      <div><small>Reward : risk</small><b class="num">${p.rr != null ? p.rr.toFixed(1) : "—"} : 1</b></div></div>` : "";
  return `<div class="kai-sec">What I see</div><ul>${see.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
    <p class="muted">Why ${esc(r.bias.label.toLowerCase())}: ${esc(why || "signals are mixed")}.</p>
    <div class="kai-sec">Key levels</div><ul class="kai-levels">${levels}</ul>
    <div class="kai-sec">What to do next</div><p><b>${esc(p.headline)}</b></p>${nums}
    <ul>${p.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>${oddsHtml(fc)}`;
}

function recapHtml(r, fc) {
  const x = r.recap;
  return `<div class="kai-sec">How the last session went</div><ul>${x.lines.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
    <div class="kai-sec">Scenarios for the next session</div><ul>${x.scenarios.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
    ${planHtml(r, fc)}`;
}

/** The full plan card (or, with the market closed, the session recap + next-session plan). */
function showPlan(force = false) {
  const c = K.api.ctx(), r = K.read;
  if (!c || !r) return;
  if (K.open && !force) return;
  const fc = isOpt(c.sym) || c.replay ? null : forecast(c.sym);
  drawLevels(r);
  const closed = !r.open && r.recap;
  const p = r.plan, canTrade = p.side && p.entry != null && tradable(c.sym) && !c.replay;  // after the close, orders queue for the next session
  show({ id: "plan", wide: true, dir: r.bias.dir, trade: canTrade ? { sym: c.sym, label: c.label, plan: p, atr: r.atr, price: r.price, intraday: c.interval !== "1D", open: r.open } : null, chip: r.bias.label, sub: `Live teacher · ${c.label} · ${c.interval}`,
    title: closed ? "Market's closed. Here's my read for next session" : c.replay ? "My read at this point in the replay" : "My read right now",
    html: closed ? recapHtml(r, fc) : planHtml(r, fc) });
  K.planned.add(`${c.sym}|${r.open}|${!!c.replay}`);
}

/** Open the order ticket as a super order pre-filled from the plan; the user reviews it before placing. */
function tradePlan(tip) {
  const t = tip?.trade; if (!t) return;
  const p = t.plan, near = Math.abs(p.entry - t.price) <= 0.2 * t.atr;
  openTicket({ symbol: t.sym, side: p.side === "long" ? "BUY" : "SELL", ltp: t.price, label: t.label,
    preset: { mode: "super", type: near ? "MARKET" : "LIMIT", price: near ? null : p.entry, target: p.target, stop: p.stop, qty: p.qty || 1,
      product: p.side === "short" || (t.intraday && t.open) ? "INTRADAY" : "DELIVERY" } });
}

// ------------------------------------------------------------------ deciding what to say
function fresh(tip, sym, now = Date.now()) {
  if (tip.once && K.seen.has(tip.id)) return false;
  if (K.fired.has(`${tip.id}|${sym}|${tip.key ?? ""}`)) return false;
  const last = K.fired.get(`${tip.id}|${sym}`);
  return !(last && Math.abs(now - last) < RULE_COOLDOWN_MS);
}
function mark(tip, sym, now = Date.now()) {
  K.fired.set(`${tip.id}|${sym}|${tip.key ?? ""}`, now); K.fired.set(`${tip.id}|${sym}`, now);
  if (tip.once || tip.lesson) { K.seen.add(tip.id); store.set("kairo-kai-seen", [...K.seen]); }
}
function eventTip(e, r) {
  return { id: e.id, key: e.time, at: { time: e.time, price: e.price }, dir: e.dir, chip: e.dir === "up" ? "Bullish" : e.dir === "down" ? "Bearish" : "Heads-up",
    title: e.name, planLink: true, show: { vwap: "vwap", "vwap-up": "vwap", "vwap-down": "vwap", rsi70: "rsi", rsi30: "rsi", "macd-up": "macd", "macd-down": "macd",
      "ema-up": "ema20", "ema-down": "ema20", "pullback-up": "ema20", "pullback-down": "ema20", squeeze: "bb", volume: "vol", "bear-div": "rsi", "bull-div": "rsi" }[e.id],
    html: `<p>${esc(e.what)}</p><p><b>What to do next:</b> ${esc(e.next)}</p>
      <p class="muted">Overall read: ${esc(r.bias.label)}. ${esc(r.plan.headline)}</p>` };
}

function consider(force = false) {
  if (!K.on || !K.api) return;
  const c = K.api.ctx();
  if (!c || !c.bars.length) return;
  const now = Date.now();
  if (!force && now - K.lastCheck < CHECK_MS) return;
  K.lastCheck = now;
  if (c.sym !== K.sym) { K.sym = c.sym; K.symAt = now; K.read = null; K.api.setLevels([]); if (K.open && !K.open.float && K.open.id !== "intro") close(); }
  const dailyAll = !isOpt(c.sym) && c.interval !== "1D" ? dailyBars(c.sym) : null, fc = isOpt(c.sym) || c.replay ? null : forecast(c.sym);
  const daily = c.replay && dailyAll ? dailyAll.filter((b) => b.time < c.now - 86400) : dailyAll;
  K.read = analyse(c.bars, { interval: c.interval, open: c.open, indian: indian(c.sym), daily, capital: capital(), fmt: f, now: c.now });
  renderStrip();
  const r = K.read;
  if (!r || K.open) return;

  // first look at a chart (or the market just closed): the full read
  // (waits briefly for the daily context and odds check, so the first card is complete)
  const ready = (daily !== undefined && fc !== undefined) || now - K.symAt > 6_000;
  if (!K.planned.has(`${c.sym}|${r.open}|${!!c.replay}`) && now - K.lastClosed > 4_000) { if (ready) showPlan(); else setTimeout(() => consider(true), 6_100); return; }
  const clock = c.replay ? c.now * 1000 : now;
  if (!c.replay && now - K.lastClosed < GAP_MS) return;

  // exchange surveillance / F&O ban / thin liquidity: say it once per stock, before anything else
  const flags = /\.(NS|BO)$/.test(c.sym) ? indiaFlags(c.sym) : null;
  if (flags?.length && fresh({ id: "flags", once: false }, c.sym, Number.MAX_SAFE_INTEGER)) {
    const tip = { id: "flags", key: "", dir: "down", chip: "Risk", title: "Check this before you trade",
      html: flags.map((x) => `<p>${esc(x.text)}</p>`).join("") + "<p class=\"muted\">These come from NSE's surveillance lists and trading data. Many beginners lose money in stocks under ASM/GSM: moves are erratic and exits can be hard.</p>" };
    mark(tip, c.sym, Number.MAX_SAFE_INTEGER); show(tip); return;
  }

  // the overall read changed on a closed candle
  const prev = K.bias.get(c.sym);
  K.bias.set(c.sym, { label: r.bias.label, time: r.time });
  if (prev && prev.label !== r.bias.label && prev.time !== r.time) {
    const tip = { id: "bias", key: r.time, dir: r.bias.dir, chip: r.bias.label, title: `My read changed: ${prev.label} → ${r.bias.label}`, planLink: true,
      at: { time: r.time, price: r.price },
      html: `<p>Why: ${esc(r.bias.reasons.slice(0, 3).map((x) => x.text).join(", "))}.</p><p><b>What to do next:</b> ${esc(r.plan.headline)}</p>` };
    if (fresh(tip, c.sym, clock)) { mark(tip, c.sym, clock); drawLevels(r); show(tip); return; }
  }

  // a pattern or event on the newest candle
  const idle = !c.replay && now - K.lastClosed >= LESSON_GAP_MS;
  const ev = r.events.find((e) => (e.strength >= 2 || idle) && fresh({ id: e.id, key: e.time }, c.sym, clock));
  if (ev) { const tip = eventTip(ev, r); mark(tip, c.sym, clock); show(tip); return; }

  if (!idle) return;
  const lesson = LESSONS.find((l) => !K.seen.has(l.id));
  if (lesson) { mark(lesson, c.sym); show(lesson); }
}

// ------------------------------------------------------------------ public
/** `api`: { ctx() -> {sym, label, interval, bars, open, tradable, ind} | null, point(time, price) -> {x, y} | null,
 *  showIndicator(key), setLevels([{price, kind, label}]) }. */
export function initKai(hooks) {
  K.api = hooks;
  setInterval(() => consider(), 15_000);  // time-of-day guidance and lessons, even on a quiet chart
  window.addEventListener("resize", place);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && K.open && !document.querySelector(".scrim")) close(); });
  document.getElementById("kaiStrip")?.addEventListener("click", () => { consider(true); showPlan(true); });
  renderStrip();
}
export const kaiOn = () => K.on;
export function setKai(on) {
  K.on = on; store.set("kairo-kai", on);
  renderStrip();
  if (!on) { close(); K.api?.setLevels([]); K.planned.clear(); return; }
  K.lastClosed = 0; K.planned.clear();
  show(EVENTS.intro());
  consider(true);
}
/** Live tick or new bars: Kai reads the chart (throttled). */
export function kaiTick() { consider(); }
/** A user action worth coaching: "ticket" (with side), "index", "1m". */
export function kaiEvent(name, arg) {
  if (!K.on) return;
  const sym = K.api?.ctx()?.sym || "";
  if (name === "ticket") { if (K.open) close(); show(ticketTip(arg)); return; }  // the order ticket can't wait
  const tip = EVENTS[name]?.();
  if (!tip || K.open || !fresh(tip, sym)) return;
  mark(tip, sym); show(tip);
}
function ticketTip(side) {
  const r = K.read, p = r?.plan, buy = side === "BUY";
  const agree = !p?.side ? "My read has no clear edge here. Make sure you have a reason beyond \"it might move\"."
    : (p.side === "long") === buy ? `This ${buy ? "buy" : "sell"} goes with my current read (${r.bias.label.toLowerCase()}).`
      : `Careful: this ${buy ? "buy" : "sell"} goes against my current read (${r.bias.label.toLowerCase()}). Counter-trend trades need tighter stops and smaller size.`;
  const nums = p?.side && (p.side === "long") === buy
    ? `<p>My plan's levels: stop <b class="num">${f(p.stop)}</b>, target <b class="num">${f(p.target)}</b>${p.rr != null ? ` (${p.rr.toFixed(1)} : 1)` : ""}. Choose <b>Super</b> in the ticket to place the entry, target and stop together.</p>` : "";
  const size = p?.steps.find((s) => s.startsWith("Position size"));
  return { id: "ticket", float: true, title: `Before you ${buy ? "buy" : "sell"}`, dir: buy ? "up" : "down",
    html: `<p>${esc(agree)}</p>${nums}${size ? `<p>${esc(size)}</p>` : `<p>Keep the loss if your stop is hit to about 1% of your capital.</p>`}
      ${!buy && r && indian(K.sym) ? "<p class=\"muted\">Selling shares you don't hold is an intraday short: it must be closed the same day.</p>" : ""}` };
}
/** The Terminal was hidden: put away tips pinned to its chart. */
export function kaiHide() { if (K.open && !K.open.float) close(); }
