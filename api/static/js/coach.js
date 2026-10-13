// Kai's brain: reads a chart the way a trading coach would and turns it into plain-language guidance.
// Pure functions (no DOM): candles in, a "read" out: trend structure, key levels, candlestick and chart
// patterns, a bias score with its reasons, and a plan for what to do next (trigger, stop, target, size).
// Everything comes from the bars given. A plan is a scenario with an exit, never a prediction.
import { sma, ema, rsi, macd, vwap, atr, pivots, bollinger, dayKey } from "./indicators.js";

const avg = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);
/** Minutes after midnight IST for an epoch-seconds time. */
export const istMin = (t) => { const d = new Date((t + 330 * 60) * 1000); return d.getUTCHours() * 60 + d.getUTCMinutes(); };
const plainFmt = (v) => (v == null || !isFinite(v) ? "—" : v.toFixed(2));

function shape(b) {
  const body = Math.abs(b.close - b.open), range = b.high - b.low;
  return { body, range, up: b.close > b.open, down: b.close < b.open, upper: b.high - Math.max(b.open, b.close),
    lower: Math.min(b.open, b.close) - b.low, mid: (b.open + b.close) / 2 };
}
/** Direction of the move into bar j: compares its close with 5 bars earlier, in ATRs. */
function trendInto(bars, j, A) {
  const a = bars[j], b = bars[j - 5];
  if (!a || !b) return "flat";
  return a.close < b.close - A ? "down" : a.close > b.close + A ? "up" : "flat";
}

