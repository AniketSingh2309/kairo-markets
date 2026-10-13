// Bar Replay: rewind the Terminal chart to any candle and play it forward bar by bar, practising trades
// on the way. Kai treats the replay as a live market (and can pause it when he spots something), so it's
// a way to learn on real history any time, including when the market is closed. No future bar is shown
// before its time: the chart, the indicators and Kai all see only the bars up to the replay cursor.
import { $, fmt, pctTxt, tone, toast, store } from "./core.js";

const SPEEDS = [1, 2, 5, 10, 25];
const R = { h: null, on: false, picking: false, all: [], i: 0, playing: false, timer: 0, speed: store.get("kairo-replay-speed", 2),
  pauseOnKai: store.get("kairo-replay-kai-pause", true), pos: null, trades: [] };

/** `hooks`: { bars() (full series), setBars(bars, step), interval(), label(), exitTo(bars), hint(text) }. */
export function initReplay(hooks) {
  R.h = hooks;
  window.addEventListener("kai:tip", (e) => { if (R.on && R.playing && R.pauseOnKai && !e.detail?.lesson) { pause(); flash("Paused: Kai has a tip for you"); } });
  document.addEventListener("keydown", (e) => {
    if (!R.on || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName) || document.querySelector(".scrim")) return;
    if (e.key === " ") { e.preventDefault(); R.playing ? pause() : play(); }
    else if (e.key === "ArrowRight" && !e.ctrlKey) { e.preventDefault(); step(1); }
    else if (e.key === "ArrowLeft" && !e.ctrlKey) { e.preventDefault(); step(-1); }
  });
}
export const replaying = () => R.on;
export const picking = () => R.picking;
/** Time of the replay cursor's candle (for Kai's clock), or null when not replaying. */
export const replayTime = () => (R.on ? R.all[R.i]?.time ?? null : null);

/** Enter pick mode: the next click on the chart chooses where the replay starts. */
export function startPick() {
  if (R.on) { exit(); return; }
  const bars = R.h.bars();
  if (bars.length < 80) { toast("Not enough candles to replay", "Pick a longer range or interval.", "err"); return; }
  R.picking = true;
  R.h.hint("Click a candle to start the replay from there · Esc to cancel");
  render();
}
/** Leave replay without reloading (the caller loads fresh bars), e.g. on a symbol or interval change. */
export function stopReplay() {
  if (!R.on && !R.picking) return;
  pause(); R.on = false; R.picking = false; R.pos = null; R.h.hint(""); render();
}
export function cancelPick() { if (R.picking) { R.picking = false; R.h.hint(""); render(); } }

/** A click on the chart while picking: start from the candle at `time` (at least 60 candles in, for the indicators). */
export function pickAt(time) {
  if (!R.picking) return false;
  R.picking = false; R.h.hint("");
  R.all = R.h.bars().slice();
  let i = R.all.findIndex((b) => b.time >= time);
  if (i < 0) i = R.all.length - 1;
  R.i = Math.max(60, Math.min(i, R.all.length - 2));
  R.on = true; R.pos = null; R.trades = [];
  R.h.setBars(R.all.slice(0, R.i + 1), false);
  render();
  flash("Replay started. Press ▶ or Space to play, → to step.");
  return true;
}

function play() {
  if (!R.on || R.i >= R.all.length - 1) return;
  R.playing = true; render();
  clearInterval(R.timer);
  R.timer = setInterval(() => { if (!step(1)) pause(); }, 1000 / R.speed);
}
function pause() { R.playing = false; clearInterval(R.timer); render(); }
function step(n) {
  const j = Math.max(60, Math.min(R.all.length - 1, R.i + n));
  if (j === R.i) return false;
  R.i = j;
  R.h.setBars(R.all.slice(0, R.i + 1), n === 1);
  render();
  if (R.i >= R.all.length - 1) { pause(); flash("You've reached the latest candle. Exit to go back to the live chart."); }
  return true;
}

