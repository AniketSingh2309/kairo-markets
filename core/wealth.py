"""Wealth tools for Indian investors: tax harvesting plans and the cost of Regular mutual-fund plans.

Tax harvesting (like Kuvera's):

* **Gains**: long-term gains on listed equity and equity funds are tax-free up to ₹1.25 lakh a year. Selling
  enough long-term units to book gains up to the unused exemption, then buying them straight back, raises the
  cost basis for free: less tax when you finally sell. Sales are FIFO, so only the oldest lots, and only while
  they are long-term, are used.
* **Losses**: unrealised losses can be booked to offset gains already realised this year (short-term losses
  offset any gain; long-term losses only long-term gains).

Regular vs Direct: the same fund's Regular plan carries a distributor commission in its expense ratio. The
gap shows up as a lower return; comparing both plans' actual NAV histories measures it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from core.tax_india import EQUITY_LIKE, category_of, equity_rates, is_long_term

NavSeries = Sequence[tuple[dt.date, float]]


def harvest_plan(book: Any, prices: dict[str, float], today: dt.date, exemption_remaining: float,
                 realised_stcg: float, realised_ltcg_taxable: float, mf_kind=None) -> dict[str, Any]:
    st_rate, lt_rate = equity_rates(today)
    gains, losses = [], []
    room = max(0.0, exemption_remaining)
    for sym, b in sorted(book.symbols.items()):
        if category_of(sym, None, mf_kind) not in EQUITY_LIKE or sym not in prices or not b.lots:
            continue
        price = prices[sym]
        # gains: FIFO from the oldest lot, while lots are long-term and in profit, within the remaining exemption
        qty = gain = 0.0
        for lot in b.lots:
            per = price - lot.unit_cost
            if room - gain <= 1 or not is_long_term("indian_equity", lot.date, today) or per <= 0:
                break
            take = min(lot.quantity, (room - gain) / per)
            if take < lot.quantity:
                take = float(int(take)) if not sym.startswith("MF") else round(take, 3)  # whole shares; fund units to 3 places
            if take <= 0:
                break
            qty += take; gain += take * per
            if take < lot.quantity:
                break
        if qty > 0 and gain > 0:
            gains.append({"symbol": sym, "quantity": round(qty, 4), "price": price, "gain": round(gain, 2),
                          "future_tax_saved": round(gain * lt_rate, 2)})
            room -= gain
        # losses: FIFO lots currently under water (what a sale would actually realise first)
        first = b.lots[0]
        if price < first.unit_cost:
            lqty = lloss = 0.0
            st_part = 0.0
            for lot in b.lots:
                if price >= lot.unit_cost:
                    break
                lqty += lot.quantity
                loss = lot.quantity * (lot.unit_cost - price)
                lloss += loss
                if not is_long_term("indian_equity", lot.date, today):
                    st_part += loss
            losses.append({"symbol": sym, "quantity": round(lqty, 4), "price": price, "loss": round(lloss, 2), "short_term_loss": round(st_part, 2)})
    gains.sort(key=lambda g: -g["gain"])
    losses.sort(key=lambda x: -x["loss"])
    # what booking the losses would save, offsetting this year's taxable gains (ST losses offset either kind)
    st_avail = sum(x["short_term_loss"] for x in losses)
    lt_avail = sum(x["loss"] - x["short_term_loss"] for x in losses)
    off_st = min(st_avail, realised_stcg)
    off_lt = min(lt_avail + (st_avail - off_st), realised_ltcg_taxable)
    return {"exemption_remaining": round(exemption_remaining, 2), "gains": gains, "losses": losses if (realised_stcg > 0 or realised_ltcg_taxable > 0) else [],
            "gain_booked": round(sum(g["gain"] for g in gains), 2), "future_tax_saved": round(sum(g["future_tax_saved"] for g in gains), 2),
            "loss_tax_saved": round(off_st * st_rate + off_lt * lt_rate, 2), "rates": {"stcg": st_rate, "ltcg": lt_rate},
            "note": ("Sell the units and buy them back the same day, at about the same price, to reset the cost. Sales are FIFO, so the oldest units go first. "
                     "India has no explicit wash-sale rule, but tax authorities can question artificial transactions; for large amounts, check with a tax adviser. "
                     "Brokerage, STT and exit loads (for funds) reduce the benefit.")}


def plan_sibling(scheme: Any, schemes: Sequence[Any], plan: str = "Direct") -> Any | None:
    """The same fund in the other plan (same name and option)."""
    return next((x for x in schemes if x.name == scheme.name and x.code != scheme.code and (x.plan or "") == plan
                 and (x.option or "") == (scheme.option or "")), None)


def _nav_on(series: NavSeries, day: dt.date) -> float | None:
    for d, n in series:
        if d >= day:
            return n
    return None


def annual_gap(regular: NavSeries, direct: NavSeries, years: float = 3.0) -> dict[str, Any] | None:
    """Direct minus Regular CAGR (percentage points a year) over the same window, up to ``years`` long."""
    if not regular or not direct:
        return None
    end = min(regular[-1][0], direct[-1][0])
    start = max(end - dt.timedelta(days=round(365.25 * years)), regular[0][0], direct[0][0])
    span = (end - start).days / 365.25
    if span < 1:
        return None
    r0, r1, d0, d1 = _nav_on(regular, start), _nav_on(regular, end), _nav_on(direct, start), _nav_on(direct, end)
    if not all((r0, r1, d0, d1)):
        return None
    reg = (r1 / r0) ** (1 / span) - 1
    dirc = (d1 / d0) ** (1 / span) - 1
    return {"regular_cagr": round(reg * 100, 2), "direct_cagr": round(dirc * 100, 2), "gap": round((dirc - reg) * 100, 2), "years": round(span, 1)}


def switch_value(value: float, gap_pct: float, held_years: float, horizon: int = 10, growth_pct: float = 12.0) -> dict[str, float]:
    """Rough rupee cost of staying in Regular: already lost while held, and over the next ``horizon`` years."""
    g, r = gap_pct / 100, growth_pct / 100
    past = value * ((1 + g / (1 + r - g)) ** held_years - 1) if held_years > 0 else 0.0
    future = value * ((1 + r) ** horizon - (1 + r - g) ** horizon)
    return {"lost_so_far": round(max(0.0, past), 2), "next_years_cost": round(max(0.0, future), 2), "horizon": horizon, "growth_assumed": growth_pct}
