"""Mutual funds API: search, top funds by category, fund detail, SIP back-test, compare."""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from api.live import get_live_provider
from core import mutual_funds as mf
from tools.models import mf_symbol
from tools.providers.amfi_provider import AmfiData, Scheme
from tools.providers.base import ProviderError

router = APIRouter(prefix="/funds", tags=["funds"])
RANK_PERIODS: list[tuple[str, int]] = [("1Y", 1), ("3Y", 3), ("5Y", 5)]
DATA_NOTE = ("NAVs from AMFI (official). NAV history via mfapi.in, a free mirror of AMFI data. Expense ratio, "
             "AUM and portfolio holdings aren't published as free official data, so they're not shown.")


def get_funds(provider: Any = Depends(get_live_provider)) -> AmfiData:
    funds = getattr(provider, "funds", None)
    if funds is None:
        raise HTTPException(status_code=503, detail="mutual-fund data is not configured")
    return funds.data


def _call(fn, *args):
    try:
        return fn(*args)
    except ProviderError as exc:
        status = 404 if "no AMFI scheme" in str(exc) or "no NAV history" in str(exc) else 503
        raise HTTPException(status_code=status, detail=str(exc)) from None


def _active(schemes: dict[str, Scheme]) -> list[Scheme]:
    """Open-ended schemes with a NAV in the last 15 days (drops matured / merged / legacy entries)."""
    latest = max((s.nav_date for s in schemes.values() if s.nav_date), default=dt.date.today())
    return [s for s in schemes.values() if s.nav and s.nav_date and (latest - s.nav_date).days <= 15
            and (s.scheme_type or "").startswith("Open Ended")]


# ------------------------------------------------------------------------- category rankings


class _Rankings:
    """Point-to-point 1Y/3Y/5Y returns for every scheme, from AMFI's all-scheme NAV snapshots."""

    TTL_S = 12 * 3600

    def __init__(self) -> None:
        self.at = 0.0
        self.returns: dict[str, dict[str, float]] = {}
        self.lock = threading.Lock()

    def get(self, data: AmfiData) -> dict[str, dict[str, float]]:
        if time.monotonic() - self.at < self.TTL_S and self.returns:
            return self.returns
        with self.lock:
            if time.monotonic() - self.at < self.TTL_S and self.returns:
                return self.returns
            schemes = {s.code: s for s in _active(data.catalog())}
            today = max(s.nav_date for s in schemes.values())
            out: dict[str, dict[str, float]] = {c: {} for c in schemes}
            for label, years in RANK_PERIODS:
                try:
                    then_d, navs = data.snapshot(mf.years_ago(today, years))
                except ProviderError:
                    continue
                for code, s in schemes.items():
                    old = navs.get(code)
                    span = (s.nav_date - then_d).days / 365.25
                    if old and span > 0.9 * years:
                        out[code][label] = round(((s.nav / old) ** (1 / span) - 1) * 100, 2)
            self.returns, self.at = out, time.monotonic()
            return out


RANKINGS = _Rankings()


def _peers(s: Scheme, schemes: list[Scheme]) -> list[Scheme]:
    return [x for x in schemes if x.category == s.category and x.plan == s.plan and x.option == "Growth"]


