"""Portfolio health check: deterministic, explainable checks on current holdings.

Every check states what was measured, the value, and why it matters; the score is
100 minus fixed penalties (bad = 20, warn = 8), so it is fully traceable. Risk metrics
use the *current* weights applied to the last year of daily returns -- "how would
today's portfolio have behaved", not your historical returns (that is XIRR's job).
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from core import technicals as ta
from tools.models import PriceBar

Status = Literal["good", "warn", "bad", "info"]
PENALTY = {"bad": 20, "warn": 8, "good": 0, "info": 0}
TRADING_DAYS = 252


class HealthCheck(BaseModel):
    id: str
    title: str
    status: Status
    value: str
    detail: str


class SectorSlice(BaseModel):
    sector: str
    weight: float
    symbols: list[str]


class HealthMetrics(BaseModel):
    holdings: int
    effective_holdings: float | None
    largest_weight: float | None
    volatility_pct: float | None
    benchmark: str | None
    benchmark_volatility_pct: float | None
    beta: float | None
    max_drawdown_pct: float | None
    avg_correlation: float | None
    below_200dma_weight: float | None
    days_used: int


class HealthReport(BaseModel):
    currency: str
    as_of: dt.datetime
    score: int
    grade: str
    checks: list[HealthCheck]
    sectors: list[SectorSlice]
    metrics: HealthMetrics
    notes: list[str]


class HoldingInput(BaseModel):
    symbol: str
    value: float
    pnl_pct: float | None = None
    sector: str | None = None


def _closes_by_date(bars: Sequence[PriceBar]) -> dict[dt.date, float]:
    return {b.date: b.close for b in bars}


def _returns(series: dict[dt.date, float], dates: list[dt.date]) -> list[float]:
    return [series[b] / series[a] - 1 for a, b in zip(dates, dates[1:])]


def _corr(x: list[float], y: list[float]) -> float | None:
    if len(x) < 20:
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def _grade(score: int) -> str:
    return "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"


def build_health(
    currency: str,
    holdings: list[HoldingInput],
    histories: dict[str, list[PriceBar]],
    benchmark: tuple[str, list[PriceBar]] | None,
) -> HealthReport:
    checks: list[HealthCheck] = []
    notes: list[str] = []
    total = sum(h.value for h in holdings if h.value > 0)
    weights = {h.symbol: h.value / total for h in holdings if total and h.value > 0}

    # --- diversification -------------------------------------------------------------
    n = len(weights)
    hhi = sum(w * w for w in weights.values())
    eff = 1 / hhi if hhi else None
    status: Status = "bad" if n < 3 else "warn" if n < 8 else "good"
    checks.append(HealthCheck(
        id="holdings", title="Number of holdings", status=status,
        value=f"{n} holdings" + (f" (behaves like {eff:.1f} equal positions)" if eff else ""),
        detail="Fewer than about 8 positions leaves results dominated by a handful of companies."
        if status != "good" else "Spread across enough companies that one surprise can't sink the whole portfolio."))

    largest = max(weights.items(), key=lambda kv: kv[1]) if weights else None
    if largest:
        sym, w = largest
        status = "bad" if w > 0.40 else "warn" if w > 0.25 else "good"
        checks.append(HealthCheck(
            id="concentration", title="Largest position", status=status, value=f"{sym} is {w:.0%} of the portfolio",
            detail="A single stock above 25% means one result or regulatory event can move your whole net worth."
            if status != "good" else "No single stock dominates."))

    # --- sectors -------------------------------------------------------------------------
    by_sector: dict[str, list[str]] = {}
    for h in holdings:
        if h.symbol in weights:
            by_sector.setdefault(h.sector or "Unclassified", []).append(h.symbol)
    sectors = sorted((SectorSlice(sector=s, weight=round(sum(weights[x] for x in syms) * 100, 2), symbols=syms)
                      for s, syms in by_sector.items()), key=lambda s: -s.weight)
    known = [s for s in sectors if s.sector != "Unclassified"]
    unknown = next((s.symbols for s in sectors if s.sector == "Unclassified"), [])
    if unknown:
        notes.append(f"Sector unknown for {', '.join(unknown)} (shown as Unclassified).")
    if known:
        top = known[0]
        status = "bad" if top.weight > 50 else "warn" if top.weight > 35 else "good"
        checks.append(HealthCheck(
            id="sector", title="Sector concentration", status=status, value=f"{top.sector} is {top.weight:.0f}%",
            detail="Stocks in one sector tend to fall together when that sector is out of favour."
            if status != "good" else f"Exposure is spread across {len(known)} sectors."))

    # --- risk from price history -----------------------------------------------------------
    series = {s: _closes_by_date(histories[s]) for s in weights if histories.get(s)}
    missing = [s for s in weights if s not in series]
    if missing:
        notes.append(f"No price history for {', '.join(missing)}; risk metrics exclude them.")
    vol = beta = mdd = avg_corr = bench_vol = None
    days_used = 0
    common: list[dt.date] = []
    if series:
        common = sorted(set.intersection(*(set(v) for v in series.values())))[-(TRADING_DAYS + 1):]
    if len(common) >= 30:
        days_used = len(common) - 1
        w_sub = {s: weights[s] for s in series}
        norm = sum(w_sub.values())
        rets = {s: _returns(series[s], common) for s in series}
        port = [sum(w_sub[s] / norm * rets[s][i] for s in series) for i in range(days_used)]
        vol = statistics.stdev(port) * math.sqrt(TRADING_DAYS) * 100
        level, peak, mdd = 1.0, 1.0, 0.0
        for r in port:
            level *= 1 + r
            peak = max(peak, level)
            mdd = min(mdd, level / peak - 1)
        mdd *= 100
        syms = list(series)
        pairs = [_corr(rets[a], rets[b]) for i, a in enumerate(syms) for b in syms[i + 1:]]
        pairs = [p for p in pairs if p is not None]
        avg_corr = statistics.fmean(pairs) if pairs else None

        if benchmark and benchmark[1]:
            bser = _closes_by_date(benchmark[1])
            bdates = [d for d in common if d in bser]
            if len(bdates) >= 30:
                b_rets = _returns(bser, bdates)
                p_rets = [sum(w_sub[s] / norm * (series[s][b] / series[s][a] - 1) for s in series)
                          for a, b in zip(bdates, bdates[1:])]
                var_b = statistics.pvariance(b_rets)
                if var_b > 0:
                    mb, mp = statistics.fmean(b_rets), statistics.fmean(p_rets)
                    beta = sum((x - mb) * (y - mp) for x, y in zip(b_rets, p_rets)) / len(b_rets) / var_b
                bench_vol = statistics.stdev(b_rets) * math.sqrt(TRADING_DAYS) * 100

        status = "info"
        if bench_vol:
            status = "warn" if vol > 1.5 * bench_vol else "good"
        checks.append(HealthCheck(
            id="volatility", title="Volatility (annualised)", status=status,
            value=f"{vol:.1f}%" + (f" vs {bench_vol:.1f}% for {benchmark[0]}" if bench_vol and benchmark else ""),
            detail="Typical yearly swing of today's holdings over the last year. Much higher than the index means "
                   "bigger ups and downs to sit through." if status == "warn" else
                   "Typical yearly swing of today's holdings, measured over the last year."))
        if beta is not None and benchmark:
            status = "warn" if beta > 1.3 else "good"
            checks.append(HealthCheck(
                id="beta", title=f"Sensitivity to {benchmark[0]} (beta)", status=status, value=f"{beta:.2f}",
                detail=f"When {benchmark[0]} moves 1%, this portfolio has typically moved about {beta:.2f}%."))
        status = "bad" if mdd < -35 else "warn" if mdd < -20 else "good"
        checks.append(HealthCheck(
            id="drawdown", title="Worst fall in the last year", status=status, value=f"{mdd:.1f}%",
            detail="Largest peak-to-trough drop today's holdings would have suffered over the period measured."))
        if avg_corr is not None:
            status = "warn" if avg_corr > 0.6 else "good"
            checks.append(HealthCheck(
                id="correlation", title="How much holdings move together", status=status,
                value=f"average correlation {avg_corr:.2f}",
                detail="Holdings that move in lockstep diversify less than their count suggests."
                if status == "warn" else "Holdings move independently enough to diversify one another."))
    elif series:
        notes.append("Less than 30 overlapping trading days of history; risk metrics skipped.")

    # --- trend & losers ----------------------------------------------------------------------
    below = 0.0
    measured = 0.0
    for s, bars in histories.items():
        if s not in weights or len(bars) < 200:
            continue
        sma200 = ta.sma([b.close for b in bars], 200)[-1]
        measured += weights[s]
        if sma200 and bars[-1].close < sma200:
            below += weights[s]
    below_pct = below / measured * 100 if measured else None
    if below_pct is not None:
        status = "warn" if below_pct > 50 else "good"
        checks.append(HealthCheck(
            id="trend", title="Holdings below their 200-day average", status=status,
            value=f"{below_pct:.0f}% of value",
            detail="Positions trading under their 200-day average are in longer-term downtrends."))
    losers = [h for h in holdings if h.pnl_pct is not None and h.pnl_pct < -25]
    if losers:
        checks.append(HealthCheck(
            id="losers", title="Deep unrealized losses", status="warn",
            value=", ".join(f"{h.symbol} {h.pnl_pct:.0f}%" for h in losers[:4]),
            detail="Worth reviewing whether the original reason for owning these still holds."))

    score = max(0, 100 - sum(PENALTY[c.status] for c in checks))
    notes.append("Based on current holdings and the last year of daily prices. Not investment advice.")
    return HealthReport(
        currency=currency, as_of=dt.datetime.now(dt.timezone.utc), score=score, grade=_grade(score),
        checks=checks, sectors=sectors, notes=notes,
        metrics=HealthMetrics(
            holdings=n, effective_holdings=round(eff, 2) if eff else None,
            largest_weight=round(largest[1] * 100, 2) if largest else None,
            volatility_pct=round(vol, 2) if vol is not None else None,
            benchmark=benchmark[0] if benchmark else None,
            benchmark_volatility_pct=round(bench_vol, 2) if bench_vol is not None else None,
            beta=round(beta, 3) if beta is not None else None,
            max_drawdown_pct=round(mdd, 2) if mdd is not None else None,
            avg_correlation=round(avg_corr, 3) if avg_corr is not None else None,
            below_200dma_weight=round(below_pct, 1) if below_pct is not None else None,
            days_used=days_used))
