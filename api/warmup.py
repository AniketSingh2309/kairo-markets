"""Background warm-up at server start, so the first page view after a restart is fast.

Every cache in the app lives in memory (plus a few on disk), so a fresh server would otherwise make the
first visitor wait for: Explore's default market scan, the screener, and the mutual-fund rankings
(megabytes from AMFI). This kicks those off in the background a moment after startup. It never runs
under the test suite or with the mock data provider, and KAIRO_WARMUP=0 turns it off.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from starlette.concurrency import run_in_threadpool

log = logging.getLogger("kairo.warmup")


def enabled() -> bool:
    from core.config import get_settings

    return ("pytest" not in sys.modules and os.environ.get("KAIRO_WARMUP", "1") != "0"
            and get_settings().data_provider != "mock")


async def warm_up(delay_s: float = 1.0) -> None:
    from api import deps
    from api.explore import movers, screens
    from api.funds import RANKINGS
    from api.insights import screener
    from api.live import get_live_provider

    await asyncio.sleep(delay_s)
    provider, registry = get_live_provider(), deps.get_universes()

    async def step(name, coro):
        try:
            await coro
            log.info("warm-up: %s started", name)
        except Exception as exc:  # noqa: BLE001 - warm-up is best effort
            log.warning("warm-up: %s failed: %s", name, exc)

    # Endpoint functions start their own background jobs and return at once.
    await step("explore movers", movers(universe=None, refresh=False, registry=registry, provider=provider))
    await step("explore screens", screens(universe=None, horizon=10, registry=registry, provider=provider))
    await step("screener", screener(universe=None, symbols=None, refresh=False, registry=registry, provider=provider))
    from api.options import RECORDER, get_nse
    RECORDER.ensure_running(get_nse(), deps.get_store())  # PCR through the day for NIFTY / BANKNIFTY
    log.info("warm-up: option-chain recorder started")
    funds = getattr(provider, "funds", None)
    if funds is not None:
        await step("fund rankings", run_in_threadpool(RANKINGS.get, funds.data))
