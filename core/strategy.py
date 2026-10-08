"""Option strategy analysis: payoff at expiry and today, max profit / loss, breakevens, probability of profit.

All amounts are in rupees for the whole position (legs carry quantities in units, lots x lot size).
A long leg pays its premium; a short leg receives it. Payoff at expiry is piecewise linear in the
underlying price, kinked only at strikes, so extremes and breakevens can be found exactly from the
strikes plus the slopes beyond them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core import options_math as om


@dataclass(frozen=True)
class Leg:
    kind: str        # CE / PE
    side: str        # BUY / SELL
    strike: float
    qty: int         # units
    premium: float   # per unit
    iv: float | None = None  # fraction, e.g. 0.13

    @property
    def sign(self) -> int:
        return 1 if self.side == "BUY" else -1


def intrinsic(leg: Leg, s: float) -> float:
    return max(0.0, s - leg.strike) if leg.kind == "CE" else max(0.0, leg.strike - s)


def payoff(legs: list[Leg], s: float) -> float:
    return sum(leg.sign * leg.qty * (intrinsic(leg, s) - leg.premium) for leg in legs)


def value_now(legs: list[Leg], s: float, t: float, vol: float, r: float = om.DEFAULT_RATE) -> float:
    return sum(leg.sign * leg.qty * (om.bs_price(leg.kind, s, leg.strike, t, leg.iv or vol, r) - leg.premium) for leg in legs)


def analyse(legs: list[Leg], spot: float, t: float, vol: float, r: float = om.DEFAULT_RATE, points: int = 161) -> dict:
    if not legs:
        raise ValueError("add at least one leg")
    strikes = sorted({leg.strike for leg in legs})
    up_slope = sum(leg.sign * leg.qty for leg in legs if leg.kind == "CE")       # payoff slope above the top strike
    knots = [0.0, *strikes]
    vals = [payoff(legs, k) for k in knots]
    top = strikes[-1]
    candidates = vals + ([payoff(legs, top)] if up_slope == 0 else [])
    max_profit = None if up_slope > 0 else max(candidates)
    max_loss = None if up_slope < 0 else min(candidates)
    # Breakevens: zero crossings on each linear segment, plus beyond the top strike.
    breakevens: list[float] = []
    for (a, va), (b, vb) in zip(zip(knots, vals), zip(knots[1:], vals[1:])):
        if va == 0:
            breakevens.append(a)
        if (va < 0 < vb) or (va > 0 > vb):
            breakevens.append(a + (b - a) * (-va) / (vb - va))
    if vals[-1] == 0 and top not in breakevens:
        breakevens.append(top)
    if up_slope != 0 and (vals[-1] < 0 < up_slope or vals[-1] > 0 > up_slope):
        breakevens.append(top - vals[-1] / up_slope)
    breakevens = sorted({round(b, 2) for b in breakevens if b > 0})
    # Curves over a sensible range around spot and the strikes.
    lo = max(0.01, min(spot, strikes[0]) * 0.85)
    hi = max(spot, top) * 1.15
    xs = sorted({lo + (hi - lo) * i / (points - 1) for i in range(points)} | set(strikes) | {spot})   # exact kinks at strikes
    curve = [{"s": round(x, 2), "expiry": round(payoff(legs, x), 2), "today": round(value_now(legs, x, t, vol, r), 2)} for x in xs]
    net = sum(-leg.sign * leg.qty * leg.premium for leg in legs)                     # + credit received, - debit paid
    greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in legs:
        g = om.greeks(leg.kind, spot, leg.strike, t, leg.iv or vol, r)
        for k in greeks:
            greeks[k] += leg.sign * leg.qty * g[k]
    return {"net_premium": round(net, 2), "max_profit": None if max_profit is None else round(max_profit, 2),
            "max_loss": None if max_loss is None else round(max_loss, 2), "breakevens": breakevens,
            "pop": probability_of_profit(legs, spot, t, vol, r), "greeks": {k: round(v, 2) for k, v in greeks.items()},
            "curve": curve, "payoff_now": round(value_now(legs, spot, t, vol, r), 2)}


def probability_of_profit(legs: list[Leg], spot: float, t: float, vol: float, r: float = om.DEFAULT_RATE, steps: int = 2000) -> float | None:
    """Chance the position ends above zero at expiry if the underlying is lognormal with volatility ``vol``."""
    if vol <= 0 or t <= 0:
        return None
    sd = vol * math.sqrt(t)
    mu = math.log(spot) + (r - vol * vol / 2) * t
    total = prob = 0.0
    for i in range(steps):                     # integrate over +-5 standard deviations of log price
        z = -5 + 10 * (i + 0.5) / steps
        w = math.exp(-0.5 * z * z)
        total += w
        if payoff(legs, math.exp(mu + sd * z)) > 0:
            prob += w
    return round(prob / total * 100, 1)


def short_option_margin(legs: list[Leg], rate: float = 0.15) -> float:
    """The paper engine's rough margin: 15% of strike x quantity for each short leg (no hedge benefit)."""
    return round(sum(rate * leg.strike * leg.qty for leg in legs if leg.side == "SELL"), 2)