// ------------------------------------------------------------------ candlestick patterns (ending at bar k)
export function candlePatterns(bars, k, A, f = plainFmt) {
  const out = [], c0 = bars[k], c1 = bars[k - 1], c2 = bars[k - 2];
  if (!c2 || !A) return out;
  const s0 = shape(c0), s1 = shape(c1), s2 = shape(c2), before = trendInto(bars, k - 1, A), before2 = trendInto(bars, k - 2, A);
  const add = (id, name, dir, strength, what, next) =>
    out.push({ id, name, dir, strength, what, next, kind: "candle", time: c0.time, price: dir === "down" ? c0.high : dir === "up" ? c0.low : c0.close });

  // three-candle patterns
  if (s2.up && s1.up && s0.up && c1.close > c2.close && c0.close > c1.close && c1.open >= c2.open && c1.open <= c2.close
      && c0.open >= c1.open && c0.open <= c1.close && Math.min(s0.body, s1.body, s2.body) >= 0.5 * A)
    add("soldiers", "Three white soldiers", "up", 3, "Three strong green candles, each closing higher: steady, determined buying.",
      `Momentum is up, but chasing after three big candles is risky. A pullback that holds above ${f(c1.low)} is the calmer entry.`);
  if (s2.down && s1.down && s0.down && c1.close < c2.close && c0.close < c1.close && c1.open <= c2.open && c1.open >= c2.close
      && c0.open <= c1.open && c0.open >= c1.close && Math.min(s0.body, s1.body, s2.body) >= 0.5 * A)
    add("crows", "Three black crows", "down", 3, "Three strong red candles, each closing lower: steady, determined selling.",
      `Don't try to catch this falling knife. If you hold it, a close below ${f(c0.low)} keeps the pressure on; buyers need to reclaim ${f(c1.high)} first.`);
  if (s2.down && s2.body >= 0.8 * A && s1.body <= 0.35 * s2.body && s0.up && c0.close > s2.mid && before2 === "down")
    add("morningstar", "Morning star", "up", 3, "A big red candle, a small pause candle, then a strong green one: selling ran out and buyers took over.",
      `Confirmation: the next candle holds above ${f(s2.mid)}. A stop usually goes under the pattern's low, ${f(Math.min(c0.low, c1.low, c2.low))}.`);
  if (s2.up && s2.body >= 0.8 * A && s1.body <= 0.35 * s2.body && s0.down && c0.close < s2.mid && before2 === "up")
    add("eveningstar", "Evening star", "down", 3, "A big green candle, a small pause candle, then a strong red one: buying ran out and sellers took over.",
      `Warning sign for longs. If you hold, consider tightening your stop; the pattern fails if price closes back above ${f(Math.max(c0.high, c1.high, c2.high))}.`);

  // two-candle patterns
  if (s1.down && s0.up && c0.open <= c1.close && c0.close >= c1.open && s0.body > s1.body && s0.body >= 0.4 * A)
    add("engulf-up", "Bullish engulfing", "up", before === "down" ? 3 : 2, "A green body that completely covers the previous red one: buyers overwhelmed sellers.",
      `Strongest after a fall and at support. Buyers' line in the sand is this candle's low, ${f(c0.low)}: a close below it cancels the signal.`);
  else if (s1.up && s0.down && c0.open >= c1.close && c0.close <= c1.open && s0.body > s1.body && s0.body >= 0.4 * A)
    add("engulf-down", "Bearish engulfing", "down", before === "up" ? 3 : 2, "A red body that completely covers the previous green one: sellers overwhelmed buyers.",
      `Strongest after a rally and at resistance. The signal is cancelled if price closes above this candle's high, ${f(c0.high)}.`);
  else if (s1.down && s1.body >= 0.6 * A && s0.up && c0.open <= c1.close && c0.close > s1.mid && c0.close < c1.open && before === "down")
    add("piercing", "Piercing line", "up", 2, "After a red candle, a green one closes above its midpoint: buyers pushed back hard.",
      `Needs follow-through: a close above ${f(c1.open)} would confirm. Stop under ${f(c0.low)}.`);
  else if (s1.up && s1.body >= 0.6 * A && s0.down && c0.open >= c1.close && c0.close < s1.mid && c0.close > c1.open && before === "up")
    add("darkcloud", "Dark cloud cover", "down", 2, "After a green candle, a red one closes below its midpoint: sellers pushed back hard.",
      `Confirmation is a close below ${f(c1.open)}. Longs often protect profits here.`);
  else if (s1.down && s1.body >= 0.8 * A && s0.up && Math.max(c0.open, c0.close) <= c1.open && Math.min(c0.open, c0.close) >= c1.close && s0.body <= 0.6 * s1.body && before === "down")
    add("harami-up", "Bullish harami", "up", 1, "A small green candle inside the previous big red one: the selling is slowing down.",
      `A weak signal on its own. Wait for a close above ${f(c1.open)} before treating it as a turn.`);
  else if (s1.up && s1.body >= 0.8 * A && s0.down && Math.max(c0.open, c0.close) <= c1.close && Math.min(c0.open, c0.close) >= c1.open && s0.body <= 0.6 * s1.body && before === "up")
    add("harami-down", "Bearish harami", "down", 1, "A small red candle inside the previous big green one: the buying is slowing down.",
      `A weak signal on its own. A close below ${f(c1.open)} would make it meaningful.`);
  if (s1.down && s0.up && Math.abs(c0.low - c1.low) <= 0.1 * A && before === "down")
    add("tweezer-bottom", "Tweezer bottom", "up", 2, "Two candles with the same low: price was rejected twice at that level.",
      `${f(Math.min(c0.low, c1.low))} is now a defended floor. A break below it means the floor gave way.`);
  if (s1.up && s0.down && Math.abs(c0.high - c1.high) <= 0.1 * A && before === "up")
    add("tweezer-top", "Tweezer top", "down", 2, "Two candles with the same high: price was rejected twice at that level.",
      `${f(Math.max(c0.high, c1.high))} is now a ceiling. A close above it would cancel the signal.`);
  if (c0.high < c1.high && c0.low > c1.low && s1.range >= 0.8 * A)
    add("insidebar", "Inside bar", "flat", 1, "This candle sits entirely inside the previous one: the market is pausing and coiling.",
      `Breakout traders watch the mother candle: above ${f(c1.high)} is the buy trigger, below ${f(c1.low)} the sell trigger.`);

  // single-candle shapes
  if (s0.range >= 0.5 * A && s0.body <= 0.1 * s0.range) {
    if (s0.lower >= 0.6 * s0.range) add("dragonfly", "Dragonfly doji", before === "down" ? "up" : "flat", before === "down" ? 2 : 1,
      "Open and close at the top with a long lower wick: sellers pushed down, buyers pushed all the way back.", `Bullish if the next candle closes above ${f(c0.high)}.`);
    else if (s0.upper >= 0.6 * s0.range) add("gravestone", "Gravestone doji", before === "up" ? "down" : "flat", before === "up" ? 2 : 1,
      "Open and close at the bottom with a long upper wick: buyers pushed up, sellers pushed all the way back.", `Bearish if the next candle closes below ${f(c0.low)}.`);
    else add("doji", "Doji", "flat", 1, "Open and close almost equal: buyers and sellers are evenly matched. Indecision.",
      `After a strong move, a doji can mean the move is tiring. The next candle decides: above ${f(c0.high)} or below ${f(c0.low)}.`);
  } else if (s0.range >= 0.6 * A && s0.lower >= 2 * Math.max(s0.body, 0.05 * s0.range) && s0.upper <= 0.25 * s0.range) {
    if (before === "down") add("hammer", "Hammer", "up", 2, "Long lower wick, small body near the top, after a fall: sellers pushed down but buyers drove it back.",
      `Confirmation: the next candle closes above ${f(c0.high)}. A stop usually goes under the wick, ${f(c0.low)}.`);
    else if (before === "up") add("hangingman", "Hanging man", "down", 1, "A hammer shape after a rise: buyers are starting to struggle.",
      `Only a warning. It matters if the next candle closes below ${f(c0.low)}.`);
  } else if (s0.range >= 0.6 * A && s0.upper >= 2 * Math.max(s0.body, 0.05 * s0.range) && s0.lower <= 0.25 * s0.range) {
    if (before === "up") add("shootingstar", "Shooting star", "down", 2, "Long upper wick, small body near the bottom, after a rise: buyers pushed up but sellers slammed it back.",
      `Confirmation: the next candle closes below ${f(c0.low)}. Longs often tighten stops here.`);
    else if (before === "down") add("invhammer", "Inverted hammer", "up", 1, "Long upper wick after a fall: buyers are testing higher prices.",
      `Weak until confirmed by a close above ${f(c0.high)}.`);
  } else if (s0.body >= 0.9 * s0.range && s0.range >= 1.3 * A) {
    add(s0.up ? "marubozu-up" : "marubozu-down", s0.up ? "Bullish marubozu" : "Bearish marubozu", s0.up ? "up" : "down", 2,
      `A big ${s0.up ? "green" : "red"} candle with almost no wicks: ${s0.up ? "buyers" : "sellers"} controlled it from open to close.`,
      `Shows conviction. The candle's midpoint, ${f(s0.mid)}, often acts as ${s0.up ? "support" : "resistance"} on a retest.`);
  }
  return out;
}

// ------------------------------------------------------------------ trend structure + key levels
export function structure(piv, A) {
  const H = piv.highs.slice(-2), L = piv.lows.slice(-2), t = 0.1 * A;
  if (H.length < 2 || L.length < 2) return { dir: 0, label: "not enough swings yet to read a trend" };
  const hh = H[1].price > H[0].price + t, lh = H[1].price < H[0].price - t, hl = L[1].price > L[0].price + t, ll = L[1].price < L[0].price - t;
  if (hh && hl) return { dir: 1, label: "higher highs and higher lows: an uptrend" };
  if (lh && ll) return { dir: -1, label: "lower highs and lower lows: a downtrend" };
  if (lh && hl) return { dir: 0, label: "lower highs and higher lows: a narrowing triangle, pressure is building", triangle: true };
  if (hh && ll) return { dir: 0, label: "wider swings both ways: volatile and directionless" };
  return { dir: 0, label: "sideways swings: a range" };
}

