"""Technical indicators in pure Python (no pandas/numpy).

Every function takes plain lists aligned to the price bars and returns a list
of the same length, with ``None`` where the indicator is not yet defined
(warm-up period). Values at index ``i`` use only data up to and including
``i`` -- nothing here looks ahead.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Series = list[float | None]


def sma(values: Sequence[float | None], n: int) -> Series:
    out: Series = [None] * len(values)
    window: list[float] = []
    total = 0.0
    for i, v in enumerate(values):
        if v is None:
            window, total = [], 0.0
            continue
        window.append(v)
        total += v
        if len(window) > n:
            total -= window.pop(0)
        if len(window) == n:
            out[i] = total / n
    return out


def ema(values: Sequence[float | None], n: int) -> Series:
    """Exponential moving average, seeded with the SMA of the first n defined values."""
    out: Series = [None] * len(values)
    k = 2 / (n + 1)
    seed: list[float] = []
    prev: float | None = None
    for i, v in enumerate(values):
        if v is None:
            continue
        if prev is None:
            seed.append(v)
            if len(seed) == n:
                prev = sum(seed) / n
                out[i] = prev
            continue
        prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def _wilder(values: Sequence[float], n: int) -> Series:
    """Wilder's smoothing (RMA), used by RSI, ATR and ADX."""
    out: Series = [None] * len(values)
    if len(values) < n:
        return out
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = _wilder(gains[1:], n)
    avg_loss = _wilder(losses[1:], n)
    for i in range(len(avg_gain)):
        g, l = avg_gain[i], avg_loss[i]
        if g is None or l is None:
            continue
        out[i + 1] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[Series, Series, Series]:
    ema_fast, ema_slow = ema(closes, fast), ema(closes, slow)
    line: Series = [f - s if f is not None and s is not None else None
                    for f, s in zip(ema_fast, ema_slow)]
    sig = ema(line, signal)
    hist: Series = [m - s if m is not None and s is not None else None for m, s in zip(line, sig)]
    return line, sig, hist


def stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def bollinger(closes: Sequence[float], n: int = 20, k: float = 2.0) -> tuple[Series, Series, Series]:
    mid = sma(closes, n)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        if mid[i] is None:
            continue
        window = closes[i - n + 1 : i + 1]
        # Bollinger uses the population standard deviation.
        sd = math.sqrt(sum((c - mid[i]) ** 2 for c in window) / n)
        upper[i], lower[i] = mid[i] + k * sd, mid[i] - k * sd
    return mid, upper, lower


def rolling_max(values: Sequence[float], n: int) -> Series:
    return [max(values[i - n + 1 : i + 1]) if i >= n - 1 else None for i in range(len(values))]


def rolling_min(values: Sequence[float], n: int) -> Series:
    return [min(values[i - n + 1 : i + 1]) if i >= n - 1 else None for i in range(len(values))]


def stochastic(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               n: int = 14, smooth: int = 3) -> tuple[Series, Series]:
    hh, ll = rolling_max(highs, n), rolling_min(lows, n)
    raw: Series = [None] * len(closes)
    for i in range(len(closes)):
        if hh[i] is None or ll[i] is None:
            continue
        rng = hh[i] - ll[i]
        raw[i] = 50.0 if rng == 0 else 100 * (closes[i] - ll[i]) / rng
    k = sma(raw, smooth)
    return k, sma(k, smooth)


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    tr = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return tr


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14) -> Series:
    return _wilder(true_range(highs, lows, closes), n)


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        n: int = 14) -> tuple[Series, Series, Series]:
    """Average Directional Index with +DI / -DI (Wilder)."""
    size = len(closes)
    adx_out: Series = [None] * size
    pdi_out: Series = [None] * size
    mdi_out: Series = [None] * size
    if size <= 2 * n:
        return adx_out, pdi_out, mdi_out
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, size):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr_s = _wilder(true_range(highs, lows, closes)[1:], n)
    pdm_s, mdm_s = _wilder(plus_dm[1:], n), _wilder(minus_dm[1:], n)
    dx: list[float] = []
    dx_index: list[int] = []
    for j in range(len(tr_s)):
        if tr_s[j] is None or tr_s[j] == 0:
            continue
        pdi = 100 * pdm_s[j] / tr_s[j]  # type: ignore[operator]
        mdi = 100 * mdm_s[j] / tr_s[j]  # type: ignore[operator]
        pdi_out[j + 1], mdi_out[j + 1] = pdi, mdi
        dx.append(0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi))
        dx_index.append(j + 1)
    smoothed = _wilder(dx, n)
    for k, idx in enumerate(dx_index):
        adx_out[idx] = smoothed[k]
    return adx_out, pdi_out, mdi_out


def cci(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 20) -> Series:
    tp = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    out: Series = [None] * len(tp)
    for i in range(n - 1, len(tp)):
        window = tp[i - n + 1 : i + 1]
        mean = sum(window) / n
        mad = sum(abs(x - mean) for x in window) / n
        out[i] = 0.0 if mad == 0 else (tp[i] - mean) / (0.015 * mad)
    return out


def mfi(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        volumes: Sequence[float], n: int = 14) -> Series:
    tp = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    out: Series = [None] * len(tp)
    for i in range(n, len(tp)):
        pos = neg = 0.0
        for j in range(i - n + 1, i + 1):
            flow = tp[j] * volumes[j]
            if tp[j] > tp[j - 1]:
                pos += flow
            elif tp[j] < tp[j - 1]:
                neg += flow
        out[i] = 100.0 if neg == 0 else 100 - 100 / (1 + pos / neg)
    return out


def obv(closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    out = [0.0]
    for i in range(1, len(closes)):
        step = volumes[i] if closes[i] > closes[i - 1] else -volumes[i] if closes[i] < closes[i - 1] else 0
        out.append(out[-1] + step)
    return out


def roc(closes: Sequence[float], n: int) -> Series:
    return [closes[i] / closes[i - n] - 1 if i >= n and closes[i - n] else None for i in range(len(closes))]


def linreg(values: Sequence[float]) -> tuple[float, float]:
    """Least-squares slope (per step) and R^2 of values against 0..n-1."""
    n = len(values)
    if n < 2:
        return 0.0, 0.0
    mean_x, mean_y = (n - 1) / 2, sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in range(n))
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in enumerate(values))
    syy = sum((y - mean_y) ** 2 for y in values)
    slope = sxy / sxx
    r2 = 0.0 if syy == 0 else (sxy * sxy) / (sxx * syy)
    return slope, r2


def candle_pattern(opens: Sequence[float], highs: Sequence[float], lows: Sequence[float],
                   closes: Sequence[float], i: int) -> tuple[str, float]:
    """Classic one/two-bar patterns at index i -> (name, vote in [-1, 1])."""
    if i < 1:
        return "none", 0.0
    o, h, l, c = opens[i], highs[i], lows[i], closes[i]
    po, pc = opens[i - 1], closes[i - 1]
    body, rng = abs(c - o), h - l
    if rng <= 0:
        return "none", 0.0
    upper, lower = h - max(o, c), min(o, c) - l
    if pc < po and c > o and o <= pc and c >= po:
        return "bullish engulfing", 1.0
    if pc > po and c < o and o >= pc and c <= po:
        return "bearish engulfing", -1.0
    if body <= 0.35 * rng and lower >= 2 * body and upper <= 0.25 * rng:
        return "hammer", 0.6
    if body <= 0.35 * rng and upper >= 2 * body and lower <= 0.25 * rng:
        return "shooting star", -0.6
    if body <= 0.1 * rng:
        return "doji", 0.0
    return "none", 0.0
