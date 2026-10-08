"""Indian capital-gains estimate for realized trades (resident individual).

Rules encoded (Income-tax Act as amended by the Finance (No. 2) Act, 2024):

* Listed Indian equity (``.NS`` / ``.BO``), STT paid:
    - long-term if held > 12 months
    - STCG (sec 111A): 15% on transfers before 23-Jul-2024, 20% from 23-Jul-2024
    - LTCG (sec 112A): 10% before 23-Jul-2024, 12.5% from 23-Jul-2024, on gains above an
      annual exemption of Rs 1,00,000 (Rs 1,25,000 from FY 2024-25)
* Foreign equity (any other share): long-term if held > 24 months. LTCG 12.5% (transfers from
  23-Jul-2024); STCG is taxed at the slab rate, so no tax is estimated for it.
* Crypto / virtual digital assets (``-USD``, ``-INR`` ... Yahoo pairs): 30% flat (sec 115BBH);
  losses can't be set off or carried forward.
* Set-off inside a financial year: short-term losses against short- then long-term gains,
  long-term losses against long-term gains only. Highest-rate gains are offset first.
* 4% health & education cess on the tax. Surcharge, rebate u/s 87A, carry-forward of earlier
  years' losses and grandfathering (pre-1-Feb-2018 cost step-up) are NOT applied -- flagged.

Only INR-denominated gains get a rupee tax estimate; foreign-currency gains must be converted at
the SBI TT buying rate (Rule 115), which this app does not have, so they are reported natively.
This is an estimate to plan with, not tax advice.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Callable, Literal

from pydantic import BaseModel

from tools.models import is_mf_symbol

from core.portfolio import Book, OpenLot, RealizedLot

RATE_CHANGE = dt.date(2024, 7, 23)
GRANDFATHER_CUTOFF = dt.date(2018, 2, 1)
CESS = 0.04
Category = Literal["indian_equity", "foreign_equity", "vda", "equity_mf", "debt_mf", "other_mf"]
_CRYPTO_QUOTES = ("-USD", "-INR", "-EUR", "-GBP", "-USDT")
SPECIFIED_MF_FROM = dt.date(2023, 4, 1)   # sec 50AA: debt funds bought from this date -> slab rate
MfKind = Callable[[str], str | None]      # symbol -> "equity" | "debt" | "other" | None


def category_of(symbol: str, buy_date: dt.date | None = None, mf_kind: MfKind | None = None) -> Category:
    if is_mf_symbol(symbol):
        kind = (mf_kind(symbol) if mf_kind else None) or "other"
        if kind == "equity":
            return "equity_mf"
        if kind == "debt":
            # Debt funds bought before 1-Apr-2023 keep the older regime (24 months / 12.5%).
            return "debt_mf" if buy_date is None or buy_date >= SPECIFIED_MF_FROM else "other_mf"
        return "other_mf"
    if symbol.endswith((".NS", ".BO")):
        return "indian_equity"
    if symbol.endswith(_CRYPTO_QUOTES):
        return "vda"
    return "foreign_equity"


EQUITY_LIKE = ("indian_equity", "equity_mf")


def fy_of(day: dt.date) -> str:
    start = day.year if day.month >= 4 else day.year - 1
    return f"FY {start}-{str(start + 1)[-2:]}"


def fy_bounds(fy: str) -> tuple[dt.date, dt.date]:
    start = int(fy.split()[1][:4])
    return dt.date(start, 4, 1), dt.date(start + 1, 3, 31)


def ltcg_exemption(fy: str) -> float:
    return 125_000.0 if int(fy.split()[1][:4]) >= 2024 else 100_000.0


def long_term_months(category: Category) -> int | None:
    return {"indian_equity": 12, "equity_mf": 12, "foreign_equity": 24, "other_mf": 24, "vda": None, "debt_mf": None}[category]


def add_months(day: dt.date, months: int) -> dt.date:
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    for d in (day.day, 30, 29, 28):  # clamp e.g. 31-Jan + 1 month -> 28/29-Feb
        try:
            return dt.date(year, month, d)
        except ValueError:
            continue
    raise ValueError(day)


def is_long_term(category: Category, buy: dt.date, sell: dt.date) -> bool:
    """'Held for more than N months': the sale must fall after the N-month anniversary."""
    months = long_term_months(category)
    return months is not None and sell > add_months(buy, months)


def equity_rates(sale: dt.date) -> tuple[float, float]:
    """(STCG 111A, LTCG 112A) rates for a listed Indian share sold on ``sale``."""
    return (0.15, 0.10) if sale < RATE_CHANGE else (0.20, 0.125)


class TaxLine(BaseModel):
    symbol: str
    currency: str | None
    category: Category
    term: Literal["short", "long", "vda"]
    quantity: float
    buy_date: dt.date
    sell_date: dt.date
    holding_days: int
    cost: float
    proceeds: float
    gain: float
    rate: float | None  # None = slab rate / not estimated
    grandfathering_possible: bool = False


class RateBucket(BaseModel):
    rate: float
    gains: float
    after_setoff: float
    exempt: float = 0.0
    taxable: float
    tax: float


class IndianSummary(BaseModel):
    stcg: float
    stcl: float
    ltcg: float
    ltcl: float
    net_stcg: float
    net_ltcg: float
    exemption_limit: float
    exemption_used: float
    st_buckets: list[RateBucket]
    lt_buckets: list[RateBucket]
    tax: float
    cess: float
    total_tax: float


class VdaSummary(BaseModel):
    currency: str
    gains: float
    losses_ignored: float
    tax: float | None
    total_tax: float | None


class MfSummary(BaseModel):
    """Non-equity mutual funds (equity-oriented funds are taxed with shares in ``indian``)."""

    debt_slab_gains: float          # sec 50AA debt funds: add to income, taxed at slab
    other_short_gains: float        # other funds held <= 24 months: slab
    other_long_gains: float         # other funds held > 24 months
    other_long_tax: float | None    # 12.5% + cess on net long-term gains (sales from 23-Jul-2024)
    note: str


class ForeignSummary(BaseModel):
    currency: str
    stcg: float
    ltcg: float
    note: str


class FYReport(BaseModel):
    fy: str
    lines: list[TaxLine]
    indian: IndianSummary | None
    vda: list[VdaSummary]
    foreign: list[ForeignSummary]
    mutual_funds: MfSummary | None = None
    estimated_total_tax_inr: float
    notes: list[str]


class HarvestIdea(BaseModel):
    symbol: str
    quantity: float
    unrealized_gain: float
    note: str


class LtcgPending(BaseModel):
    symbol: str
    quantity: float
    turns_long_term_on: dt.date
    days_left: int
    unrealized_gain: float


class TaxPlanning(BaseModel):
    fy: str
    exemption_limit: float
    ltcg_realized_this_fy: float
    exemption_remaining: float
    harvest_ideas: list[HarvestIdea]
    turning_long_term_soon: list[LtcgPending]


def classify(lot: RealizedLot, mf_kind: MfKind | None = None) -> TaxLine:
    cat = category_of(lot.symbol, lot.buy_date, mf_kind)
    if cat == "vda":
        term, rate = "vda", 0.30
    elif cat == "debt_mf":
        term, rate = "short", None  # deemed short-term whatever the holding period: slab rate
    else:
        term = "long" if is_long_term(cat, lot.buy_date, lot.sell_date) else "short"
        if cat in EQUITY_LIKE:
            st, lt = equity_rates(lot.sell_date)
            rate = lt if term == "long" else st
        else:
            rate = 0.125 if term == "long" and lot.sell_date >= RATE_CHANGE else None
    return TaxLine(
        symbol=lot.symbol, currency=lot.currency, category=cat, term=term, quantity=lot.quantity,  # type: ignore[arg-type]
        buy_date=lot.buy_date, sell_date=lot.sell_date, holding_days=lot.holding_days, cost=lot.cost,
        proceeds=lot.proceeds, gain=lot.gain, rate=rate,
        grandfathering_possible=cat in EQUITY_LIKE and term == "long" and lot.buy_date < GRANDFATHER_CUTOFF)


def _offset(buckets: dict[float, float], loss: float) -> float:
    """Absorb ``loss`` from the highest-rate buckets first; returns the unabsorbed loss."""
    for rate in sorted(buckets, reverse=True):
        take = min(buckets[rate], loss)
        buckets[rate] -= take
        loss -= take
    return loss


def _indian_summary(lines: list[TaxLine], fy: str) -> IndianSummary | None:
    inr = [l for l in lines if l.category in EQUITY_LIKE]
    if not inr:
        return None
    st_gain: dict[float, float] = defaultdict(float)
    lt_gain: dict[float, float] = defaultdict(float)
    stcl = ltcl = 0.0
    for l in inr:
        if l.gain >= 0:
            (lt_gain if l.term == "long" else st_gain)[l.rate or 0.0] += l.gain
        elif l.term == "long":
            ltcl += -l.gain
        else:
            stcl += -l.gain
    st_raw, lt_raw = dict(st_gain), dict(lt_gain)
    left = _offset(st_gain, stcl)       # STCL -> STCG first
    _offset(lt_gain, left + ltcl)       # remaining STCL and all LTCL -> LTCG
    limit = ltcg_exemption(fy)
    exempt_left = limit
    lt_exempt: dict[float, float] = {}
    for rate in sorted(lt_gain, reverse=True):  # exemption against the highest-rate LTCG first
        use = min(lt_gain[rate], exempt_left)
        lt_exempt[rate] = use
        exempt_left -= use

    def buckets(raw, net, exempt) -> list[RateBucket]:
        out = []
        for rate in sorted(raw):
            taxable = max(0.0, net.get(rate, 0.0) - exempt.get(rate, 0.0))
            out.append(RateBucket(rate=rate, gains=raw[rate], after_setoff=net.get(rate, 0.0),
                                  exempt=exempt.get(rate, 0.0), taxable=taxable, tax=taxable * rate))
        return out

    stb, ltb = buckets(st_raw, st_gain, {}), buckets(lt_raw, lt_gain, lt_exempt)
    tax = sum(b.tax for b in stb + ltb)
    return IndianSummary(
        stcg=sum(st_raw.values()), stcl=stcl, ltcg=sum(lt_raw.values()), ltcl=ltcl,
        net_stcg=sum(st_gain.values()), net_ltcg=sum(lt_gain.values()), exemption_limit=limit,
        exemption_used=limit - exempt_left, st_buckets=stb, lt_buckets=ltb,
        tax=tax, cess=tax * CESS, total_tax=tax * (1 + CESS))


def fy_report(book: Book, fy: str, mf_kind: MfKind | None = None) -> FYReport:
    start, end = fy_bounds(fy)
    lines = [classify(r, mf_kind) for r in book.realized if start <= r.sell_date <= end]
    indian = _indian_summary(lines, fy)

    vda: list[VdaSummary] = []
    for cur in sorted({l.currency or "?" for l in lines if l.category == "vda"}):
        ls = [l for l in lines if l.category == "vda" and (l.currency or "?") == cur]
        gains = sum(l.gain for l in ls if l.gain > 0)
        tax = gains * 0.30 if cur == "INR" else None
        vda.append(VdaSummary(currency=cur, gains=gains, losses_ignored=-sum(l.gain for l in ls if l.gain < 0),
                              tax=tax, total_tax=tax * (1 + CESS) if tax is not None else None))

    foreign: list[ForeignSummary] = []
    for cur in sorted({l.currency or "?" for l in lines if l.category == "foreign_equity"}):
        ls = [l for l in lines if l.category == "foreign_equity" and (l.currency or "?") == cur]
        foreign.append(ForeignSummary(
            currency=cur, stcg=sum(l.gain for l in ls if l.term == "short"),
            ltcg=sum(l.gain for l in ls if l.term == "long"),
            note="Convert each sale at the SBI TT buying rate (Rule 115) before filing. STCG is taxed at "
                 "your slab rate; LTCG (held > 24 months) at 12.5%."))

    funds = None
    debt = [l for l in lines if l.category == "debt_mf"]
    other = [l for l in lines if l.category == "other_mf"]
    if debt or other:
        long_net = sum(l.gain for l in other if l.term == "long")
        taxable = [l for l in other if l.term == "long" and l.rate]
        long_tax = max(0.0, sum(l.gain for l in taxable)) * 0.125 * (1 + CESS) if taxable else None
        funds = MfSummary(
            debt_slab_gains=sum(l.gain for l in debt), other_short_gains=sum(l.gain for l in other if l.term == "short"),
            other_long_gains=long_net, other_long_tax=long_tax,
            note="Debt funds bought on or after 1-Apr-2023 are taxed at your slab rate whatever the holding period "
                 "(sec 50AA). Other non-equity funds (hybrid under 65% equity, international, gold, FoF) are long-term "
                 "after 24 months at 12.5%, otherwise slab. Fund type is inferred from the AMFI category - verify.")

    notes = [
        "Estimate for a resident individual; not tax advice. Verify against your broker's capital-gains "
        "statement and the current Finance Act before filing.",
        "Surcharge, the sec 87A rebate and losses brought forward from earlier years are not applied.",
    ]
    if any(l.grandfathering_possible for l in lines):
        notes.append("Some long-term lots were bought before 1-Feb-2018: grandfathering (cost = higher of "
                     "actual cost and 31-Jan-2018 FMV) could lower the gain; it is not applied here.")
    if vda:
        notes.append("Crypto: 30% flat on each gain; losses cannot be set off against any income. 1% TDS "
                     "deducted on sales can be claimed as credit.")
    total = ((indian.total_tax if indian else 0.0) + sum(v.total_tax or 0.0 for v in vda)
             + ((funds.other_long_tax or 0.0) if funds else 0.0))
    return FYReport(fy=fy, lines=lines, indian=indian, vda=vda, foreign=foreign, mutual_funds=funds,
                    estimated_total_tax_inr=total, notes=notes)


def available_fys(book: Book, today: dt.date) -> list[str]:
    fys = {fy_of(r.sell_date) for r in book.realized} | {fy_of(today)}
    return sorted(fys, reverse=True)


def tax_planning(book: Book, prices: dict[str, float], today: dt.date, mf_kind: MfKind | None = None) -> TaxPlanning:
    """LTCG-exemption headroom this FY and lots about to turn long-term (Indian equity & equity funds)."""
    fy = fy_of(today)
    report = fy_report(book, fy, mf_kind)
    limit = ltcg_exemption(fy)
    realized_ltcg = report.indian.net_ltcg if report.indian else 0.0
    remaining = max(0.0, limit - realized_ltcg)
    ideas: list[HarvestIdea] = []
    soon: list[LtcgPending] = []
    for sym, b in book.symbols.items():
        if category_of(sym, None, mf_kind) not in EQUITY_LIKE or sym not in prices:
            continue
        price = prices[sym]
        long_lots: list[OpenLot] = [l for l in b.lots if is_long_term("indian_equity", l.date, today)]
        gain = sum(l.quantity * (price - l.unit_cost) for l in long_lots)
        if long_lots and gain > 0:
            qty = sum(l.quantity for l in long_lots)
            ideas.append(HarvestIdea(symbol=sym, quantity=qty, unrealized_gain=gain, note=(
                f"{qty:g} long-term shares carry {gain:,.0f} of unrealized gain")))
        for l in b.lots:
            turn = add_months(l.date, 12) + dt.timedelta(days=1)  # first day it counts as long-term
            if 0 < (turn - today).days <= 30:
                soon.append(LtcgPending(symbol=sym, quantity=l.quantity, turns_long_term_on=turn,
                                        days_left=(turn - today).days,
                                        unrealized_gain=l.quantity * (price - l.unit_cost)))
    ideas.sort(key=lambda i: -i.unrealized_gain)
    soon.sort(key=lambda s: s.days_left)
    return TaxPlanning(fy=fy, exemption_limit=limit, ltcg_realized_this_fy=realized_ltcg,
                       exemption_remaining=remaining, harvest_ideas=ideas, turning_long_term_soon=soon)
