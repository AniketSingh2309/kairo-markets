"""Technical indicators, forecast model, backtest integrity and honesty."""

from __future__ import annotations

import datetime as dt
import math
import random

import pytest

from core import technicals as ta
from core.facts import FactRegistry
from core.forecast import (
    FEATURES,
    MIN_BARS,
    WARMUP,
    add_forecast_facts,
    build_forecast,
    compute_indicators,
    labels,
    predict_at,
    votes_at,
    wilson_interval,
)
from core.schemas import AnalyzeRequest
from core.validation import Grounder
from tools.models import PriceBar


def make_bars(closes: list[float], start=dt.date(2024, 1, 1)) -> list[PriceBar]:
    bars, day, prev = [], start, closes[0]
    for i, c in enumerate(closes):
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        o = prev
        bars.append(PriceBar(date=day, open=round(o, 4), high=round(max(o, c) * 1.004, 4),
                             low=round(min(o, c) * 0.996, 4), close=round(c, 4),
                             volume=1_000_000 + (i * 7919) % 500_000))
        prev, day = c, day + dt.timedelta(days=1)
    return bars


def random_walk(n: int, seed: int) -> list[float]:
    rng, price, out = random.Random(seed), 100.0, []
    for _ in range(n):
        price *= math.exp(rng.gauss(0, 0.015))
        out.append(price)
    return out


# --- indicators --------------------------------------------------------------------


def test_sma_and_ema():
    assert ta.sma([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]
    e = ta.ema([1, 2, 3, 4, 5], 3)
    assert e[:2] == [None, None] and e[2] == 2 and e[3] == pytest.approx(3.0) and e[4] == pytest.approx(4.0)


def test_rsi_extremes():
    assert ta.rsi(list(range(1, 40)), 14)[-1] == 100.0
    assert ta.rsi(list(range(40, 1, -1)), 14)[-1] == pytest.approx(0.0)


def test_bollinger_flat_series_has_zero_width():
    mid, up, lo = ta.bollinger([10.0] * 25, 20, 2)
    assert mid[-1] == up[-1] == lo[-1] == 10.0


def test_linreg_exact_line():
    slope, r2 = ta.linreg([3 + 2 * x for x in range(10)])
    assert slope == pytest.approx(2.0) and r2 == pytest.approx(1.0)


def test_trend_indicators_agree_on_a_steady_uptrend():
    bars = make_bars([100 * 1.004 ** i for i in range(120)])
    ind = compute_indicators(bars)
    s, i = ind.s, len(bars) - 1
    assert s["macd_hist"][i] is not None and s["macd"][i] > 0
    assert s["pdi"][i] > s["mdi"][i]
    assert s["sma20"][i] > s["sma50"][i]
    votes = votes_at(ind, i)
    assert votes["price_vs_sma50"] > 0 and votes["ema_cross"] > 0 and votes["regression"] > 0
    assert votes["rsi"] < 0  # textbook: persistent gains = overbought = contrarian bearish vote


def test_candle_patterns():
    assert ta.candle_pattern([10, 8.9], [10.1, 10.6], [8.8, 8.8], [9, 10.5], 1)[0] == "bullish engulfing"
    assert ta.candle_pattern([9, 10.5], [10.1, 10.6], [8.8, 8.8], [10, 8.9], 1)[0] == "bearish engulfing"


def test_wilson_interval():
    lo, hi = wilson_interval(60, 100)
    assert 0.50 < lo < 0.6 < hi < 0.7


# --- model integrity ------------------------------------------------------------------


def test_too_little_history_gives_no_forecast():
    assert build_forecast(make_bars(random_walk(MIN_BARS - 1, 1)), 5, "USD") is None


def test_no_look_ahead():
    """A prediction made at bar t must not change when later bars are added."""
    bars = make_bars(random_walk(400, 7))
    horizon, t = 5, 300
    full = compute_indicators(bars)
    cut = compute_indicators(bars[: t + 1])
    rows_full = [votes_at(full, i) if i >= WARMUP else {} for i in range(len(bars))]
    rows_cut = [votes_at(cut, i) if i >= WARMUP else {} for i in range(t + 1)]
    assert rows_full[t] == rows_cut[t]
    p_full = predict_at(rows_full, labels(full.closes, horizon), t, horizon)
    p_cut = predict_at(rows_cut, labels(cut.closes, horizon), t, horizon)
    assert p_full == pytest.approx(p_cut)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_walk_is_not_overclaimed(seed):
    fc = build_forecast(make_bars(random_walk(520, seed)), 5, "USD")
    assert fc is not None and fc.backtest is not None
    assert fc.backtest.models[0].predictions >= 20
    assert 0.3 <= fc.backtest.models[0].hit_rate <= 0.7
    assert fc.confidence == "low"


def test_predictable_cycle_is_detected():
    rng = random.Random(3)
    closes = [100 + 8 * math.sin(2 * math.pi * i / 40) + rng.gauss(0, 0.3) for i in range(520)]
    fc = build_forecast(make_bars(closes), 5, "USD")
    assert fc.backtest.models[0].hit_rate > 0.65
    assert fc.backtest.significant


def test_forecast_shape():
    bars = make_bars(random_walk(300, 11))
    fc = build_forecast(bars, 5, "USD")
    assert fc.as_of == bars[-1].date and fc.last_close == bars[-1].close
    assert fc.expected_low < fc.last_close < fc.expected_high
    assert len(fc.indicators) == len(FEATURES)
    assert all(-1 <= r.vote <= 1 for r in fc.indicators)
    assert len(fc.chart) == 180 and fc.chart[-1].date == bars[-1].date
    assert fc.direction in ("up", "down", "no_clear_edge")
    assert fc.confidence in ("low", "moderate") and fc.disclaimer


def test_forecast_facts_are_groundable():
    fc = build_forecast(make_bars(random_walk(300, 5)), 5, "USD")
    reg = FactRegistry()
    add_forecast_facts(reg, fc, "mock://h", dt.datetime(2026, 9, 29, 20, tzinfo=dt.timezone.utc))
    labels_ = [f.label for f in reg.all()]
    assert any(label.startswith("Statistical 5-day direction outlook") for label in labels_)
    prob = next(f for f in reg.all() if f.label.startswith("Model probability"))
    assert Grounder(reg).check(f"The model gives a {prob.display} chance of a higher close.", [prob.id]).ok


# --- pipeline -------------------------------------------------------------------------


def test_pipeline_includes_forecast_for_long_history(settings, clock, mock_provider):
    from agents.orchestrator import Orchestrator

    resp = Orchestrator(settings, provider=mock_provider, clock=clock).run(
        AnalyzeRequest(symbol="RWLK", question="Will it go up or down next week?"))
    assert resp.status == "ok"
    assert resp.forecast is not None and resp.forecast.confidence == "low"
    assert "directional outlook" in resp.report_markdown
    outlook = next(f for f in resp.computed_metrics if f.label.startswith("Statistical"))
    assert any(outlook.id in c.citations for c in resp.answer.claims)
    # recent-window metrics still use the requested window, not the full 2y history
    days = next(f for f in resp.computed_metrics if f.label == "Trading days in price window")
    assert days.value < 30


def test_short_history_flags_forecast_unavailable(settings, clock, mock_provider):
    from agents.orchestrator import Orchestrator

    resp = Orchestrator(settings, provider=mock_provider, clock=clock).run(
        AnalyzeRequest(symbol="AAPL", question="Will it go up?"))
    assert resp.forecast is None
    assert "FORECAST_UNAVAILABLE" in {f.code for f in resp.flags}