function sessions(bars) {
  const days = [];
  for (let i = 0; i < bars.length; i++) {
    const k = dayKey(bars[i].time), d = days[days.length - 1];
    if (d && d.key === k) d.end = i; else days.push({ key: k, start: i, end: i });
  }
  return days.map((d) => {
    const s = bars.slice(d.start, d.end + 1);
    return { ...d, open: s[0].open, high: Math.max(...s.map((b) => b.high)), low: Math.min(...s.map((b) => b.low)), close: s[s.length - 1].close, bars: s };
  });
}

/** Support below and resistance above the price: clustered swing points plus the levels intraday traders watch. */
export function keyLevels(bars, piv, A, price, named) {
  const recent = bars.length - 250, pts = [...piv.highs, ...piv.lows].filter((p) => p.i >= recent).map((p) => p.price).sort((a, b) => a - b);
  const clusters = [];
  for (const p of pts) {
    const c = clusters[clusters.length - 1];
    if (c && p - c.hi <= 0.5 * A) { c.hi = p; c.sum += p; c.n++; } else clusters.push({ hi: p, sum: p, n: 1 });
  }
  const all = clusters.map((c) => ({ price: c.sum / c.n, touches: c.n, label: c.n >= 2 ? `swing level, tested ${c.n}×` : "recent swing" }));
  for (const [label, p] of named) {
    if (p == null || !isFinite(p)) continue;
    const near = all.find((l) => Math.abs(l.price - p) <= 0.3 * A);
    if (near) { near.label = `${label} + ${near.label}`; near.touches += 1; near.named = true; } else all.push({ price: p, touches: 1, label, named: true });
  }
  const gap = 0.15 * A;
  return {
    supports: all.filter((l) => l.price < price - gap).sort((a, b) => b.price - a.price).slice(0, 4),
    resistances: all.filter((l) => l.price > price + gap).sort((a, b) => a.price - b.price).slice(0, 4),
  };
}

