"""Hard per-call timeout for blocking provider calls.

The provider call runs on a worker thread and we stop *waiting* for it after
``timeout_s``. Python cannot kill a thread, so a hung call keeps its worker
until it returns -- real providers should also pass their own timeout to their
HTTP client so the worker is released promptly.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Callable, TypeVar

from tools.errors import ToolTimeoutError

R = TypeVar("R")

_POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="tool-call")


def call_with_timeout(fn: Callable[[], R], timeout_s: float, *, tool: str, symbol: str | None) -> R:
    future = _POOL.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeout:
        future.cancel()
        raise ToolTimeoutError(
            f"{tool} did not respond within {timeout_s:.2f}s", tool=tool, symbol=symbol
        ) from None
