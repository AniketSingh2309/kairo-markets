"""Mutual-fund analytics: classification, returns, risk, SIP back-tests and category ranks.

Everything is computed from NAV series (date, NAV) -- the only data AMFI publishes for free.
Expense ratio, AUM and portfolio holdings are NOT available from free official sources, so they
are never shown rather than estimated.
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from core.finance import xirr

NavSeries = Sequence[tuple[dt.date, float]]
TaxKind = Literal["equity", "debt", "other"]

# ----------------------------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------------------------

_DEBTLIKE = re.compile(r"gilt|bond|sdl|g-?sec|debt|liquid|money market|overnight|treasury|t-?bill|target maturity|"
                       r"bharat bond|crisil.*(ibx|index)|psu|corporate|banking|duration|income|floater|credit", re.I)
_FOREIGN = re.compile(r"nasdaq|s&p ?500|\bus\b|u\.s\.|global|international|world|overseas|hang seng|msci|fang|"
                      r"china|japan|taiwan|europe|emerging", re.I)
_COMMODITY = re.compile(r"\bgold\b|\bsilver\b|commodit", re.I)
_EQUITY_HYBRID = re.compile(r"aggressive hybrid|arbitrage|equity savings|balanced advantage|dynamic asset", re.I)


_HEADS = [  # (prefix of AMFI's section heading, canonical heading)
    ("equity scheme", "Equity Scheme"), ("hybrid scheme", "Hybrid Scheme"), ("debt scheme", "Debt Scheme"),
    ("income/debt oriented", "Debt Scheme"), ("solution oriented", "Solution Oriented Scheme"),
    ("children", "Solution Oriented Scheme"), ("life cycle", "Solution Oriented Scheme"),
    ("index funds", "Other Scheme"), ("exchange traded funds", "Other Scheme"), ("fund of funds scheme", "Other Scheme"),
    ("overseas fund of funds", "Other Scheme"), ("other scheme", "Other Scheme"),
]
_SUBS = {  # AMFI publishes both the current SEBI names and older ones; map onto the current names.
    "elss": "ELSS (Tax Saver)", "elss- tax saver fund": "ELSS (Tax Saver)",
    "sectoral/ thematic": "Sectoral/Thematic", "sectoral fund": "Sectoral/Thematic", "thematic fund": "Sectoral/Thematic",
    "dynamic asset allocation or balanced advantage": "Balanced Advantage / Dynamic Asset Allocation",
    "balanced advantage fund/ dynamic asset allocation": "Balanced Advantage / Dynamic Asset Allocation",
    "equity savings fund": "Equity Savings", "multi asset allocation fund": "Multi Asset Allocation",
    "banking and psu debt fund": "Banking and PSU Fund", "dynamic term fund": "Dynamic Bond",
    "short term fund": "Short Duration Fund", "ultra short term fund": "Ultra Short Duration Fund",
    "ultra short to short term fund": "Low Duration Fund", "medium term fund": "Medium Duration Fund",
    "medium to long term fund": "Medium to Long Duration Fund", "long term fund": "Long Duration Fund",
    "floating interest rates fund": "Floater Fund", "10-year constant maturity gilt fund": "Gilt Fund with 10 year constant duration",
    "childrens' fund": "Children's Fund", "children’s fund": "Children's Fund", "retirement fund": "Retirement Fund",
    "fund of funds scheme (domestic)": "FoF Domestic", "fof domestic": "FoF Domestic",
    "fund of funds investing overseas": "FoF Overseas", "fof overseas": "FoF Overseas",
    "gold etf": "Gold ETF", "silver etf": "Silver ETF", "other etfs": "Other ETF", "other etf": "Other ETF",
    "equity etf": "Equity ETF", "debt etf": "Debt ETF",
}


def canonical_category(raw: str | None, name: str = "") -> str | None:
    """Normalise AMFI's two category naming systems into one, and split mixed index-fund buckets by
    what they hold, so ranking compares like with like (e.g. "Equity Schemes - ELSS- Tax Saver Fund"
    and "Equity Scheme - ELSS" both become "Equity Scheme - ELSS (Tax Saver)")."""
    if not raw:
        return raw
    head, _, sub = raw.partition(" - ")
    head_l = re.sub(r"\s+", " ", head).strip().lower()
    canon_head = next((c for p, c in _HEADS if head_l.startswith(p)), head.strip())
    sub_clean = re.sub(r"\s+", " ", sub).strip()
    sub_l = sub_clean.lower()
    if head_l.startswith("life cycle"):
        canon_sub = "Life Cycle Fund"
    elif head_l.startswith("children"):
        canon_sub = "Children's Fund"
    elif head_l.startswith("index funds") or sub_l == "index funds":
        kind = "debt" if (head_l.startswith("index funds") and "debt" in sub_l) else None
        if kind is None:
            kind = "debt" if _DEBTLIKE.search(name) else "other" if (_FOREIGN.search(name) or _COMMODITY.search(name)) else "equity"
        canon_sub = {"equity": "Index Funds (Equity)", "debt": "Index Funds (Debt)", "other": "Index Funds (International & other)"}[kind]
        if head_l.startswith("index funds") and "hybrid" in sub_l:
            canon_sub = "Index Funds (International & other)"
    elif not sub_l and head_l in _SUBS:  # e.g. "Fund of Funds Scheme (Domestic)" with no " - " part
        canon_sub = _SUBS[head_l]
    else:
        canon_sub = _SUBS.get(sub_l, sub_clean)
    return f"{canon_head} - {canon_sub}" if canon_sub else canon_head


class FundClass(BaseModel):
    asset_class: str          # Equity / Debt / Hybrid / Index & ETF / FoF / Solution / Other
    sub_category: str         # e.g. "Flexi Cap Fund"
    tax_kind: TaxKind         # how capital gains are taxed in India
    tax_inferred: bool        # True when the tax kind is a best guess from the fund's name


def classify(category: str | None, name: str) -> FundClass:
    cat = category or ""
    head, _, sub = cat.partition(" - ")
    head_l, sub_l = head.lower(), sub.lower()
    if head_l.startswith("equity"):
        return FundClass(asset_class="Equity", sub_category=sub or "Equity", tax_kind="equity", tax_inferred=False)
    if head_l.startswith(("debt", "income")):
        return FundClass(asset_class="Debt", sub_category=sub or "Debt", tax_kind="debt", tax_inferred=False)
    if head_l.startswith("hybrid"):
        if _EQUITY_HYBRID.search(sub):
            return FundClass(asset_class="Hybrid", sub_category=sub, tax_kind="equity", tax_inferred="balanced" in sub_l or "dynamic" in sub_l)
        if "conservative" in sub_l:
            return FundClass(asset_class="Hybrid", sub_category=sub, tax_kind="debt", tax_inferred=True)
        return FundClass(asset_class="Hybrid", sub_category=sub or "Hybrid", tax_kind="other", tax_inferred=True)
    if head_l.startswith("solution"):
        return FundClass(asset_class="Solution", sub_category=sub or "Solution oriented", tax_kind="other", tax_inferred=True)
    # "Other Scheme" (index funds, ETFs, FoFs): decide from the name.
    asset = "FoF" if "fof" in sub_l or "fund of fund" in name.lower() else "Index & ETF" if sub else "Other"
    if "(debt)" in sub_l or sub_l == "debt etf":
        kind: TaxKind = "debt"
    elif _COMMODITY.search(name) or _FOREIGN.search(name):
        kind = "other"
    elif _DEBTLIKE.search(name):
        kind = "debt"
    elif "fof" in sub_l:
        kind = "other"
    else:
        kind = "equity"
    return FundClass(asset_class=asset, sub_category=sub or "Other", tax_kind=kind, tax_inferred=True)


# ----------------------------------------------------------------------------------------------
# Returns & risk from a NAV series
# ----------------------------------------------------------------------------------------------

def nav_on(series: NavSeries, day: dt.date, tolerance_days: int = 7,
           dates: list[dt.date] | None = None) -> tuple[dt.date, float] | None:
    """NAV on ``day`` or the closest earlier NAV within ``tolerance_days`` (pass ``dates`` when calling in a loop)."""
    dates = dates if dates is not None else [d for d, _ in series]
    i = bisect.bisect_right(dates, day) - 1
    if i < 0 or (day - dates[i]).days > tolerance_days:
        return None
    return series[i]


def years_ago(day: dt.date, years: float) -> dt.date:
    try:
        return day.replace(year=day.year - int(years)) if years == int(years) else day - dt.timedelta(days=round(years * 365.25))
    except ValueError:  # 29-Feb
        return day.replace(year=day.year - int(years), day=28)


PERIODS: list[tuple[str, float]] = [("1M", 1 / 12), ("3M", 0.25), ("6M", 0.5), ("1Y", 1), ("3Y", 3), ("5Y", 5), ("10Y", 10)]


def period_return(series: NavSeries, years: float, end: tuple[dt.date, float] | None = None) -> float | None:
    """Absolute % for periods under a year, CAGR % for a year or more."""
    if not series:
        return None
    end_d, end_n = end or series[-1]
    start = nav_on(series, years_ago(end_d, years) if years >= 1 else end_d - dt.timedelta(days=round(years * 365.25)))
    if start is None or start[1] <= 0:
        return None
    growth = end_n / start[1]
    span = (end_d - start[0]).days / 365.25
    if years < 1:
        return (growth - 1) * 100
    return (growth ** (1 / span) - 1) * 100 if span > 0 else None


class Returns(BaseModel):
    periods: dict[str, float | None]
    since_inception_cagr: float | None
    inception_date: dt.date
    nav_date: dt.date
    nav: float


def returns(series: NavSeries) -> Returns:
    d0, n0 = series[0]
    d1, n1 = series[-1]
    span = (d1 - d0).days / 365.25
    return Returns(periods={k: round(v, 2) if (v := period_return(series, y)) is not None else None for k, y in PERIODS},
                   since_inception_cagr=round(((n1 / n0) ** (1 / span) - 1) * 100, 2) if span >= 1 else None,
                   inception_date=d0, nav_date=d1, nav=n1)


class Rolling(BaseModel):
    window_years: int
    samples: int
    min: float
    p25: float
    median: float
    p75: float
    max: float
    pct_positive: float
    pct_above_10: float


def rolling(series: NavSeries, window_years: int, lookback_years: int = 10) -> Rolling | None:
    """Distribution of every rolling ``window_years`` CAGR whose start is within the last ``lookback_years``."""
    if not series:
        return None
    end = series[-1][0]
    first_start = years_ago(end, lookback_years)
    dates = [d for d, _ in series]
    vals: list[float] = []
    for d, n in series:
        if d < first_start:
            continue
        target = d.replace(year=d.year + window_years) if not (d.month == 2 and d.day == 29) else d.replace(year=d.year + window_years, day=28)
        if target > end:
            break
        hit = nav_on(series, target, dates=dates)
        if hit and n > 0:
            vals.append(((hit[1] / n) ** (1 / window_years) - 1) * 100)
    if len(vals) < 20:
        return None
    q = statistics.quantiles(vals, n=4)
    return Rolling(window_years=window_years, samples=len(vals), min=round(min(vals), 2), p25=round(q[0], 2),
                   median=round(statistics.median(vals), 2), p75=round(q[2], 2), max=round(max(vals), 2),
                   pct_positive=round(sum(v > 0 for v in vals) / len(vals) * 100, 1),
                   pct_above_10=round(sum(v > 10 for v in vals) / len(vals) * 100, 1))


class Risk(BaseModel):
    volatility_pct: float | None       # annualised, last 3 years
    max_drawdown_pct: float
    max_drawdown_peak: dt.date
    max_drawdown_trough: dt.date
    recovered_on: dt.date | None
    current_drawdown_pct: float
    best_year_pct: float | None
    worst_year_pct: float | None


def risk(series: NavSeries) -> Risk:
    peak_d, peak_n = series[0]
    mdd, mdd_peak, mdd_trough = 0.0, series[0][0], series[0][0]
    for d, n in series:
        if n > peak_n:
            peak_d, peak_n = d, n
        dd = n / peak_n - 1
        if dd < mdd:
            mdd, mdd_peak, mdd_trough = dd, peak_d, d
    peak_val = dict(series)[mdd_peak]
    recovered = next((d for d, n in series if d > mdd_trough and n >= peak_val), None) if mdd < 0 else None
    top = max(n for _, n in series)
    end = series[-1][0]
    recent = [n for d, n in series if d >= years_ago(end, 3)]
    rets = [b / a - 1 for a, b in zip(recent, recent[1:]) if a > 0]
    # Observations per year in the window (NAVs skip holidays and some funds skip days).
    per_year = len(rets) / 3 if len(rets) > 30 else None
    vol = statistics.stdev(rets) * math.sqrt(per_year) * 100 if per_year and len(rets) > 30 else None
    yearly: dict[int, list[float]] = defaultdict(list)
    for d, n in series:
        yearly[d.year].append(n)
    full_years = [y for y in yearly if y < end.year and y > series[0][0].year]
    year_rets = [(yearly[y][-1] / yearly[y - 1][-1] - 1) * 100 for y in full_years if y - 1 in yearly]
    return Risk(volatility_pct=round(vol, 2) if vol else None, max_drawdown_pct=round(mdd * 100, 2),
                max_drawdown_peak=mdd_peak, max_drawdown_trough=mdd_trough, recovered_on=recovered,
                current_drawdown_pct=round((series[-1][1] / top - 1) * 100, 2),
                best_year_pct=round(max(year_rets), 2) if year_rets else None,
                worst_year_pct=round(min(year_rets), 2) if year_rets else None)


# ----------------------------------------------------------------------------------------------
# SIP / lumpsum back-tests on real NAVs
# ----------------------------------------------------------------------------------------------

class SipInstalment(BaseModel):
    date: dt.date
    amount: float
    nav: float
    units: float


class SipResult(BaseModel):
    monthly_amount: float
    step_up_pct: float
    start: dt.date
    end: dt.date
    instalments: int
    invested: float
    units: float
    value: float
    gain: float
    absolute_return_pct: float
    xirr_pct: float | None
    curve: list[tuple[dt.date, float, float]]  # (date, invested so far, value)


def add_months(day: dt.date, months: int) -> dt.date:
    y, m = divmod(day.month - 1 + months, 12)
    for d in (day.day, 30, 29, 28):
        try:
            return dt.date(day.year + y, m + 1, d)
        except ValueError:
            continue
    raise ValueError(day)


def _first_on_or_after(series: NavSeries, day: dt.date) -> tuple[dt.date, float] | None:
    dates = [d for d, _ in series]
    i = bisect.bisect_left(dates, day)
    return series[i] if i < len(series) else None


def sip_backtest(series: NavSeries, monthly: float, years: float, step_up_pct: float = 0.0) -> SipResult | None:
    """Invest ``monthly`` on the same date each month (next NAV day if a holiday), stepping the
    amount up by ``step_up_pct`` every 12 instalments; value everything at the latest NAV."""
    if not series or monthly <= 0:
        return None
    end_d, end_n = series[-1]
    start = max(series[0][0], add_months(end_d, -round(years * 12)))
    units = invested = 0.0
    flows: list[tuple[dt.date, float]] = []
    curve: list[tuple[dt.date, float, float]] = []
    k, amount = 0, monthly
    while True:
        day = add_months(start, k)
        if day > end_d:
            break
        hit = _first_on_or_after(series, day)
        if hit is None:
            break
        if k and k % 12 == 0:
            amount *= 1 + step_up_pct / 100
        units += amount / hit[1]
        invested += amount
        flows.append((hit[0], -amount))
        curve.append((hit[0], round(invested, 2), round(units * hit[1], 2)))
        k += 1
    if not flows:
        return None
    value = units * end_n
    curve.append((end_d, round(invested, 2), round(value, 2)))
    rate = xirr([*flows, (end_d, value)])
    return SipResult(monthly_amount=monthly, step_up_pct=step_up_pct, start=flows[0][0], end=end_d, instalments=len(flows),
                     invested=round(invested, 2), units=round(units, 4), value=round(value, 2), gain=round(value - invested, 2),
                     absolute_return_pct=round((value / invested - 1) * 100, 2),
                     xirr_pct=round(rate * 100, 2) if rate is not None else None, curve=curve)


# ----------------------------------------------------------------------------------------------
# Category ranking from all-scheme NAV snapshots
# ----------------------------------------------------------------------------------------------

class Rank(BaseModel):
    period: str
    return_pct: float
    rank: int
    of: int
    percentile: float      # 100 = best in category
    quartile: int          # 1 = top quarter


def rank_category(peers: dict[str, float], code: str, period: str) -> Rank | None:
    """``peers``: scheme code -> period return for every comparable scheme (same category & plan, growth)."""
    if code not in peers or len(peers) < 4:
        return None
    ordered = sorted(peers, key=lambda c: -peers[c])
    pos = ordered.index(code) + 1
    n = len(ordered)
    pct = round((n - pos) / (n - 1) * 100, 1) if n > 1 else 100.0
    return Rank(period=period, return_pct=round(peers[code], 2), rank=pos, of=n, percentile=pct,
                quartile=min(4, 1 + (pos - 1) * 4 // n))
