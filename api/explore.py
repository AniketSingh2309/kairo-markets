"""Explore home: market movers (+ sectors) and trading screens with track records.

Both are background jobs keyed by universe. A request returns immediately with the
latest finished result plus progress of any refresh in flight ("stale while
revalidate"), so the page never waits on 40 data-source round trips.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from api import deps
from api.live import get_live_provider
from api.insights import SCAN_CONCURRENCY, load_universe
from core.universes import Universes
from core.signals import run_screens
from tools.errors import ToolError
from tools.market_tools import get_daily_snapshot
from tools.models import DailySnapshot
from tools.providers import MarketDataProvider

router = APIRouter(prefix="/explore", tags=["explore"])
SNAPSHOTS: deps.TTLCache[DailySnapshot] = deps.TTLCache(60)
MOVERS_TTL_S, SCREENS_TTL_S = 60, 30 * 60


class Job:
    def __init__(self, total: int):
        self.status, self.done, self.total = "running", 0, total
        self.result: Any = None
        self.errors: dict[str, str] = {}
        self.started = time.monotonic()
        self.finished_at: dt.datetime | None = None


class JobBoard:
    """Latest finished result per key + at most one refresh in flight."""

    def __init__(self) -> None:
        self.ready: dict[str, Job] = {}
        self.running: dict[str, Job] = {}

    def state(self, key: str, ttl: float, total: int, worker: Callable[[Job], Awaitable[Any]],
              refresh: bool = False) -> dict[str, Any]:
        ready, running = self.ready.get(key), self.running.get(key)
        stale = ready is None or refresh or time.monotonic() - ready.started > ttl
        if stale and running is None:
            job = running = self.running[key] = Job(total)

            async def run() -> None:
                try:
                    job.result = await worker(job)
                    job.status = "ready"
                    self.ready[key] = job
                except Exception as exc:  # noqa: BLE001 - surface, keep last good result
                    job.status, job.errors["_job"] = "error", f"{type(exc).__name__}: {exc}"
                finally:
                    job.finished_at = dt.datetime.now(dt.timezone.utc)
                    self.running.pop(key, None)
            asyncio.create_task(run())
        shown = ready or running
        return {
            "status": "ready" if ready and not running else "refreshing" if ready else running.status,
            "progress": {"done": running.done, "total": running.total} if running else None,
            "computed_at": ready.finished_at.isoformat() if ready and ready.finished_at else None,
            "errors": shown.errors if shown else {},
            "result": ready.result if ready else None,
        }


BOARD = JobBoard()


def movers_ttl(n: int) -> float:
    """Big universes refresh less often (about 2 price requests a second at most) to stay under rate limits."""
    return max(MOVERS_TTL_S, n * 0.5)


async def snapshot(symbol: str, provider: MarketDataProvider) -> DailySnapshot:
    async def load() -> DailySnapshot:
        return (await run_in_threadpool(get_daily_snapshot, symbol, provider=provider,
                                        timeout_s=deps.get_settings().tool_timeout_seconds)).data
    return await SNAPSHOTS.get(symbol, load)


@router.get("/movers")
async def movers(universe: str | None = Query(None), refresh: bool = Query(False),
                 registry: Universes = Depends(deps.get_universes),
                 provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    """Every symbol's latest session: change, volume vs average, 52-week position, sector, sparkline."""
    u = await run_in_threadpool(load_universe, universe, registry)
    universe, label, symbols = u.key, u.label, u.symbols
    sectors = {m.symbol: m.sector for m in u.members if m.sector}

    async def work(job: Job) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        async def one(sym: str) -> None:
            try:
                snap = await snapshot(sym, provider)
                sector = sectors.get(sym)  # from the official list; ask the provider only if it has none
                if sector is None:
                    try:
                        sector = (await deps.profile(sym, provider)).sector
                    except ToolError:
                        sector = None
                hi, lo = snap.fifty_two_week_high, snap.fifty_two_week_low
                rows.append({**snap.model_dump(mode="json"), "sector": sector,
                             "from_52w_high": round((snap.price / hi - 1) * 100, 2) if hi else None,
                             "from_52w_low": round((snap.price / lo - 1) * 100, 2) if lo else None})
            except ToolError as exc:
                job.errors[sym] = exc.code
            finally:
                job.done += 1
        await deps.gather_limited(symbols, one, limit=SCAN_CONCURRENCY)
        return rows

    return {"universe": universe, "label": label, "source": u.source,
            **BOARD.state(f"movers:{universe}", movers_ttl(len(symbols)), len(symbols), work, refresh)}


@router.get("/screens")
async def screens(universe: str | None = Query(None), horizon: int = Query(10, ge=3, le=30),
                  registry: Universes = Depends(deps.get_universes),
                  provider: MarketDataProvider = Depends(get_live_provider)) -> dict[str, Any]:
    """Technical screens: who triggered in the last 3 sessions, and each screen's 2-year track record."""
    u = await run_in_threadpool(load_universe, universe, registry)
    universe, label, symbols = u.key, u.label, u.symbols

    async def work(job: Job) -> list[dict[str, Any]]:
        histories: dict[str, tuple[str | None, list]] = {}

        async def one(sym: str) -> None:
            try:
                h = await deps.history(sym, "2y", provider)
                histories[sym] = (h.name, h.bars)
            except ToolError as exc:
                job.errors[sym] = exc.code
            finally:
                job.done += 1
        await deps.gather_limited(symbols, one, limit=SCAN_CONCURRENCY)
        results = await run_in_threadpool(run_screens, histories, horizon)
        return [r.model_dump(mode="json") for r in results]

    key = f"screens:{universe}:{horizon}"
    return {"universe": universe, "label": label, "source": u.source, "horizon": horizon,
            **BOARD.state(key, SCREENS_TTL_S, len(symbols), work)}
