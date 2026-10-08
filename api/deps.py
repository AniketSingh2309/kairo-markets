"""Shared dependencies for the app-shell APIs: store, alert engine and TTL caches.

Caches keep Yahoo traffic polite (it rate-limits aggressively) and make page loads fast.
Each cache entry remembers when it was fetched so responses can say how old they are.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any, Generic, TypeVar

from starlette.concurrency import run_in_threadpool

from api.live import get_live_provider, get_stream_hub
from core.alerts import AlertEngine
from core.config import get_settings
from core.portfolio import QuoteLite
from core.store import Store
from core.universes import Universes, shared_universes
from tools.errors import ToolError
from tools.market_tools import get_announcements, get_live_quote, get_price_history, get_profile
from tools.models import Announcements, PriceHistory, Profile
from tools.providers import MarketDataProvider
from tools.providers.logos import LogoStore
from tools.providers.news_images import NewsImageStore
from tools.providers.news_reader import NewsReader
from tools.providers.mock_provider import MockMarketDataProvider

V = TypeVar("V")
CONCURRENCY = 3


class TTLCache(Generic[V]):
    def __init__(self, ttl_s: float, max_items: int = 2000):
        self.ttl, self.max = ttl_s, max_items
        self._data: dict[Any, tuple[float, V]] = {}
        self._locks: dict[Any, asyncio.Lock] = {}

    def peek(self, key: Any) -> V | None:
        hit = self._data.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] < self.ttl else None

    async def get(self, key: Any, loader: Callable[[], Awaitable[V]]) -> V:
        if (hit := self.peek(key)) is not None:
            return hit
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # one fetch per key even under concurrent requests
            if (hit := self.peek(key)) is not None:
                return hit
            value = await loader()
            if len(self._data) >= self.max:
                self._data.pop(next(iter(self._data)))
            self._data[key] = (time.monotonic(), value)
            return value


QUOTES: TTLCache[QuoteLite] = TTLCache(20)
HISTORY: TTLCache[PriceHistory] = TTLCache(30 * 60)
PROFILES: TTLCache[Profile] = TTLCache(24 * 3600)
NEWS: TTLCache[Announcements] = TTLCache(10 * 60)


async def gather_limited(items: list[Any], fn: Callable[[Any], Awaitable[V]],
                         limit: int = CONCURRENCY) -> list[V | BaseException]:
    sem = asyncio.Semaphore(limit)

    async def one(item: Any) -> V:
        async with sem:
            return await fn(item)
    return await asyncio.gather(*(one(i) for i in items), return_exceptions=True)


def _timeout() -> float:
    return get_settings().tool_timeout_seconds


async def quote(symbol: str, provider: MarketDataProvider) -> QuoteLite:
    async def load() -> QuoteLite:
        q = (await run_in_threadpool(get_live_quote, symbol, provider=provider, timeout_s=_timeout())).data
        return QuoteLite(price=q.price, previous_close=q.previous_close, currency=q.currency, name=q.name,
                         market_time=q.market_time)
    return await QUOTES.get(symbol, load)


async def history(symbol: str, period: str, provider: MarketDataProvider) -> PriceHistory:
    async def load() -> PriceHistory:
        return (await run_in_threadpool(get_price_history, symbol, period, provider=provider,
                                        timeout_s=_timeout())).data
    return await HISTORY.get((symbol, period), load)


async def profile(symbol: str, provider: MarketDataProvider) -> Profile:
    async def load() -> Profile:
        return (await run_in_threadpool(get_profile, symbol, provider=provider, timeout_s=_timeout())).data
    return await PROFILES.get(symbol, load)


async def news(symbol: str, period: str, provider: MarketDataProvider) -> Announcements:
    async def load() -> Announcements:
        return (await run_in_threadpool(get_announcements, symbol, period, provider=provider,
                                        timeout_s=_timeout())).data
    return await NEWS.get((symbol, period), load)


async def quotes_for(symbols: list[str], provider: MarketDataProvider) -> tuple[dict[str, QuoteLite], dict[str, str]]:
    results = await gather_limited(symbols, lambda s: quote(s, provider))
    ok, errors = {}, {}
    for sym, res in zip(symbols, results):
        if isinstance(res, ToolError):
            errors[sym] = res.code
        elif isinstance(res, BaseException):
            raise res
        else:
            ok[sym] = res
    return ok, errors


@lru_cache(maxsize=1)
def get_store() -> Store:
    return Store(get_settings().db_path)


@lru_cache(maxsize=1)
def get_universes() -> Universes:
    """Stock lists for the screener, Explore and search. Real lists are downloaded in the background at
    first use and cached under data/listings/ (refreshed weekly); the mock provider gets its own list."""
    settings = get_settings()
    if settings.data_provider == "mock":
        return Universes.for_mock(MockMarketDataProvider().symbols)
    universes = shared_universes(settings.db_path.parent / "listings")
    threading.Thread(target=universes.warm, name="listings-warm", daemon=True).start()
    return universes


@lru_cache(maxsize=1)
def get_news_reader() -> NewsReader:
    return NewsReader(get_settings().db_path.parent / "filings")


@lru_cache(maxsize=1)
def get_news_images() -> NewsImageStore:
    return NewsImageStore(get_settings().db_path.parent / "news_images")


@lru_cache(maxsize=1)
def get_logo_store() -> LogoStore:
    return LogoStore(get_settings().db_path.parent / "logos")


@lru_cache(maxsize=1)
def get_alert_engine() -> AlertEngine:
    return AlertEngine(get_store(), get_live_provider(), get_stream_hub(), timeout_s=_timeout())