// ------------------------------------------------------------------ practice trades (local, per replay)
const price = () => R.all[R.i]?.close;
function trade(side) {
  const px = price(); if (px == null) return;
  if (R.pos && R.pos.side !== side) { closePos(); return; }
  if (R.pos) return;
  R.pos = { side, entry: px, at: R.all[R.i].time };
  render();
}
function closePos() {
  if (!R.pos) return;
  const px = price(), pts = (px - R.pos.entry) * (R.pos.side === "BUY" ? 1 : -1);
  R.trades.push({ ...R.pos, exit: px, pts, pct: (pts / R.pos.entry) * 100 });
  R.pos = null; render();
}
function summary() {
  const n = R.trades.length, wins = R.trades.filter((t) => t.pts > 0).length, total = R.trades.reduce((a, t) => a + t.pct, 0);
  return { n, wins, total };
}

function exit() {
  pause();
  if (R.pos) closePos();
  const s = summary();
  R.on = false; R.picking = false;
  R.h.exitTo();
  render();
  if (s.n) toast("Replay finished", `${s.n} practice trade${s.n > 1 ? "s" : ""} · ${s.wins} won · total ${pctTxt(s.total)}`, s.total >= 0 ? "ok" : "");
}

// ------------------------------------------------------------------ controls
function bar() {
  let el = $("#termReplay");
  if (!el) {
    el = document.createElement("div"); el.id = "termReplay"; el.className = "term-replay";
    el.innerHTML = `<div class="rp-controls"></div><div class="rp-flash" hidden></div>`;
    document.querySelector("#view-terminal .term-main")?.appendChild(el);
    el.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      const k = b.dataset.k;
      if (k === "play") R.playing ? pause() : play();
      if (k === "back") step(-1);
      if (k === "fwd") step(1);
      if (k === "speed") { R.speed = SPEEDS[(SPEEDS.indexOf(R.speed) + 1) % SPEEDS.length]; store.set("kairo-replay-speed", R.speed); if (R.playing) play(); else render(); }
      if (k === "kai") { R.pauseOnKai = !R.pauseOnKai; store.set("kairo-replay-kai-pause", R.pauseOnKai); render(); }
      if (k === "buy") trade("BUY");
      if (k === "sell") trade("SELL");
      if (k === "close") closePos();
      if (k === "exit") exit();
    });
  }
  return el;
}
function flash(text) {
  const el = bar(), f = el.querySelector(".rp-flash");
  if (f) { f.textContent = text; f.hidden = false; clearTimeout(R.flashT); R.flashT = setTimeout(() => { f.hidden = true; }, 3500); }
}
function render() {
  const el = bar();
  el.hidden = !R.on;
  document.getElementById("termReplayBtn")?.classList.toggle("on", R.on || R.picking);
  if (!R.on) return;
  const b = R.all[R.i], when = new Date(b.time * 1000), px = b.close;
  const open = R.pos ? (px - R.pos.entry) * (R.pos.side === "BUY" ? 1 : -1) : null, s = summary();
  el.querySelector(".rp-controls").innerHTML = `<span class="rp-tag">REPLAY</span>
    <span class="rp-when num">${when.toLocaleDateString([], { day: "numeric", month: "short", year: "2-digit" })}${R.h.interval() !== "1D" ? " " + when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}</span>
    <button data-k="back" title="Back one candle (←)">⏮</button>
    <button data-k="play" class="rp-play" title="Play / pause (Space)">${R.playing ? "⏸" : "▶"}</button>
    <button data-k="fwd" title="Forward one candle (→)">⏭</button>
    <button data-k="speed" title="Playback speed">${R.speed}×</button>
    <button data-k="kai" class="${R.pauseOnKai ? "on" : ""}" title="Pause the replay when Kai has a tip">Kai pauses</button>
    <span class="rp-sep"></span>
    ${R.pos ? `<span class="rp-pos ${tone(open)}">${R.pos.side === "BUY" ? "Long" : "Short"} @ ${fmt(R.pos.entry)} · ${open >= 0 ? "+" : "−"}${fmt(Math.abs(open))} (${pctTxt((open / R.pos.entry) * 100)})</span>
       <button data-k="close" class="rp-close">Close</button>`
      : `<button data-k="buy" class="rp-buy">Buy</button><button data-k="sell" class="rp-sell">Sell</button>`}
    ${s.n ? `<span class="rp-score">${s.n} trade${s.n > 1 ? "s" : ""} · ${s.wins} won · <b class="${tone(s.total)}">${pctTxt(s.total)}</b></span>` : ""}
    <button data-k="exit" class="rp-exit" title="Back to the live chart">Exit</button>`;
}
