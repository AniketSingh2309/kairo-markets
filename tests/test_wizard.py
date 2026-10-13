"""Option strategy wizard and IV statistics."""

import math

import pytest

from core import options_math as om
from core import wizard as wz


def chain(spot=25000.0, step=50, n=40, vol=0.14, T=7 / 365):
    rows = []
    for i in range(-n, n + 1):
        k = spot + i * step
        rows.append({"strike": k, "ce": {"ltp": round(max(0.05, om.bs_price("CE", spot, k, T, vol)), 2)},
                     "pe": {"ltp": round(max(0.05, om.bs_price("PE", spot, k, T, vol)), 2)}})
    return rows


T = 7 / 365


def test_bullish_view_ranks_defined_risk_first():
    items = wz.build("bullish", 25000, 25400, chain(), 75, T, 0.14)
    names = [c["name"] for c in items]
    assert set(names) >= {"Bull call spread", "Bull put spread", "Buy call", "Sell put"}
    assert items[-1]["unlimited_risk"] is False or names[-1] == "Sell put"
    sell_put = next(c for c in items if c["name"] == "Sell put")
    assert all(not c["unlimited_risk"] for c in items[: names.index("Sell put")])
    spread = next(c for c in items if c["name"] == "Bull call spread")
    assert spread["at_target"] > 0 and spread["max_loss"] < 0 and spread["return_on_risk"] > 0
    assert {l["strike"] for l in spread["legs"]} == {25000, 25400} and all(l["lots"] == 1 for l in spread["legs"])
    assert sell_put["max_loss"] is not None  # a short put's loss is large but bounded (price can't go below zero)


def test_bearish_and_neutral_and_volatile():
    bear = wz.build("bearish", 25000, 24600, chain(), 75, T, 0.14)
    assert next(c for c in bear if c["name"] == "Bear put spread")["at_target"] > 0
    neutral = wz.build("neutral", 25000, 25000, chain(), 75, T, 0.14)
    ic = next(c for c in neutral if c["name"] == "Iron condor")
    assert ic["net_premium"] > 0 and ic["max_loss"] is not None and len(ic["breakevens"]) == 2
    assert next(c for c in neutral if c["name"] == "Short straddle")["unlimited_risk"]
    vol = wz.build("volatile", 25000, 25000, chain(), 75, T, 0.14)
    assert [c["name"] for c in vol] == ["Long straddle", "Long strangle"] or set(c["name"] for c in vol) == {"Long straddle", "Long strangle"}
    with pytest.raises(ValueError):
        wz.build("sideways", 25000, 25000, chain(), 75, T, 0.14)


def test_spread_on_a_sparse_chain_uses_distinct_strikes():
    rows = [r for r in chain(step=500, n=3)]
    items = wz.build("bullish", 25000, 25050, rows, 75, T, 0.14)   # target closer than one strike
    spread = next(c for c in items if c["name"] == "Bull call spread")
    assert len({l["strike"] for l in spread["legs"]}) == 2


def test_iv_helpers():
    closes = [100 * math.exp(0.01 * ((-1) ** i)) for i in range(70)]
    hv = wz.realised_vol(closes, 20)
    assert hv is not None and 25 < hv < 35          # ~1% daily moves -> ~16% * sqrt(252)/... about 30% annualised
    assert wz.realised_vol(closes[:10], 20) is None
    assert wz.rank_and_percentile(15, [10, 20, 12, 18]) == (50.0, 50.0)
    assert "expensive" in wz.iv_verdict(30, 20, None) and "cheap" in wz.iv_verdict(12, 18, None) and "fairly" in wz.iv_verdict(15, 15, 50)


def test_naked_shorts_are_flagged_and_ranked_last():
    items = wz.build("bullish", 25000, 25400, chain(), 75, T, 0.14)
    assert next(c for c in items if c["name"] == "Sell put")["naked"] and items[-1]["name"] == "Sell put"
    assert not next(c for c in items if c["name"] == "Bull put spread")["naked"]