// ------------------------------------------------------------------ chart patterns + indicator events (at bar k)
function chartPatterns(c, f) {
  const { bars, k, A, piv, closes, ind, levels } = c, out = [], b = bars[k], p = bars[k - 1];
  const add = (id, name, dir, strength, what, next, price = b.close, kind = "chart") => out.push({ id, name, dir, strength, what, next, kind, time: b.time, price });

  // double top / bottom: two recent swing points at the same price with a valley/peak between
  const tol = Math.max(0.6 * A, b.close * 0.002);
  const [h1, h2] = piv.highs.slice(-2), [l1, l2] = piv.lows.slice(-2);
  if (h1 && h2 && h2.i - h1.i >= 5 && k - h2.i <= 30 && Math.abs(h1.price - h2.price) <= tol) {
    const neck = Math.min(...bars.slice(h1.i, h2.i + 1).map((x) => x.low));
    if (neck < Math.min(h1.price, h2.price) - 1.5 * A) {
      if (b.close < neck && p.close >= neck) add("doubletop-break", "Double top confirmed", "down", 3, `Price failed twice near ${f(h2.price)} and has now closed below the neckline ${f(neck)}.`,
        `Textbook target: about ${f(neck - (h2.price - neck))} (the pattern's height, projected down). The idea fails if price climbs back above ${f(neck)}.`);
      else if (b.close >= neck && k - h2.i <= 12) add("doubletop", "Possible double top", "down", 2, `Two peaks at about ${f(h2.price)}: buyers failed at the same level twice.`,
        `Not confirmed yet. The key level is the neckline at ${f(neck)}: a close below it confirms; a break above ${f(Math.max(h1.price, h2.price))} cancels it.`);
    }
  }
  if (l1 && l2 && l2.i - l1.i >= 5 && k - l2.i <= 30 && Math.abs(l1.price - l2.price) <= tol) {
    const neck = Math.max(...bars.slice(l1.i, l2.i + 1).map((x) => x.high));
    if (neck > Math.max(l1.price, l2.price) + 1.5 * A) {
      if (b.close > neck && p.close <= neck) add("doublebottom-break", "Double bottom confirmed", "up", 3, `Price held twice near ${f(l2.price)} and has now closed above the neckline ${f(neck)}.`,
        `Textbook target: about ${f(neck + (neck - l2.price))}. A retest of ${f(neck)} that holds is a common entry; back below it, the idea fails.`);
      else if (b.close <= neck && k - l2.i <= 12) add("doublebottom", "Possible double bottom", "up", 2, `Two lows at about ${f(l2.price)}: sellers failed at the same level twice.`,
        `Not confirmed yet. Watch the neckline at ${f(neck)}: a close above confirms; a break below ${f(Math.min(l1.price, l2.price))} cancels it.`);
    }
  }

  // range breakout / breakdown with volume
  const win = bars.slice(Math.max(0, k - 30), k), hi = Math.max(...win.map((x) => x.high)), lo = Math.min(...win.map((x) => x.low));
  const volAvg = avg(win.slice(-20).map((x) => x.vol || 0)), volX = volAvg > 0 ? (b.vol || 0) / volAvg : null;
  if (win.length >= 20 && hi - lo <= 5 * A) {
    if (b.close > hi && p.close <= hi) add("breakout", "Range breakout", "up", volX == null || volX >= 1.5 ? 3 : 2,
      `Price closed above the ${win.length}-candle range high ${f(hi)}${volX ? ` on ${volX.toFixed(1)}× normal volume` : ""}.`,
      `${volX != null && volX < 1.5 ? "Volume is light, so treat it with suspicion. " : ""}Breakout traders buy the break or a retest of ${f(hi)}, with a stop back inside the range near ${f(hi - A)}. Target: the range height, about ${f(hi + (hi - lo))}.`);
    if (b.close < lo && p.close >= lo) add("breakdown", "Range breakdown", "down", volX == null || volX >= 1.5 ? 3 : 2,
      `Price closed below the ${win.length}-candle range low ${f(lo)}${volX ? ` on ${volX.toFixed(1)}× normal volume` : ""}.`,
      `Holders often exit on a breakdown like this. A move back above ${f(lo)} would make it a false break, which is itself a bullish sign.`);
  }

  // Bollinger squeeze and band walks
  const bw = ind.bb.up.map((u, i) => (u != null ? (u - ind.bb.lo[i]) / ind.bb.mid[i] : null));
  const recentBw = bw.slice(Math.max(0, k - 120), k + 1).filter((x) => x != null);
  if (recentBw.length >= 60 && bw[k] != null && bw[k] <= Math.min(...recentBw) * 1.02 && bw[k - 1] > bw[k])
    add("squeeze", "Bollinger squeeze", "flat", 2, "The Bollinger Bands are the narrowest in 120 candles: volatility has dried up.",
      `Quiet periods tend to end with a sharp move, but the squeeze doesn't tell you which way. Wait for a close outside the bands: above ${f(ind.bb.up[k])} or below ${f(ind.bb.lo[k])}.`);

  // RSI divergence at the last two swings
  const r = ind.rsi;
  if (h1 && h2 && k - h2.i <= 15 && h2.price > h1.price && r[h1.i] != null && r[h2.i] != null && r[h2.i] < r[h1.i] - 3 && r[h1.i] > 55)
    add("bear-div", "Bearish RSI divergence", "down", 2, `Price made a higher high (${f(h2.price)}) but RSI made a lower one (${r[h2.i].toFixed(0)} vs ${r[h1.i].toFixed(0)}): the rally is losing strength.`,
      "Divergence warns, it doesn't time. Longs often tighten stops; sellers wait for a break of the last swing low.", h2.price);
  if (l1 && l2 && k - l2.i <= 15 && l2.price < l1.price && r[l1.i] != null && r[l2.i] != null && r[l2.i] > r[l1.i] + 3 && r[l1.i] < 45)
    add("bull-div", "Bullish RSI divergence", "up", 2, `Price made a lower low (${f(l2.price)}) but RSI made a higher one (${r[l2.i].toFixed(0)} vs ${r[l1.i].toFixed(0)}): selling is losing strength.`,
      "An early sign, not a buy signal. Buyers usually wait for a break above the last swing high.", l2.price);

  // moving-average events
  const { e20, s50, s200 } = ind;
  if (s50[k] != null && s200[k] != null && s50[k - 1] != null && s200[k - 1] != null) {
    if (s50[k - 1] <= s200[k - 1] && s50[k] > s200[k]) add("golden", "Golden cross", "up", 2, "The 50-period average crossed above the 200: the longer-term trend is turning up.",
      "A slow, widely watched signal. It's better as a filter (favour buys) than as a timing tool.", s50[k]);
    if (s50[k - 1] >= s200[k - 1] && s50[k] < s200[k]) add("death", "Death cross", "down", 2, "The 50-period average crossed below the 200: the longer-term trend is turning down.",
      "A slow signal that often comes after much of the fall. Use it as a filter: be careful with fresh buys.", s50[k]);
  }
  if (e20[k] != null && e20[k - 1] != null) {
    if (p.close <= e20[k - 1] && b.close > e20[k]) add("ema-up", "Closed above the 20 EMA", "up", 1, `The 20 EMA (${f(e20[k])}) is the short-term trend line; price just closed above it.`,
      `Short-term momentum is turning up. In a range this flips often, so check the EMA's slope too.`, e20[k], "indicator");
    if (p.close >= e20[k - 1] && b.close < e20[k]) add("ema-down", "Closed below the 20 EMA", "down", 1, `The 20 EMA (${f(e20[k])}) is the short-term trend line; price just closed below it.`,
      `Short-term momentum is turning down. Longs often wait for a close back above ${f(e20[k])}.`, e20[k], "indicator");
  }
  if (c.trend.dir === 1 && e20[k] != null && b.low <= e20[k] + 0.2 * A && b.close > e20[k] && b.close > b.open)
    add("pullback-up", "Pullback to the 20 EMA in an uptrend", "up", 2, `In an uptrend, price dipped to the 20 EMA (${f(e20[k])}) and bounced.`,
      `This is the classic with-trend entry area. A stop below the dip's low, ${f(b.low)}, keeps the risk small.`, b.low);
  if (c.trend.dir === -1 && e20[k] != null && b.high >= e20[k] - 0.2 * A && b.close < e20[k] && b.close < b.open)
    add("pullback-down", "Rally to the 20 EMA in a downtrend", "down", 2, `In a downtrend, price bounced up to the 20 EMA (${f(e20[k])}) and was pushed back.`,
      `Rallies in downtrends often fail here. Holders can use bounces like this to reduce; buyers should wait for a higher low.`, b.high);
  const stretch = e20[k] != null ? (b.close - e20[k]) / A : 0;
  if (Math.abs(stretch) > 3) add(stretch > 0 ? "stretched-up" : "stretched-down", stretch > 0 ? "Stretched far above average" : "Stretched far below average", "flat", 1,
    `Price is ${Math.abs(stretch).toFixed(1)} candle-ranges ${stretch > 0 ? "above" : "below"} its 20 EMA.`,
    `Moves this stretched tend to pause or snap back. ${stretch > 0 ? "Don't chase; let it come back toward the average" : "Don't short into it; bounces can be sharp"}.`);

  // oscillators
  if (r[k - 1] != null && r[k] != null) {
    if (r[k - 1] < 70 && r[k] >= 70) add("rsi70", "RSI above 70 (overbought)", "flat", 1, "RSI (14) compares recent up moves with down moves on a 0 to 100 scale; above 70 the move is stretched.",
      "Not a sell signal by itself: in strong uptrends RSI can stay above 70 for a long time. It's a reason not to chase.", b.high, "indicator");
    if (r[k - 1] > 30 && r[k] <= 30) add("rsi30", "RSI below 30 (oversold)", "flat", 1, "Below 30, sellers have dominated recently.",
      "Not a buy signal by itself: in a downtrend RSI can stay low. Many traders wait for RSI to climb back above 30.", b.low, "indicator");
  }
  const m = ind.macd;
  if ([m.line[k - 1], m.sig[k - 1], m.line[k], m.sig[k]].every((x) => x != null)) {
    if (m.line[k - 1] <= m.sig[k - 1] && m.line[k] > m.sig[k]) add("macd-up", "MACD bullish cross", "up", 1, "MACD crossed above its signal line: momentum is turning up.",
      "MACD lags and whipsaws in ranges; it works best in the direction of the bigger trend.", b.close, "indicator");
    if (m.line[k - 1] >= m.sig[k - 1] && m.line[k] < m.sig[k]) add("macd-down", "MACD bearish cross", "down", 1, "MACD crossed below its signal line: momentum is turning down.",
      "MACD lags and whipsaws in ranges; it works best in the direction of the bigger trend.", b.close, "indicator");
  }

  // volume and candle size
  if (volX != null && volX >= 3) add("volume", `Volume spike: ${volX.toFixed(1)}× normal`, b.close >= b.open ? "up" : "down", 2,
    `This candle traded ${volX.toFixed(1)}× the average of the last 20. Big participants are active.`,
    `Moves on heavy volume are taken seriously. The candle closed ${b.close >= b.open ? "up" : "down"}; a follow-through candle in the same direction would confirm.`, b.high, "volume");
  const range = avg(bars.slice(Math.max(0, k - 14), k).map((x) => x.high - x.low));
  if (range > 0 && b.high - b.low > 2.5 * range) add("bigbar", "Wide-range candle", b.close >= b.open ? "up" : "down", 1,
    `This candle's range is ${((b.high - b.low) / range).toFixed(1)}× the recent average: a burst of activity, often news or a breakout.`,
    "Beginners chase candles like this; experienced traders wait for the next candle to confirm.", b.high);
  const sign = Math.sign(b.close - p.close);
  let streak = 0;
  for (let j = k; j > 0 && sign !== 0 && Math.sign(bars[j].close - bars[j - 1].close) === sign; j--) streak++;
  if (streak >= 6) add("streak", `${streak} ${b.close > p.close ? "rising" : "falling"} closes in a row`, "flat", 1,
    `That many consecutive ${b.close > p.close ? "higher" : "lower"} closes is unusual: the move is extended.`,
    "Streaks this long usually pause. Better to wait for the first pullback than to join late.");

  // levels: is the candle testing one?
  const s1 = levels.supports[0], r1 = levels.resistances[0];
  for (const o of out) {
    if (o.dir === "up" && s1 && Math.abs(b.low - s1.price) <= 0.6 * A) { o.strength += 1; o.what += ` It formed right at support ${f(s1.price)} (${s1.label}).`; }
    if (o.dir === "down" && r1 && Math.abs(b.high - r1.price) <= 0.6 * A) { o.strength += 1; o.what += ` It formed right at resistance ${f(r1.price)} (${r1.label}).`; }
  }
  return out;
}

