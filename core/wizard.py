"""Option strategy wizard (like Sensibull's): from a view ("NIFTY to 25,400 by expiry") to ranked strategies.

For each view a handful of textbook strategies is built from the live chain around the spot and the target,
priced at the last traded premiums, and analysed with ``core.strategy`` (max profit / loss, breakevens,
chance of profit). They are ranked by what they make if the target is hit, per rupee they can lose;
strategies with unlimited risk, or naked short options, are listed last and flagged. Plus IV statistics: IV against realised
volatility, and IV rank / percentile from a history of readings.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from typing import Any

from core import strategy as st

VIEWS = ("bullish", "bearish", "neutral", "volatile")


def _priced(rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    key = "ce" if kind == "CE" else "pe"
    return [r for r in rows if (r.get(key) or {}).get("ltp")]


def nearest(rows: list[dict[str, Any]], price: float, kind: str, exclude: float | None = None) -> float | None:
    """The traded strike closest to ``price`` for calls or puts."""
    cands = [r["strike"] for r in _priced(rows, kind) if r["strike"] != exclude]
    return min(cands, key=lambda k: abs(k - price)) if cands else None


def _templates(view: str, spot: float, target: float, move: float) -> list[tuple[str, list[tuple[str, str, float]], str]]:
    """(name, [(side, kind, ~strike price)], what it is) for a view; strikes are snapped to the chain later."""
    d = abs(target - spot)
    width = max(d, 0.5 * move)
    if view == "bullish":
        return [("Bull call spread", [("BUY", "CE", spot), ("SELL", "CE", spot + width)], "Buy a call, sell a higher one: cheaper than a call alone, profit capped at the higher strike."),
                ("Bull put spread", [("SELL", "PE", spot), ("BUY", "PE", spot - width)], "Sell a put, buy a lower one: collect premium, keep it if the price stays above the sold strike."),
                ("Buy call", [("BUY", "CE", spot)], "Pay a premium for unlimited upside; the most you can lose is the premium."),
                ("Buy OTM call", [("BUY", "CE", spot + width / 2)], "A cheaper, out-of-the-money call: needs a bigger move, pays more if it comes."),
                ("Sell put", [("SELL", "PE", spot - width / 2)], "Collect premium betting the price won't fall far. Large losses if it drops sharply.")]
    if view == "bearish":
        return [("Bear put spread", [("BUY", "PE", spot), ("SELL", "PE", spot - width)], "Buy a put, sell a lower one: cheaper than a put alone, profit capped at the lower strike."),
                ("Bear call spread", [("SELL", "CE", spot), ("BUY", "CE", spot + width)], "Sell a call, buy a higher one: collect premium, keep it if the price stays below the sold strike."),
                ("Buy put", [("BUY", "PE", spot)], "Pay a premium to profit from a fall; the most you can lose is the premium."),
                ("Buy OTM put", [("BUY", "PE", spot - width / 2)], "A cheaper, out-of-the-money put: needs a bigger fall, pays more if it comes."),
                ("Sell call", [("SELL", "CE", spot + width / 2)], "Collect premium betting the price won't rise far. Unlimited loss if it rallies.")]
    if view == "neutral":
        m = max(move * 0.8, d)
        return [("Iron condor", [("BUY", "PE", spot - 2 * m), ("SELL", "PE", spot - m), ("SELL", "CE", spot + m), ("BUY", "CE", spot + 2 * m)],
                 "Sell a strangle and buy wings: profits if the price stays in the range, with capped losses."),
                ("Iron butterfly", [("BUY", "PE", spot - 1.5 * m), ("SELL", "PE", spot), ("SELL", "CE", spot), ("BUY", "CE", spot + 1.5 * m)],
                 "Sell the at-the-money straddle with protective wings: bigger credit, narrower profit zone."),
                ("Short strangle", [("SELL", "PE", spot - m), ("SELL", "CE", spot + m)], "Sell an out-of-the-money put and call. Wide profit zone, unlimited risk on a big move."),
                ("Short straddle", [("SELL", "PE", spot), ("SELL", "CE", spot)], "Sell the at-the-money put and call: most premium, unlimited risk.")]
    m = max(move * 0.6, d)
    return [("Long straddle", [("BUY", "PE", spot), ("BUY", "CE", spot)], "Buy the at-the-money put and call: profits from a big move either way; loses if the price sits still."),
            ("Long strangle", [("BUY", "PE", spot - m), ("BUY", "CE", spot + m)], "Cheaper than a straddle; needs an even bigger move.")]


def build(view: str, spot: float, target: float, rows: list[dict[str, Any]], lot: int, T: float, vol: float,
          ivs: dict[tuple[float, str], float | None] | None = None) -> list[dict[str, Any]]:
    """Ranked candidate strategies for a view. ``rows``: chain rows (strike, ce/pe with ltp)."""
    if view not in VIEWS:
        raise ValueError(f"view must be one of {', '.join(VIEWS)}")
    move = spot * vol * math.sqrt(max(T, 1 / 365))
    by_strike = {r["strike"]: r for r in rows}
    out, seen = [], set()
    for name, spec, about in _templates(view, spot, target, move):
        legs = []
        for side, kind, px in spec:
            k = nearest(rows, px, kind)
            if k is None:
                break
            legs.append((side, kind, k))
        if len(legs) != len(spec):
            continue
        # a spread whose two legs snapped to the same strike: move the second to the next strike out
        if len(legs) == 2 and legs[0][1] == legs[1][1] and legs[0][2] == legs[1][2]:
            outward = spec[1][2] > spec[0][2]
            alt = sorted((r["strike"] for r in _priced(rows, legs[1][1]) if (r["strike"] > legs[0][2]) == outward and r["strike"] != legs[0][2]),
                         key=lambda k: abs(k - legs[0][2]))
            if not alt:
                continue
            legs[1] = (legs[1][0], legs[1][1], alt[0])
        if len(set(legs)) != len(legs) or (len(legs) == 4 and len({k for _, _, k in legs}) < 3):
            continue  # a sparse chain collapsed the wings onto the body
        sig = tuple(sorted(legs))
        if sig in seen:
            continue
        seen.add(sig)
        objs = []
        for side, kind, k in legs:
            side_data = by_strike[k]["ce" if kind == "CE" else "pe"]
            iv = (ivs or {}).get((k, kind))
            objs.append(st.Leg(kind, side, k, lot, side_data["ltp"], iv))
        try:
            a = st.analyse(objs, spot, T, vol, points=41)
        except ValueError:
            continue
        at_target = st.payoff(objs, target)
        unlimited = a["max_loss"] is None
        # more options sold than bought of a kind: a naked short, whose loss can be many times the premium
        naked = any(sum((1 if s == "BUY" else -1) for s, kd, _ in legs if kd == kind) < 0 for kind in ("CE", "PE"))
        risk = None if unlimited else abs(min(a["max_loss"], 0)) or None
        out.append({"name": name, "about": about, "legs": [{"side": s, "kind": kd, "strike": k, "lots": 1, "premium": by_strike[k]["ce" if kd == "CE" else "pe"]["ltp"]} for s, kd, k in legs],
                    "net_premium": a["net_premium"], "max_profit": a["max_profit"], "max_loss": a["max_loss"], "breakevens": a["breakevens"], "pop": a["pop"],
                    "at_target": round(at_target, 2), "unlimited_risk": unlimited, "naked": naked, "margin": st.short_option_margin(objs),
                    "return_on_risk": round(at_target / risk, 2) if risk else None})
    out.sort(key=lambda c: (c["unlimited_risk"] or c["naked"], -(c["return_on_risk"] if c["return_on_risk"] is not None else -1e9), -(c["pop"] or 0)))
    return out


# ------------------------------------------------------------------------------------------ IV statistics


def realised_vol(closes: Sequence[float], n: int) -> float | None:
    """Annualised close-to-close volatility over the last ``n`` sessions, in %."""
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]
    return round(statistics.stdev(rets) * math.sqrt(252) * 100, 2) if len(rets) >= 5 else None


def rank_and_percentile(current: float, history: Sequence[float]) -> tuple[float | None, float | None]:
    """IV rank (where today sits between the period's low and high) and percentile (share of days below today), 0-100."""
    h = [x for x in history if x is not None]
    if len(h) < 2:
        return None, None
    lo, hi = min(h), max(h)
    rank = round((current - lo) / (hi - lo) * 100, 1) if hi > lo else None
    pct = round(sum(1 for x in h if x < current) / len(h) * 100, 1)
    return rank, pct


def iv_verdict(iv: float | None, hv: float | None, rank: float | None) -> str:
    if iv is None:
        return "No implied-volatility reading for this chain right now."
    ratio = iv / hv if hv else None
    pricey = (ratio is not None and ratio > 1.25) or (rank is not None and rank > 70)
    cheap = (ratio is not None and ratio < 0.85) or (rank is not None and rank < 25)
    if pricey and not cheap:
        return "Options look expensive (implied volatility is high against recent moves or its own history). Selling premium with defined risk (spreads, iron condors) has the odds on its side; buyers need a big move."
    if cheap and not pricey:
        return "Options look cheap (implied volatility is low against recent moves or its own history). Buying options or debit spreads costs less than usual; premium sellers get paid little for the risk."
    return "Options look fairly priced: implied volatility is close to how much the price has actually been moving."
