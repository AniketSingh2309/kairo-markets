// Chart indicators, pure functions over arrays/bars (shared by the Terminal chart and Kai, the teacher).
export const sma = (v, n) => { const o = new Array(v.length).fill(null); let s = 0; for (let i = 0; i < v.length; i++) { s += v[i]; if (i >= n) s -= v[i - n]; if (i >= n - 1) o[i] = s / n; } return o; };
export const ema = (v, n) => { const o = new Array(v.length).fill(null), k = 2 / (n + 1); let e = null; for (let i = 0; i < v.length; i++) { if (i === n - 1) { e = v.slice(0, n).reduce((a, b) => a + b, 0) / n; } else if (i >= n) e = v[i] * k + e * (1 - k); if (i >= n - 1) o[i] = e; } return o; };
export function bollinger(v, n = 20, k = 2) {
  const mid = sma(v, n), up = [], lo = [];
  for (let i = 0; i < v.length; i++) {
    if (mid[i] == null) { up.push(null); lo.push(null); continue; }
    let s = 0; for (let j = i - n + 1; j <= i; j++) s += (v[j] - mid[i]) ** 2;
    const sd = Math.sqrt(s / n); up.push(mid[i] + k * sd); lo.push(mid[i] - k * sd);
  }
  return { mid, up, lo };
}
export function rsi(v, n = 14) {
  const o = new Array(v.length).fill(null); let g = 0, l = 0;
  for (let i = 1; i < v.length; i++) {
    const d = v[i] - v[i - 1], up = Math.max(d, 0), dn = Math.max(-d, 0);
    if (i <= n) { g += up; l += dn; if (i === n) { g /= n; l /= n; o[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l); } }
    else { g = (g * (n - 1) + up) / n; l = (l * (n - 1) + dn) / n; o[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l); }
  }
  return o;
}
export function macd(v) {
  const f = ema(v, 12), s = ema(v, 26), line = v.map((_, i) => (f[i] != null && s[i] != null ? f[i] - s[i] : null));
  const start = line.findIndex((x) => x != null), sig = new Array(v.length).fill(null);
  if (start >= 0) { const e = ema(line.slice(start), 9); e.forEach((x, i) => (sig[start + i] = x)); }
  return { line, sig, hist: line.map((x, i) => (x != null && sig[i] != null ? x - sig[i] : null)) };
}
export const dayKey = (t) => { const d = new Date(t * 1000); return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; };
export function vwap(bars) {
  const o = []; let pv = 0, vv = 0, key = "";
  for (const b of bars) {
    const k = dayKey(b.time); if (k !== key) { key = k; pv = 0; vv = 0; }
    const tp = (b.high + b.low + b.close) / 3; pv += tp * (b.vol || 0); vv += b.vol || 0;
    o.push(vv > 0 ? pv / vv : null);
  }
  return o;
}
/** Average true range (Wilder), the "normal" size of one candle. */
export function atr(bars, n = 14) {
  const o = new Array(bars.length).fill(null); let a = null;
  for (let i = 1; i < bars.length; i++) {
    const b = bars[i], pc = bars[i - 1].close, tr = Math.max(b.high - b.low, Math.abs(b.high - pc), Math.abs(b.low - pc));
    if (i < n) { a = (a ?? 0) + tr; if (i === n - 1) { a /= n - 1; o[i] = a; } } else { a = (a * (n - 1) + tr) / n; o[i] = a; }
  }
  return o;
}
/** Swing points: a high (low) that no bar within `k` bars on either side exceeds. Confirmed k bars late, as on a real chart. */
export function pivots(bars, k = 3) {
  const highs = [], lows = [];
  for (let i = k; i < bars.length - k; i++) {
    let hi = true, lo = true;
    for (let j = i - k; j <= i + k; j++) {
      if (j === i) continue;
      if (bars[j].high >= bars[i].high) hi = false;
      if (bars[j].low <= bars[i].low) lo = false;
    }
    if (hi) highs.push({ i, price: bars[i].high, time: bars[i].time });
    if (lo) lows.push({ i, price: bars[i].low, time: bars[i].time });
  }
  return { highs, lows };
}