// ------------------------------------------------------------------ live intraday events (the forming candle)
function liveEvents(c, f) {
  const { bars, intraday, ses, ind, A } = c, n = bars.length, last = bars[n - 1], prev = bars[n - 2], out = [];
  if (!intraday || !ses.today) return out;
  const add = (id, name, dir, strength, what, next, price = last.close) => out.push({ id, name, dir, strength, what, next, kind: "live", time: last.time, price });
  const today = ses.today.bars.slice(0, -1);
  if (today.length >= 6) {
    const hi = Math.max(...today.map((x) => x.high)), lo = Math.min(...today.map((x) => x.low));
    if (last.close > hi && prev.close <= hi) add("dayhigh", `New high of the day above ${f(hi)}`, "up", 2, "Price is above every earlier high today.",
      `Many breakouts fail and fall back. Wait for a candle to close above ${f(hi)}, ideally on rising volume, before trusting it.`);
    if (last.close < lo && prev.close >= lo) add("daylow", `New low of the day below ${f(lo)}`, "down", 2, "Price is below every earlier low today.",
      `Breakdowns can speed up as stop-losses trigger. Don't buy just because it's "cheaper"; wait for it to stop making new lows.`);
  }
  if (c.or && istMin(last.time) >= 9 * 60 + 30) {
    if (last.close > c.or.high && prev.close <= c.or.high) add("orb-up", "Opening-range breakout (up)", "up", 3, `Price broke above the first 15 minutes' high, ${f(c.or.high)}.`,
      `A popular intraday setup: buy the break, stop below ${f((c.or.high + c.or.low) / 2)} (the range middle). It works best when the index is also rising.`);
    if (last.close < c.or.low && prev.close >= c.or.low) add("orb-down", "Opening-range breakdown", "down", 3, `Price broke below the first 15 minutes' low, ${f(c.or.low)}.`,
      `The mirror setup: intraday shorts sell the break with a stop above ${f((c.or.high + c.or.low) / 2)}. Holders: weakness from the open is a warning.`);
  }
  const v = ind.vwap, a = v[n - 2], z = v[n - 1];
  if (a != null && z != null && dayKey(prev.time) === dayKey(last.time) && today.length >= 3) {
    if (prev.close <= a && last.close > z) add("vwap-up", "Crossed above VWAP", "up", 2, `VWAP (${f(z)}) is today's average traded price. Price just moved above it: buyers are in control for now.`,
      `Intraday buyers like to see price hold above VWAP for a few candles. A drop back below ${f(z)} cancels it.`, z);
    if (prev.close >= a && last.close < z) add("vwap-down", "Crossed below VWAP", "down", 2, `VWAP (${f(z)}) is today's average traded price. Price just moved below it: sellers are in control for now.`,
      `Intraday buyers usually wait for price to reclaim ${f(z)} before buying.`, z);
  }
  return out;
}

