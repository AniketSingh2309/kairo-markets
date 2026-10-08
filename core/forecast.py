"""Directional forecast from technical analysis, with an honest backtest.

How it works
------------
1. 16 classic techniques (trend, momentum, oscillators, volume, breakout,
   candlestick patterns) are computed for every day of history. Each one is
   turned into a *vote* in [-1, +1] using its textbook interpretation (e.g.
   RSI 75 = overbought = bearish vote).
2. **Indicator consensus**: the equal-weight mean of all votes.
3. **Adaptive model**: each technique is weighted by how often its vote has
   actually called the next-``h``-day direction correctly *on this stock*
   (shrunk towards zero when evidence is thin; a technique that has worked in
   reverse gets a negative weight). The weighted score is mapped to a
   probability with a logistic calibration fitted on the same training data.
4. **Walk-forward backtest**: at each test date the model is retrained using
   only outcomes that were already known on that date, then predicts the next
   ``h`` days. Test dates are ``h`` apart so outcomes do not overlap. The hit
   rate is compared with a naive baseline ("predict whichever direction has
   been more common so far"), with a 95% Wilson interval.

Confidence is only ever "low" or "moderate": "moderate" requires the backtest
to beat the baseline with a lower confidence bound above 50%. Short-horizon
price direction is close to a coin flip for liquid stocks; this module is
built to *measure* that rather than hide it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from core import technicals as ta
from core.facts import FactRegistry
from core.schemas import (
    BacktestReport,
    ChartPoint,
    Forecast,
    IndicatorReading,
    ModelBacktest,
)
from tools.models import PriceBar

MIN_BARS = 60          # below this no forecast is attempted
WARMUP = 50            # first bar index at which votes are computed
MIN_TRAIN = 100        # labelled samples needed to fit the adaptive model
MIN_BACKTEST = 20      # fewer out-of-sample predictions than this = no verdict
CHART_BARS = 180
UP_THRESHOLD, DOWN_THRESHOLD = 0.55, 0.45
SHRINK = 50            # pseudo-count shrinking hit rates towards 50%

DISCLAIMER = (
    "Statistical estimate from past price patterns only. It ignores news, earnings, macro events "
    "and fundamentals, and past hit rates do not guarantee future results. Not investment advice."
)


@dataclass(frozen=True)
class FeatureDef:
    key: str
    name: str
    category: str


FEATURES: tuple[FeatureDef, ...] = (
    FeatureDef("price_vs_sma50", "Price vs 50-day SMA", "trend"),
    FeatureDef("sma50_vs_sma200", "Golden/death cross (SMA50 vs SMA200)", "trend"),
    FeatureDef("ema_cross", "EMA 12/26 crossover", "trend"),
    FeatureDef("regression", "20-day linear-regression trend", "trend"),
    FeatureDef("adx_dmi", "ADX / directional movement (14)", "trend"),
    FeatureDef("macd", "MACD histogram (12,26,9)", "momentum"),
    FeatureDef("roc10", "10-day rate of change", "momentum"),
    FeatureDef("roc60", "60-day rate of change", "momentum"),
    FeatureDef("rsi", "RSI (14)", "oscillator"),
    FeatureDef("stochastic", "Stochastic %K (14,3)", "oscillator"),
    FeatureDef("bollinger", "Bollinger %B (20,2)", "oscillator"),
    FeatureDef("cci", "Commodity Channel Index (20)", "oscillator"),
    FeatureDef("mfi", "Money Flow Index (14)", "volume"),
    FeatureDef("obv", "On-balance volume trend (20)", "volume"),
    FeatureDef("donchian", "Donchian channel breakout (20)", "breakout"),
    FeatureDef("candle", "Candlestick pattern", "pattern"),
)


def _clip(x: float, limit: float = 1.0) -> float:
    return max(-limit, min(limit, x))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    e = math.exp(x)
    return e / (1 + e)


def wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


# ---------------------------------------------------------------------------
# Indicators -> votes
# ---------------------------------------------------------------------------


@dataclass
class Indicators:
    dates: list
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    volumes: list[float]
    s: dict[str, list] = field(default_factory=dict)


def compute_indicators(bars: Sequence[PriceBar]) -> Indicators:
    ind = Indicators(
        dates=[b.date for b in bars], opens=[b.open for b in bars], highs=[b.high for b in bars],
        lows=[b.low for b in bars], closes=[b.close for b in bars],
        volumes=[float(b.volume) for b in bars],
    )
    h, l, c, v = ind.highs, ind.lows, ind.closes, ind.volumes
    s = ind.s
    s["sma20"], s["sma50"], s["sma200"] = ta.sma(c, 20), ta.sma(c, 50), ta.sma(c, 200)
    s["ema12"], s["ema26"] = ta.ema(c, 12), ta.ema(c, 26)
    s["macd"], s["macd_signal"], s["macd_hist"] = ta.macd(c)
    s["rsi"] = ta.rsi(c, 14)
    s["stoch_k"], s["stoch_d"] = ta.stochastic(h, l, c, 14, 3)
    s["bb_mid"], s["bb_upper"], s["bb_lower"] = ta.bollinger(c, 20, 2.0)
    s["adx"], s["pdi"], s["mdi"] = ta.adx(h, l, c, 14)
    s["cci"] = ta.cci(h, l, c, 20)
    s["mfi"] = ta.mfi(h, l, c, v, 14)
    s["obv"] = ta.obv(c, v)
    s["roc10"], s["roc60"] = ta.roc(c, 10), ta.roc(c, 60)
    s["atr"] = ta.atr(h, l, c, 14)
    s["log_close"] = [math.log(x) for x in c]
    return ind


def votes_at(ind: Indicators, i: int) -> dict[str, float | None]:
    """Textbook reading of every technique at bar i, as a vote in [-1, 1] (None = undefined)."""
    s, c = ind.s, ind.closes[i]
    out: dict[str, float | None] = {f.key: None for f in FEATURES}
    if s["sma50"][i]:
        out["price_vs_sma50"] = _clip((c / s["sma50"][i] - 1) / 0.05)
    if s["sma50"][i] and s["sma200"][i]:
        out["sma50_vs_sma200"] = _clip((s["sma50"][i] / s["sma200"][i] - 1) / 0.05)
    if s["ema12"][i] is not None and s["ema26"][i] is not None:
        out["ema_cross"] = _clip((s["ema12"][i] - s["ema26"][i]) / c / 0.02)
    if i >= 19:
        slope, r2 = ta.linreg(s["log_close"][i - 19 : i + 1])
        out["regression"] = _clip(slope * 20 / 0.05) * r2
    if s["adx"][i] is not None and s["pdi"][i] is not None and (s["pdi"][i] + s["mdi"][i]) > 0:
        direction = (s["pdi"][i] - s["mdi"][i]) / (s["pdi"][i] + s["mdi"][i])
        out["adx_dmi"] = _clip(direction * min(s["adx"][i] / 25, 1.0))
    if s["macd_hist"][i] is not None:
        out["macd"] = _clip(s["macd_hist"][i] / c / 0.005)
    if s["roc10"][i] is not None:
        out["roc10"] = _clip(s["roc10"][i] / 0.05)
    if s["roc60"][i] is not None:
        out["roc60"] = _clip(s["roc60"][i] / 0.15)
    if s["rsi"][i] is not None:
        out["rsi"] = _clip((50 - s["rsi"][i]) / 25)
    if s["stoch_k"][i] is not None:
        out["stochastic"] = _clip((50 - s["stoch_k"][i]) / 40)
    up, lo = s["bb_upper"][i], s["bb_lower"][i]
    if up is not None and lo is not None and up > lo:
        out["bollinger"] = _clip((0.5 - (c - lo) / (up - lo)) * 2)
    if s["cci"][i] is not None:
        out["cci"] = _clip(-s["cci"][i] / 150)
    if s["mfi"][i] is not None:
        out["mfi"] = _clip((50 - s["mfi"][i]) / 30)
    if i >= 19:
        slope, _ = ta.linreg(s["obv"][i - 19 : i + 1])
        avg_vol = sum(ind.volumes[i - 19 : i + 1]) / 20
        out["obv"] = _clip(slope / avg_vol / 0.3) if avg_vol > 0 else 0.0
    if i >= 20:
        hh, ll = max(ind.highs[i - 20 : i]), min(ind.lows[i - 20 : i])
        if c > hh:
            out["donchian"] = 1.0
        elif c < ll:
            out["donchian"] = -1.0
        elif hh > ll:
            out["donchian"] = ((c - ll) / (hh - ll) - 0.5)
    out["candle"] = ta.candle_pattern(ind.opens, ind.highs, ind.lows, ind.closes, i)[1]
    return out


def describe_at(ind: Indicators, i: int) -> dict[str, tuple[str, str]]:
    """Human-readable (value, reading) per technique at bar i."""
    s, c = ind.s, ind.closes[i]
    d: dict[str, tuple[str, str]] = {}

    def lvl(x, hi, lo, hi_txt, lo_txt, mid_txt="neutral"):
        return hi_txt if x >= hi else lo_txt if x <= lo else mid_txt

    if s["sma50"][i]:
        d["price_vs_sma50"] = (f"{(c / s['sma50'][i] - 1) * 100:+.2f}%",
                               "price above its 50-day average" if c >= s["sma50"][i] else "price below its 50-day average")
    if s["sma50"][i] and s["sma200"][i]:
        d["sma50_vs_sma200"] = (f"SMA50 {s['sma50'][i]:.2f} / SMA200 {s['sma200'][i]:.2f}",
                                "golden-cross regime (SMA50 above SMA200)" if s["sma50"][i] >= s["sma200"][i]
                                else "death-cross regime (SMA50 below SMA200)")
    else:
        d["sma50_vs_sma200"] = ("n/a", "needs 200 days of history")
    if s["ema12"][i] is not None and s["ema26"][i] is not None:
        d["ema_cross"] = (f"{(s['ema12'][i] - s['ema26'][i]):+.2f}",
                          "fast EMA above slow EMA" if s["ema12"][i] >= s["ema26"][i] else "fast EMA below slow EMA")
    if i >= 19:
        slope, r2 = ta.linreg(s["log_close"][i - 19 : i + 1])
        d["regression"] = (f"{slope * 100:+.3f}%/day, R2 {r2:.2f}",
                           ("rising" if slope > 0 else "falling") + (" and orderly" if r2 >= 0.5 else " but noisy"))
    if s["adx"][i] is not None:
        d["adx_dmi"] = (f"ADX {s['adx'][i]:.1f}, +DI {s['pdi'][i]:.1f}, -DI {s['mdi'][i]:.1f}",
                        ("strong " if s["adx"][i] >= 25 else "weak ") +
                        ("uptrend" if s["pdi"][i] >= s["mdi"][i] else "downtrend"))
    if s["macd_hist"][i] is not None:
        d["macd"] = (f"{s['macd_hist'][i]:+.3f}",
                     "MACD above signal line (bullish momentum)" if s["macd_hist"][i] >= 0
                     else "MACD below signal line (bearish momentum)")
    if s["roc10"][i] is not None:
        d["roc10"] = (f"{s['roc10'][i] * 100:+.2f}%", "positive 2-week momentum" if s["roc10"][i] >= 0
                      else "negative 2-week momentum")
    if s["roc60"][i] is not None:
        d["roc60"] = (f"{s['roc60'][i] * 100:+.2f}%", "positive 3-month momentum" if s["roc60"][i] >= 0
                      else "negative 3-month momentum")
    if s["rsi"][i] is not None:
        d["rsi"] = (f"{s['rsi'][i]:.1f}", lvl(s["rsi"][i], 70, 30, "overbought", "oversold"))
    if s["stoch_k"][i] is not None:
        d["stochastic"] = (f"{s['stoch_k'][i]:.1f}", lvl(s["stoch_k"][i], 80, 20, "overbought", "oversold"))
    up, lo = s["bb_upper"][i], s["bb_lower"][i]
    if up is not None and lo is not None and up > lo:
        pb = (c - lo) / (up - lo)
        d["bollinger"] = (f"{pb:.2f}", lvl(pb, 1.0, 0.0, "above upper band", "below lower band",
                                           "inside the bands"))
    if s["cci"][i] is not None:
        d["cci"] = (f"{s['cci'][i]:.1f}", lvl(s["cci"][i], 100, -100, "overbought", "oversold"))
    if s["mfi"][i] is not None:
        d["mfi"] = (f"{s['mfi'][i]:.1f}", lvl(s["mfi"][i], 80, 20, "overbought (heavy buying)",
                                               "oversold (heavy selling)"))
    if i >= 19:
        slope, _ = ta.linreg(s["obv"][i - 19 : i + 1])
        d["obv"] = ("rising" if slope > 0 else "falling",
                    "volume confirming buying" if slope > 0 else "volume confirming selling")
    if i >= 20:
        hh, ll = max(ind.highs[i - 20 : i]), min(ind.lows[i - 20 : i])
        d["donchian"] = (f"range {ll:.2f}-{hh:.2f}",
                         "breakout above 20-day high" if c > hh else "breakdown below 20-day low" if c < ll
                         else "inside 20-day range")
    name, _ = ta.candle_pattern(ind.opens, ind.highs, ind.lows, ind.closes, i)
    d["candle"] = (name, "no signal pattern on the last bar" if name in ("none", "doji") else f"{name} on the last bar")
    return d


def labels(closes: Sequence[float], horizon: int) -> list[int | None]:
    return [(1 if closes[i + horizon] > closes[i] else 0) if i + horizon < len(closes) else None
            for i in range(len(closes))]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def consensus(row: dict[str, float | None]) -> float:
    vals = [v for v in row.values() if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


@dataclass
class AdaptiveModel:
    weights: dict[str, float]
    hit_rates: dict[str, float | None]
    samples: dict[str, int]
    a: float = 0.0
    b: float = 0.0
    trained_on: int = 0

    def score(self, row: dict[str, float | None]) -> float:
        num = sum(self.weights[k] * v for k, v in row.items() if v is not None and k in self.weights)
        den = sum(abs(self.weights[k]) for k, v in row.items() if v is not None and k in self.weights)
        return num / den if den > 0 else 0.0

    def prob_up(self, row: dict[str, float | None]) -> float:
        return _sigmoid(self.a * self.score(row) + self.b)


def fit_adaptive(rows: Sequence[dict[str, float | None]], ys: Sequence[int]) -> AdaptiveModel:
    weights: dict[str, float] = {}
    hit_rates: dict[str, float | None] = {}
    samples: dict[str, int] = {}
    for f in FEATURES:
        hits = n = 0
        for row, y in zip(rows, ys):
            v = row.get(f.key)
            if v is None or abs(v) < 0.1:
                continue
            n += 1
            hits += (v > 0) == (y == 1)
        samples[f.key] = n
        hit_rates[f.key] = hits / n if n else None
        weights[f.key] = ((hits / n - 0.5) * 2 * n / (n + SHRINK)) if n else 0.0
    model = AdaptiveModel(weights=weights, hit_rates=hit_rates, samples=samples, trained_on=len(rows))
    model.a, model.b = _calibrate([model.score(r) for r in rows], ys)
    return model


def _calibrate(scores: Sequence[float], ys: Sequence[int], iters: int = 25,
               l2: float = 1.0) -> tuple[float, float]:
    """Fit p = sigmoid(a*score + b) by regularised Newton's method."""
    a = b = 0.0
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for s, y in zip(scores, ys):
            p = _sigmoid(a * s + b)
            r, w = p - y, p * (1 - p)
            ga += r * s
            gb += r
            haa += w * s * s
            hab += w * s
            hbb += w
        ga += l2 * a
        haa += l2
        hbb += 1e-6
        det = haa * hbb - hab * hab
        if det <= 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = _clip(a - da, 50), _clip(b - db, 5)
        if abs(da) + abs(db) < 1e-7:
            break
    return a, b


