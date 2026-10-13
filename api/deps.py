"""Shared dependencies for the app-shell APIs: stores (per account), alert engines and TTL caches.

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

from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from api.live import get_live_provider, get_stream_hub
from core.accounts import Accounts
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

    def last_good(self, key: Any) -> V | None:
        """The last value fetched for `key`, however old: served when a refresh fails."""
        hit = self._data.get(key)
        return hit[1] if hit else None

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
def get_shared_store() -> Store:
    """Market-wide data (OI snapshots, IV and FII/DII history), and everything in single-user mode."""
    return Store(get_settings().db_path)


# ---------------------------------------------------------------------------- accounts
AUTH_COOKIE = "kairo_session"
_USER_STORES: dict[str, Store] = {}
_USER_LOCK = threading.Lock()


def auth_on() -> bool:
    return get_settings().auth == "on"


@lru_cache(maxsize=1)
def get_accounts() -> Accounts:
    return Accounts(get_settings().db_path.parent / "accounts.db")


def user_db_path(user_id: str):
    return get_settings().db_path.parent / "users" / f"{user_id}.db"


def user_store(user_id: str) -> Store:
    """Each account's own database: portfolio, paper account, journal, alerts, scans."""
    with _USER_LOCK:
        st = _USER_STORES.get(user_id)
        if st is None:
            st = _USER_STORES[user_id] = Store(user_db_path(user_id))
        return st


def drop_user_store(user_id: str) -> None:
    with _USER_LOCK:
        st = _USER_STORES.pop(user_id, None)
    if st is not None:
        _ENGINES.pop(id(st), None)
        st._conn.close()


def current_session(request: Request) -> dict[str, Any]:
    """The signed-in, unlocked session for this request: 401 when signed out, 423 when the app PIN is needed."""
    accounts, token = get_accounts(), request.cookies.get(AUTH_COOKIE)
    s = accounts.session(token)
    if s is None:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED", "message": "Please log in to continue."})
    if accounts.is_locked(s):
        raise HTTPException(status_code=423, detail={"code": "LOCKED", "message": "Enter your PIN to continue."})
    accounts.touch(token, s)
    return s


def get_store(request: Request) -> Store:
    """The signed-in user's store (or the single shared store when accounts are off)."""
    if not auth_on():
        return get_shared_store()
    return user_store(current_session(request)["user_id"])


def all_stores() -> list[Store]:
    """Every account's store, for background jobs (scan alerts, paper orders)."""
    if not auth_on():
        return [get_shared_store()]
    return [user_store(uid) for uid in get_accounts().user_ids() if user_db_path(uid).exists()]


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


_ENGINES: dict[int, AlertEngine] = {}


def alert_engine_for(store: Store) -> AlertEngine:
    eng = _ENGINES.get(id(store))
    if eng is None:
        eng = _ENGINES[id(store)] = AlertEngine(store, get_live_provider(), get_stream_hub(), timeout_s=_timeout())
    return eng


def get_alert_engine(store: Store = Depends(get_store)) -> AlertEngine:
    return alert_engine_for(store)