// ------------------------------------------------------------------ bias, plan, recap
function biasOf(c) {
  const { k, closes, ind, trend, dctx, intraday, price } = c, reasons = [];
  let s = 0;
  const w = (pts, text) => { s += pts; reasons.push({ pts, text }); };
  if (trend.dir) w(25 * trend.dir, trend.dir > 0 ? "uptrend structure (higher highs and lows)" : "downtrend structure (lower highs and lows)");
  if (ind.e20[k] != null) {
    w(closes[k] > ind.e20[k] ? 10 : -10, closes[k] > ind.e20[k] ? "above the 20 EMA" : "below the 20 EMA");
    const slope = ind.e20[k] - (ind.e20[k - 5] ?? ind.e20[k]);
    if (Math.abs(slope) > 0.2 * c.A) w(slope > 0 ? 8 : -8, slope > 0 ? "20 EMA rising" : "20 EMA falling");
  }
  if (ind.s50[k] != null) w(closes[k] > ind.s50[k] ? 8 : -8, closes[k] > ind.s50[k] ? "above the 50 SMA" : "below the 50 SMA");
  if (ind.s50[k] != null && ind.s200[k] != null) w(ind.s50[k] > ind.s200[k] ? 7 : -7, ind.s50[k] > ind.s200[k] ? "50 SMA above the 200 (long-term up)" : "50 SMA below the 200 (long-term down)");
  const h = ind.macd.hist[k];
  if (h != null) w(h > 0 ? 7 : -7, h > 0 ? "MACD momentum positive" : "MACD momentum negative");
  const r = ind.rsi[k];
  if (r != null && r >= 55) w(8, `RSI ${r.toFixed(0)}: buyers stronger`); else if (r != null && r <= 45) w(-8, `RSI ${r.toFixed(0)}: sellers stronger`);
  if (intraday && ind.vwap[c.n - 1] != null) w(price > ind.vwap[c.n - 1] ? 10 : -10, price > ind.vwap[c.n - 1] ? "trading above VWAP" : "trading below VWAP");
  if (dctx?.dir) w(12 * dctx.dir, dctx.dir > 0 ? "daily chart in an uptrend" : "daily chart in a downtrend");
  for (const p of c.patterns) if (p.strength >= 3 && p.dir !== "flat") w(p.dir === "up" ? 8 : -8, p.name.toLowerCase());
  s = Math.max(-100, Math.min(100, s));
  const label = s >= 45 ? "Bullish" : s >= 15 ? "Leaning bullish" : s <= -45 ? "Bearish" : s <= -15 ? "Leaning bearish" : "No clear edge";
  reasons.sort((a, b) => Math.abs(b.pts) - Math.abs(a.pts));
  return { score: s, label, dir: s >= 15 ? 1 : s <= -15 ? -1 : 0, reasons };
}