def _train_window(rows, ys, t: int, horizon: int):
    """Samples whose outcome was already known at bar t (i + horizon <= t)."""
    idx = [i for i in range(WARMUP, t - horizon + 1) if ys[i] is not None]
    return [rows[i] for i in idx], [ys[i] for i in idx]  # type: ignore[misc]


def predict_at(rows, ys, t: int, horizon: int) -> tuple[float | None, float, int]:
    """(adaptive P(up) or None if too little history, consensus score, training size) at bar t."""
    train_x, train_y = _train_window(rows, ys, t, horizon)
    cons = consensus(rows[t])
    if len(train_x) < MIN_TRAIN:
        return None, cons, len(train_x)
    return fit_adaptive(train_x, train_y).prob_up(rows[t]), cons, len(train_x)


def backtest(ind: Indicators, rows, ys, horizon: int) -> BacktestReport | None:
    n = len(ind.closes)
    first = WARMUP + MIN_TRAIN + horizon
    tests = list(range(first, n - horizon, horizon))
    if not tests:
        return None
    adaptive_hits = consensus_hits = baseline_hits = ups = 0
    for t in tests:
        p, cons, _ = predict_at(rows, ys, t, horizon)
        actual = ys[t]
        train_y = _train_window(rows, ys, t, horizon)[1]
        majority = 1 if sum(train_y) * 2 >= len(train_y) else 0
        adaptive_hits += ((p or 0.5) >= 0.5) == (actual == 1)
        consensus_hits += (cons >= 0) == (actual == 1)
        baseline_hits += majority == actual
        ups += actual == 1

    total = len(tests)

    def report(name: str, hits: int) -> ModelBacktest:
        lo, hi = wilson_interval(hits, total)
        return ModelBacktest(model=name, predictions=total, hit_rate=round(hits / total, 4),
                             ci_low=round(lo, 4), ci_high=round(hi, 4))

    adaptive = report("adaptive", adaptive_hits)
    baseline = baseline_hits / total
    edge = (adaptive.hit_rate or 0) - baseline
    return BacktestReport(
        method=f"walk-forward, retrained at each test date, non-overlapping {horizon}-day windows",
        horizon_days=horizon,
        first_test_date=ind.dates[tests[0]],
        last_test_date=ind.dates[tests[-1]],
        models=[adaptive, report("consensus", consensus_hits)],
        baseline_hit_rate=round(baseline, 4),
        up_rate=round(ups / total, 4),
        edge_vs_baseline=round(edge, 4),
        significant=total >= MIN_BACKTEST and (adaptive.ci_low or 0) > 0.5 and edge >= 0.03,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_forecast(bars: Sequence[PriceBar], horizon: int, currency: str) -> Forecast | None:
    if len(bars) < MIN_BARS:
        return None
    ind = compute_indicators(bars)
    n = len(bars)
    rows = [votes_at(ind, i) if i >= WARMUP else {} for i in range(n)]
    ys = labels(ind.closes, horizon)
    last = n - 1

    p_adaptive, cons, train_size = predict_at(rows, ys, last, horizon)
    model = None
    if p_adaptive is not None:
        tx, ty = _train_window(rows, ys, last, horizon)
        model = fit_adaptive(tx, ty)
        p_up, model_used = p_adaptive, f"adaptive weighted ensemble (trained on {train_size} samples)"
    else:
        # Too little history to learn weights: fall back to the raw consensus,
        # squashed so it can never look confident.
        p_up, model_used = 0.5 + 0.2 * cons, "indicator consensus (uncalibrated: too little history)"

    direction = "up" if p_up >= UP_THRESHOLD else "down" if p_up <= DOWN_THRESHOLD else "no_clear_edge"
    bt = backtest(ind, rows, ys, horizon)

    if bt is None or bt.models[0].predictions < MIN_BACKTEST:
        confidence, reason = "low", "not enough history to backtest the model reliably"
    elif not bt.significant:
        confidence, reason = "low", (
            f"in backtesting the model was right {bt.models[0].hit_rate:.0%} of the time vs "
            f"{bt.baseline_hit_rate:.0%} for a naive baseline - no demonstrated edge")
    elif direction == "no_clear_edge":
        confidence, reason = "low", "the model has had an edge historically but gives no clear signal now"
    else:
        confidence, reason = "moderate", (
            f"backtested hit rate {bt.models[0].hit_rate:.0%} vs baseline {bt.baseline_hit_rate:.0%} "
            f"over {bt.models[0].predictions} out-of-sample predictions")

    log_returns = [math.log(ind.closes[i] / ind.closes[i - 1]) for i in range(max(1, n - 60), n)]
    sigma_h = ta.stdev(log_returns) * math.sqrt(horizon)
    close = ind.closes[last]

    described = describe_at(ind, last)
    votes = rows[last]
    readings = [
        IndicatorReading(
            key=f.key, name=f.name, category=f.category,  # type: ignore[arg-type]
            value=described.get(f.key, ("n/a", ""))[0],
            reading=described.get(f.key, ("n/a", "not enough history"))[1],
            vote=round(votes.get(f.key) or 0.0, 3),
            weight=round(model.weights[f.key], 3) if model else None,
            hit_rate=round(model.hit_rates[f.key], 3) if model and model.hit_rates[f.key] is not None else None,
            samples=model.samples[f.key] if model else 0,
        )
        for f in FEATURES
    ]

    s = ind.s
    chart = [
        ChartPoint(date=ind.dates[i], open=ind.opens[i], high=ind.highs[i], low=ind.lows[i],
                   close=ind.closes[i],
                   **{k: (round(s[src][i], 4) if s[src][i] is not None else None)
                      for k, src in (("sma20", "sma20"), ("sma50", "sma50"), ("sma200", "sma200"),
                                     ("bb_upper", "bb_upper"), ("bb_lower", "bb_lower"))})
        for i in range(max(0, n - CHART_BARS), n)
    ]

    return Forecast(
        as_of=ind.dates[last], horizon_days=horizon, last_close=close, currency=currency,
        direction=direction, probability_up=round(p_up, 4), model_used=model_used,
        consensus_score=round(cons, 3),
        adaptive_score=round(model.score(votes), 3) if model else None,
        confidence=confidence, confidence_reason=reason,
        expected_low=round(close * math.exp(-sigma_h), 2),
        expected_high=round(close * math.exp(sigma_h), 2),
        sigma_pct=round(sigma_h * 100, 2),
        indicators=readings, backtest=bt, chart=chart, disclaimer=DISCLAIMER,
    )


_FACT_INDICATORS = ("rsi", "macd", "adx_dmi", "bollinger", "stochastic", "sma50_vs_sma200", "price_vs_sma50")


def add_forecast_facts(reg: FactRegistry, fc: Forecast, source_id: str, observed_at) -> None:
    """Expose the forecast as citable computed facts."""
    common = {"kind": "computed", "source_id": source_id, "observed_at": observed_at,
              "method": "core/forecast.py (deterministic, from daily bars)"}
    h, day = fc.horizon_days, fc.as_of.isoformat()
    label = {"up": "UP", "down": "DOWN", "no_clear_edge": "NO CLEAR EDGE"}[fc.direction]
    reg.add("C", label=f"Statistical {h}-day direction outlook (as of {day})", value=label,
            display=label, detail=f"model: {fc.model_used}", **common)
    reg.add("C", label=f"Model probability of a higher close in {h} trading days",
            value=round(fc.probability_up * 100, 1), unit="%", display=f"{fc.probability_up * 100:.1f}%",
            **common)
    reg.add("C", label="Forecast confidence", value=fc.confidence, display=fc.confidence,
            detail=fc.confidence_reason, **common)
    reg.add("C", label=f"Expected {h}-day price range (1 sigma, about 68% of outcomes)",
            value=f"{fc.expected_low:.2f} to {fc.expected_high:.2f} {fc.currency}",
            display=f"{fc.expected_low:.2f} to {fc.expected_high:.2f} {fc.currency} "
                    f"(+/-{fc.sigma_pct:.2f}%)", **common)
    if fc.backtest is not None:
        adaptive = fc.backtest.models[0]
        if adaptive.hit_rate is not None:
            reg.add("C", label=f"Backtested hit rate of the forecast model "
                               f"({adaptive.predictions} out-of-sample predictions)",
                    value=round(adaptive.hit_rate * 100, 1), unit="%",
                    display=f"{adaptive.hit_rate * 100:.1f}%", **common)
        if fc.backtest.baseline_hit_rate is not None:
            reg.add("C", label="Backtested hit rate of a naive baseline",
                    value=round(fc.backtest.baseline_hit_rate * 100, 1), unit="%",
                    display=f"{fc.backtest.baseline_hit_rate * 100:.1f}%", **common)
    for r in fc.indicators:
        if r.key in _FACT_INDICATORS and r.value != "n/a":
            reg.add("C", label=f"{r.name} as of {day}", value=r.value,
                    display=f"{r.value} ({r.reading})", **common)