def _row(s: Scheme, rets: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    cls = mf.classify(s.category, s.name)
    return {"code": s.code, "symbol": mf_symbol(s.code), "name": s.name, "amc": s.amc, "category": s.category,
            "asset_class": cls.asset_class, "sub_category": cls.sub_category, "plan": s.plan, "option": s.option,
            "nav": s.nav, "nav_date": s.nav_date.isoformat() if s.nav_date else None,
            "returns": (rets or {}).get(s.code, {})}


# ------------------------------------------------------------------------- endpoints


@router.get("/categories")
async def categories(data: AmfiData = Depends(get_funds)) -> list[dict[str, Any]]:
    schemes = await run_in_threadpool(_call, data.catalog)
    counts: dict[str, int] = {}
    for s in _active(schemes):
        if s.option == "Growth" and s.plan == "Direct" and s.category:
            counts[s.category] = counts.get(s.category, 0) + 1
    out = []
    for cat, n in counts.items():
        cls = mf.classify(cat, "")
        out.append({"category": cat, "asset_class": cls.asset_class, "sub_category": cls.sub_category, "funds": n})
    order = {"Equity": 0, "Hybrid": 1, "Index & ETF": 2, "Debt": 3, "FoF": 4, "Solution": 5, "Other": 6}
    return sorted(out, key=lambda c: (order.get(c["asset_class"], 9), -c["funds"]))


@router.get("/search")
async def search(q: str = Query("", max_length=80), category: str | None = None,
                 plan: str = Query("Direct", pattern="^(Direct|Regular|all)$"), growth_only: bool = True,
                 limit: int = Query(40, ge=1, le=200), data: AmfiData = Depends(get_funds)) -> dict[str, Any]:
    schemes = _active(await run_in_threadpool(_call, data.catalog))
    tokens = [t for t in q.lower().split() if t]
    hits = []
    for s in schemes:
        if category and s.category != category:
            continue
        if plan != "all" and s.plan != plan:
            continue
        if growth_only and s.option != "Growth":
            continue
        hay = f"{s.name} {s.amc or ''} {s.code}".lower()
        if tokens and not all(t in hay for t in tokens):
            continue
        score = (0 if tokens and s.name.lower().startswith(tokens[0]) else 1, s.name)
        hits.append((score, s))
    hits.sort(key=lambda h: h[0])
    rets = RANKINGS.returns or None
    return {"total": len(hits), "items": [_row(s, rets) for _, s in hits[:limit]], "ranked": bool(rets)}


@router.get("/top")
async def top(category: str, period: str = Query("3Y", pattern="^(1Y|3Y|5Y)$"),
              plan: str = Query("Direct", pattern="^(Direct|Regular)$"), limit: int = Query(25, ge=1, le=100),
              data: AmfiData = Depends(get_funds)) -> dict[str, Any]:
    schemes = _active(await run_in_threadpool(_call, data.catalog))
    rets = await run_in_threadpool(_call, RANKINGS.get, data)
    pool = [s for s in schemes if s.category == category and s.plan == plan and s.option == "Growth"]
    ranked = sorted((s for s in pool if period in rets.get(s.code, {})), key=lambda s: -rets[s.code][period])
    vals = [rets[s.code][period] for s in ranked]
    return {"category": category, "period": period, "plan": plan, "funds": len(pool), "with_history": len(ranked),
            "median": round(sorted(vals)[len(vals) // 2], 2) if vals else None,
            "items": [{**_row(s, rets), "rank": i + 1} for i, s in enumerate(ranked[:limit])]}


@router.get("/compare")
async def compare(codes: str, data: AmfiData = Depends(get_funds)) -> dict[str, Any]:
    wanted = [c.strip().upper().removeprefix("MF") for c in codes.split(",") if c.strip()][:4]
    if len(wanted) < 2:
        raise HTTPException(status_code=422, detail="give 2-4 scheme codes")
    funds = []
    for code in wanted:
        s = await run_in_threadpool(_call, data.scheme, code)
        series = await run_in_threadpool(_call, data.history, code)
        funds.append((s, series))
    start = max(series[0][0] for _, series in funds)
    end = min(series[-1][0] for _, series in funds)
    out = []
    for s, series in funds:
        window = [(d, n) for d, n in series if start <= d <= end]
        base = window[0][1]
        step = max(1, len(window) // 400)
        thinned = window[::step] if window[::step][-1] == window[-1] else window[::step] + window[-1:]
        out.append({**_row(s), "returns": mf.returns(series).model_dump(mode="json"),
                    "risk": mf.risk(series).model_dump(mode="json"),
                    "rolling_3y": (r.model_dump() if (r := mf.rolling(series, 3)) else None),
                    "growth": [(d.isoformat(), round(n / base * 100, 3)) for d, n in thinned]})
    return {"start": start.isoformat(), "end": end.isoformat(), "funds": out}


@router.get("/{code}")
async def detail(code: str, data: AmfiData = Depends(get_funds)) -> dict[str, Any]:
    code = code.upper().removeprefix("MF")
    s = await run_in_threadpool(_call, data.scheme, code)
    series = await run_in_threadpool(_call, data.history, code)
    schemes = _active(await run_in_threadpool(_call, data.catalog))
    ranks: list[dict[str, Any]] = []
    try:
        rets = await run_in_threadpool(RANKINGS.get, data)
        peers = _peers(s, schemes)
        for label, _ in RANK_PERIODS:
            pr = {p.code: rets[p.code][label] for p in peers if label in rets.get(p.code, {})}
            if (r := mf.rank_category(pr, s.code, label)) is not None:
                ranks.append({**r.model_dump(), "category_median": round(sorted(pr.values())[len(pr) // 2], 2)})
    except ProviderError:
        pass
    cls = mf.classify(s.category, s.name)
    other_plans = [_row(x) for x in schemes if x.name == s.name and x.code != s.code][:4]
    step = max(1, len(series) // 1500)
    sampled = list(series[::step])
    if sampled[-1] != series[-1]:
        sampled.append(series[-1])
    prev_nav = series[-2][1] if len(series) > 1 else None
    return {**_row(s), "classification": cls.model_dump(), "returns": mf.returns(series).model_dump(mode="json"),
            "previous_nav": prev_nav, "previous_nav_date": series[-2][0].isoformat() if prev_nav else None,
            "day_change_pct": round((series[-1][1] / prev_nav - 1) * 100, 4) if prev_nav else None,
            "rolling": {f"{y}Y": (r.model_dump() if (r := mf.rolling(series, y)) else None) for y in (1, 3, 5)},
            "risk": mf.risk(series).model_dump(mode="json"), "ranks": ranks, "other_plans": other_plans,
            "nav_series": [(d.isoformat(), n) for d, n in sampled], "data_note": DATA_NOTE}


@router.get("/{code}/sip")
async def sip(code: str, amount: float = Query(5000, gt=0, le=10_000_000), years: float = Query(5, gt=0, le=30),
              step_up: float = Query(0, ge=0, le=50), data: AmfiData = Depends(get_funds)) -> dict[str, Any]:
    code = code.upper().removeprefix("MF")
    series = await run_in_threadpool(_call, data.history, code)
    res = mf.sip_backtest(series, amount, years, step_up)
    if res is None:
        raise HTTPException(status_code=404, detail="not enough NAV history for this SIP")
    available = (series[-1][0] - series[0][0]).days / 365.25
    return {**res.model_dump(mode="json"), "requested_years": years,
            "note": None if available >= years else f"Fund history covers only {available:.1f} years; the SIP starts at inception."}