function planOf(c, f, capital) {
  const { price, A, levels, bias, ind, k, open, indian, intraday } = c;
  const S = levels.supports, R = levels.resistances, S1 = S[0], R1 = R[0], e20 = ind.e20[k], steps = [];
  const stretch = e20 != null ? (price - e20) / A : 0, rsiNow = ind.rsi[k];
  // a stop inside normal noise gets hit for no reason: at least 0.3% away on intraday charts, 1% on daily
  const minStop = price * (intraday ? 0.003 : 0.01);
  const when = open ? "" : "Next session: ";
  let side = null, entry = null, stop = null, target = null, targetLabel = "", headline, qty = null;

  if (bias.dir > 0) {
    side = "long";
    if (stretch > 2.5 || (rsiNow != null && rsiNow > 75)) {
      entry = Math.max(e20 ?? -Infinity, S1?.price ?? -Infinity); if (!isFinite(entry)) entry = price - A;
      headline = `${when}Buyers are in control, but the move is stretched. Don't chase: wait for a pullback toward ${f(entry)}.`;
    } else if (S1 && price - S1.price <= 0.8 * A) {
      entry = price; headline = `${when}Price is sitting on support ${f(S1.price)} in an up-leaning chart: a with-trend buy zone, as long as it holds.`;
    } else {
      entry = S1?.price ?? e20 ?? price - A;
      headline = `${when}Buyers are in control. Better entries: a dip toward ${f(entry)} that holds${R1 ? `, or a close above ${f(R1.price)}` : ""}. Avoid buying in the middle of a candle spike.`;
    }
    const base = S.find((l) => l.price < entry - 0.2 * A)?.price ?? entry - A;
    stop = Math.min(base, entry - 0.5 * A, entry - minStop) - 0.3 * A;
    const risk = entry - stop, t = R.find((l) => l.price - entry >= 1.5 * risk);
    target = t ? t.price : entry + 2 * risk; targetLabel = t ? t.label : "2× the risk";
    if (R1 && R1.price - entry < 1.5 * risk) steps.push(`Resistance ${f(R1.price)} (${R1.label}) is close overhead, so there's little room before it. Many traders wait for a close above it instead.`);
    steps.push(`The idea is wrong if price closes below ${f(stop)}. Exit then; never move a stop further away.`);
  } else if (bias.dir < 0) {
    side = "short";
    if (stretch < -2.5 || (rsiNow != null && rsiNow < 25)) {
      entry = Math.min(e20 ?? Infinity, R1?.price ?? Infinity); if (!isFinite(entry)) entry = price + A;
      headline = `${when}Sellers are in control, but the fall is stretched: bounces can be sharp. Avoid fresh buys; sellers wait for a bounce toward ${f(entry)}.`;
    } else if (R1 && R1.price - price <= 0.8 * A) {
      entry = price; headline = `${when}Price is right under resistance ${f(R1.price)} in a down-leaning chart. Not a place to buy; sellers like this zone.`;
    } else {
      entry = R1?.price ?? e20 ?? price + A;
      headline = `${when}Sellers are in control. Avoid buying until price reclaims ${f(entry)}. If you hold it, decide your exit level now.`;
    }
    const base = R.find((l) => l.price > entry + 0.2 * A)?.price ?? entry + A;
    stop = Math.max(base, entry + 0.5 * A, entry + minStop) + 0.3 * A;
    const risk = stop - entry, t = S.find((l) => entry - l.price >= 1.5 * risk);
    target = t ? t.price : entry - 2 * risk; targetLabel = t ? t.label : "2× the risk";
    if (indian) steps.push("In Indian cash stocks you can only short intraday (it must be closed the same day). For delivery holders, this is a 'protect and wait' chart.");
    steps.push(`The bearish idea is wrong if price closes above ${f(stop)}.`);
  } else {
    const lo = S1?.price ?? price - 1.5 * A, hi = R1?.price ?? price + 1.5 * A;
    headline = `${when}No clear edge: the chart is between ${f(lo)} and ${f(hi)}. Let it pick a side before you do.`;
    steps.push(`Buy trigger: a candle closes above ${f(hi)} with rising volume. Stop back inside, near ${f(hi - A)}.`);
    steps.push(`Sell / exit trigger: a candle closes below ${f(lo)}. Stop near ${f(lo + A)}.`);
    steps.push(`Range traders buy near ${f(lo)} and sell near ${f(hi)} with tight stops. Waiting is also a position: most losses come from forcing trades in ranges.`);
  }

  if (open && indian && intraday) {
    const m = istMin(c.now);
    if (m < 9 * 60 + 30) steps.unshift("It's the first 15 minutes: let the opening range (its high and low) form before acting. Early moves often reverse.");
    else if (m >= 15 * 60) steps.unshift("It's after 15:00: avoid new intraday trades. Manage or exit open ones before the 15:20 auto square-off.");
    else if (m >= 12 * 60 && m < 13 * 60 + 30) steps.push("Midday is often slow; breakouts during the lull fail more often. Patience pays.");
  }
  const rr = side && entry != null && stop != null && target != null ? Math.abs(target - entry) / Math.abs(entry - stop) : null;
  if (rr != null && rr < 1.5) steps.push(`Reward to risk is only ${rr.toFixed(1)} : 1 here. Good trades usually offer at least 2 : 1; skipping this one is fine.`);
  if (side && capital > 0 && entry !== stop) {
    const riskAmt = capital * 0.01, byRisk = Math.floor(riskAmt / Math.abs(entry - stop)), byCash = Math.floor(capital / entry);
    qty = Math.min(byRisk, byCash);
    steps.push(qty < 1 ? `Your 1% risk (${f(riskAmt)}) is smaller than the stop distance for even one share. Skip it or use a smaller instrument.`
      : `Position size: risking 1% of your ${f(capital)} capital (${f(riskAmt)}) with a stop ${f(Math.abs(entry - stop))} away is about ${qty.toLocaleString()} shares`
        + (byCash < byRisk ? `, capped by your capital (the stop is tight, so the 1% rule alone would allow ${byRisk.toLocaleString()}).` : "."));
  }
  return { side, headline, steps, entry, stop, target, targetLabel, rr, qty: qty >= 1 ? qty : null };
}

