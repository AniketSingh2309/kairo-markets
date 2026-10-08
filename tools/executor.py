"""Reliability policy around the contract tools.

``ToolExecutor`` is the only way the pipeline calls a tool. It adds:

* bounded retries (``tool_max_retries``, max 3) with exponential backoff, only
  for errors marked ``retryable`` (timeouts, source unavailable);
* a per-request ``ToolBudget`` -- every *attempt* that reaches the data source
  consumes one unit, so retries cannot be used to escape the cap;
* a ledger of every attempt (tool, args, outcome, latency, source,
  observed_at) that is returned with the API response for inspection.

One executor instance == one request. It is thread-safe so the data agent can
fan calls out in parallel.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
import uuid
from typing import Any, Callable, Literal

from pydantic import BaseModel

from core.clock import Clock, utc_now
from core.config import Settings
from tools.errors import InvalidInputError, ToolBudgetExceededError, ToolError
from tools.market_tools import TOOL_SPECS, validate_input
from tools.models import ToolResponse
from tools.providers import MarketDataProvider


class ToolCallRecord(BaseModel):
    call_id: str
    tool: str
    arguments: dict[str, Any]
    attempt: int
    status: Literal["ok", "error", "rejected"]
    error_code: str | None = None
    error_message: str | None = None
    started_at: dt.datetime
    latency_ms: float
    source_id: str | None = None
    observed_at: dt.datetime | None = None


class ToolBudget:
    def __init__(self, max_calls: int):
        self.max_calls = max_calls
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self._used)

    def acquire(self, tool: str, symbol: str | None) -> None:
        with self._lock:
            if self._used >= self.max_calls:
                raise ToolBudgetExceededError(
                    f"tool-call budget of {self.max_calls} per request is exhausted; "
                    f"{tool} was not called",
                    tool=tool,
                    symbol=symbol,
                )
            self._used += 1


class ToolExecutor:
    def __init__(
        self,
        *,
        provider: MarketDataProvider,
        settings: Settings,
        clock: Clock = utc_now,
        budget: ToolBudget | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.provider = provider
        self.settings = settings
        self.clock = clock
        self.budget = budget or ToolBudget(settings.max_tool_calls_per_request)
        self._sleep = sleep
        self._ledger: list[ToolCallRecord] = []
        self._lock = threading.Lock()

    @property
    def ledger(self) -> list[ToolCallRecord]:
        with self._lock:
            return list(self._ledger)

    def _record(self, **fields: Any) -> None:
        record = ToolCallRecord(call_id=uuid.uuid4().hex[:10], **fields)
        with self._lock:
            self._ledger.append(record)

    def call(self, tool: str, **arguments: Any) -> ToolResponse[Any]:
        spec = TOOL_SPECS.get(tool)
        if spec is None:
            raise InvalidInputError(f"no such tool {tool!r}", tool=tool)

        # Validate before spending budget; invalid input is never retried.
        try:
            params = validate_input(spec.input_model, tool, **arguments)
        except InvalidInputError as exc:
            self._record(
                tool=tool,
                arguments={k: str(v) for k, v in arguments.items()},
                attempt=0,
                status="rejected",
                error_code=exc.code,
                error_message=exc.message,
                started_at=utc_now(),
                latency_ms=0.0,
            )
            raise

        args_json = params.model_dump(mode="json")
        symbol: str = params.symbol  # type: ignore[attr-defined]
        max_attempts = 1 + self.settings.tool_max_retries

        for attempt in range(1, max_attempts + 1):
            try:
                self.budget.acquire(tool, symbol)
            except ToolBudgetExceededError as exc:
                self._record(
                    tool=tool,
                    arguments=args_json,
                    attempt=attempt,
                    status="rejected",
                    error_code=exc.code,
                    error_message=exc.message,
                    started_at=utc_now(),
                    latency_ms=0.0,
                )
                raise

            started_at = utc_now()
            t0 = time.perf_counter()
            try:
                response = spec.fn(
                    **params.model_dump(),
                    provider=self.provider,
                    timeout_s=self.settings.tool_timeout_seconds,
                    clock=self.clock,
                )
            except ToolError as exc:
                self._record(
                    tool=tool,
                    arguments=args_json,
                    attempt=attempt,
                    status="error",
                    error_code=exc.code,
                    error_message=exc.message,
                    started_at=started_at,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )
                if not exc.retryable or attempt == max_attempts:
                    raise
                self._sleep(self.settings.tool_backoff_base_seconds * (2 ** (attempt - 1)))
                continue

            self._record(
                tool=tool,
                arguments=args_json,
                attempt=attempt,
                status="ok",
                started_at=started_at,
                latency_ms=(time.perf_counter() - t0) * 1000,
                source_id=response.source.source_id,
                observed_at=response.observed_at,
            )
            return response

        raise AssertionError("unreachable")  # pragma: no cover