/** What happened in the latest session, and the levels that matter for the next one. */
function recapOf(c, f) {
  const { ses, daily, A } = c, last = ses.today, prev = ses.prev;
  if (!last) return null;
  const dPrev = prev?.close ?? (daily && daily.length > 1 ? daily[daily.length - 2].close : null);
  const chg = dPrev ? ((last.close - dPrev) / dPrev) * 100 : null, loc = last.high > last.low ? (last.close - last.low) / (last.high - last.low) : 0.5;
  const lines = [];
  lines.push(`${chg != null ? `Closed ${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%` : "Closed"} at ${f(last.close)}, in a range of ${f(last.low)} to ${f(last.high)}.`);
  lines.push(loc >= 0.75 ? "It closed near the day's high: buyers held control into the close, which often carries into the next open."
    : loc <= 0.25 ? "It closed near the day's low: sellers held control into the close, a weak finish."
      : "It closed mid-range: neither side won the day convincingly.");
  if (daily && daily.length > 21) {
    const dv = daily[daily.length - 1].vol || 0, av = avg(daily.slice(-21, -1).map((x) => x.vol || 0));
    if (av > 0 && dv) lines.push(dv > 1.5 * av ? `Volume was ${(dv / av).toFixed(1)}× its 20-day average: a meaningful session.` : dv < 0.6 * av ? "Volume was light, so the move carries less weight." : "Volume was about normal.");
  }
  const scen = [
    `Opens and holds above ${f(last.high)} (the last session's high) for 15 minutes: strength. Buyers look for continuation; a fall back below it is a trap.`,
    `Breaks below ${f(last.low)} (the last session's low): weakness. Next support: ${c.levels.supports.find((l) => l.price < last.low - 0.2 * A) ? f(c.levels.supports.find((l) => l.price < last.low - 0.2 * A).price) : "the next swing low"}.`,
    `Opens between ${f(last.low)} and ${f(last.high)}: likely a range day. Wait for a break of either end.`,
  ];
  if (prev && Math.abs(last.open - prev.close) / prev.close > 0.005) lines.push(`The session opened with a ${last.open > prev.close ? "gap up" : "gap down"} of ${(Math.abs(last.open - prev.close) / prev.close * 100).toFixed(1)}%. Gaps are often partly filled within a few sessions.`);
  return { lines, scenarios: scen, high: last.high, low: last.low, close: last.close };
}

// ------------------------------------------------------------------ public
/**
 * Read a chart. `bars`: [{time, open, high, low, close, vol}] oldest first. Options: interval ("1m".."1D"),
 * open (market open, so the last bar is still forming), indian (NSE/BSE rules apply), daily (daily bars for
 * context, optional), capital (for position sizing, optional), fmt (price formatter).
 */
export function analyse(bars, { interval = "5m", open = false, indian = false, daily = null, capital = null, fmt = plainFmt, now = null } = {}) {
  const n = bars.length;
  if (n < 40) return null;
  const k = open ? n - 2 : n - 1, closes = bars.map((b) => b.close), intraday = interval !== "1D";
  const A = atr(bars)[k] || avg(bars.slice(-15).map((b) => b.high - b.low)) || closes[k] * 0.01;
  const ind = { e20: ema(closes, 20), s50: sma(closes, 50), s200: sma(closes, 200), rsi: rsi(closes), macd: macd(closes),
    bb: bollinger(closes), vwap: intraday ? vwap(bars) : [] };
  const piv = pivots(bars.slice(0, k + 1), interval === "1m" ? 5 : 3), trend = structure(piv, A), price = closes[n - 1];

  // sessions: today / previous (intraday), opening range, gap
  const ss = intraday ? sessions(bars) : null;
  const ses = intraday ? { today: ss[ss.length - 1], prev: ss[ss.length - 2] }
    : { today: { ...bars[n - 1], bars: [bars[n - 1]] }, prev: bars[n - 2] ? { ...bars[n - 2] } : null };
  let or = null;
  if (intraday && indian && ["1m", "5m", "15m"].includes(interval) && ses.today) {
    const first = ses.today.bars.filter((b) => b.time < ses.today.bars[0].time + 900);
    if (ses.today.bars.length > first.length) or = { high: Math.max(...first.map((b) => b.high)), low: Math.min(...first.map((b) => b.low)) };
  }
  // daily context (from separate daily bars on intraday charts)
  let dctx = null;
  const dsrc = intraday ? daily : bars;
  if (dsrc && dsrc.length >= 60) {
    const dc = dsrc.map((b) => b.close), d50 = sma(dc, 50), d200 = sma(dc, 200), j = dc.length - 1;
    const dPiv = pivots(dsrc, 3), dStr = structure(dPiv, atr(dsrc)[j] || dc[j] * 0.02);
    const above = (x) => x == null || dc[j] > x, below = (x) => x == null || dc[j] < x;
    const dir = dStr.dir || (d50[j] == null ? 0 : above(d50[j]) && above(d200[j]) ? 1 : below(d50[j]) && below(d200[j]) ? -1 : 0);
    const yr = dsrc.slice(-250), hi52 = Math.max(...yr.map((b) => b.high)), lo52 = Math.min(...yr.map((b) => b.low));
    dctx = { dir, label: dStr.label, sma50: d50[j], sma200: d200[j], hi52, lo52, fromHigh: ((dc[j] - hi52) / hi52) * 100 };
  }
  const named = [];
  if (intraday && ses.prev) named.push(["previous day high", ses.prev.high], ["previous day low", ses.prev.low], ["previous close", ses.prev.close]);
  if (intraday && ses.today && open) named.push(["today's open", ses.today.open]);
  if (!open && ses.today) named.push(["last session high", ses.today.high], ["last session low", ses.today.low]);  // what the next open is judged against
  if (or && open) named.push(["opening-range high", or.high], ["opening-range low", or.low]);
  if (intraday && open && ind.vwap[n - 1] != null) named.push(["VWAP", ind.vwap[n - 1]]);
  if (ind.s200[k] != null) named.push(["200 SMA", ind.s200[k]]);
  if (dctx && Math.abs(dctx.hi52 - price) / price < 0.03) named.push(["52-week high", dctx.hi52]);
  if (dctx && Math.abs(dctx.lo52 - price) / price < 0.03) named.push(["52-week low", dctx.lo52]);
  const levels = keyLevels(bars.slice(0, k + 1), piv, A, price, named);

  const c = { bars, n, k, A, closes, ind, piv, trend, price, intraday, ses, or, dctx, levels, open, indian, daily: intraday ? daily : bars,
    now: now ?? Date.now() / 1000 };
  c.patterns = chartPatterns(c, fmt);  // also boosts patterns that formed at a support/resistance level
  const candles = candlePatterns(bars, k, A, fmt), s1 = levels.supports[0], r1 = levels.resistances[0], kb = bars[k];
  for (const p of candles) {
    if (p.dir === "up" && s1 && Math.abs(kb.low - s1.price) <= 0.6 * A) { p.strength += 1; p.what += ` It formed right at support ${fmt(s1.price)} (${s1.label}).`; }
    if (p.dir === "down" && r1 && Math.abs(kb.high - r1.price) <= 0.6 * A) { p.strength += 1; p.what += ` It formed right at resistance ${fmt(r1.price)} (${r1.label}).`; }
  }
  c.patterns = [...candles, ...c.patterns];
  c.live = open ? liveEvents(c, fmt) : [];
  c.bias = biasOf(c);
  c.plan = planOf(c, fmt, capital);
  c.recap = open ? null : recapOf(c, fmt);
  const vAvg = avg(bars.slice(Math.max(0, k - 20), k).map((b) => b.vol || 0));
  return {
    time: bars[k].time, price, atr: A, interval, open, trend, daily: dctx, levels, or,
    rsi: ind.rsi[k], macdHist: ind.macd.hist[k], ema20: ind.e20[k], sma50: ind.s50[k], sma200: ind.s200[k], vwap: intraday ? ind.vwap[n - 1] : null,
    volX: vAvg > 0 && bars[k].vol ? bars[k].vol / vAvg : null,
    events: [...c.live, ...c.patterns].sort((a, b) => b.strength - a.strength), bias: c.bias, plan: c.plan, recap: c.recap,
  };
}
