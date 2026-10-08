"""Data-fetching agent.

Two modes, both going through the same ``ToolExecutor`` (so retries, the
per-request call budget and the ledger always apply):

* ``deterministic`` (default): the orchestrator's plan is executed as-is, in
  parallel. No model decides what to fetch, so no model can fetch the wrong
  thing or loop.
* ``llm``: an Agno agent on Groq is given the three tools and decides the
  calls itself (``tool_call_limit`` = request budget). Only results whose
  arguments exactly match the plan are accepted into the bundle, calls for
  other symbols are refused, and any planned call the model skipped is then
  executed deterministically. Downstream stages only ever see real tool
  results -- never the model's paraphrase of them.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from agents.llm import LLMError, LLMUsage, build_agent, run_agent
from core.bundle import DATASETS, DataBundle, PlannedCall
from core.config import Settings
from core.schemas import AnalyzeRequest
from tools.errors import ToolError
from tools.executor import ToolExecutor
from tools.market_tools import TOOL_SPECS, validate_input
from tools.models import PERIOD_DAYS

# Recent-window metrics need ~20 trading days, so the window is never shorter than 30d.
MIN_WINDOW_PERIOD = "30d"
# The forecast needs a long series: 200-day averages plus enough history to backtest.
FORECAST_HISTORY_PERIOD = "2y"

DATA_AGENT_INSTRUCTIONS = [
    "You are the Data-fetching agent in a stock-analysis pipeline.",
    "Call the tools needed to answer the question: the latest (or requested-date) price via "
    "get_stock_price, recent price history via get_price_history, and company announcements via "
    "get_announcements. Use exactly the symbol, date and periods given in the request.",
    "Call each tool at most once unless it returned a retryable error. Do not analyse the data. "
    "When you are done, reply with the single word DONE.",
]


def build_plan(request: AnalyzeRequest) -> list[PlannedCall]:
    history_period = max(request.period, FORECAST_HISTORY_PERIOD, key=lambda p: PERIOD_DAYS[p])
    return [
        PlannedCall("get_stock_price", {
            "symbol": request.symbol,
            "date": request.date.isoformat() if request.date else None,
        }),
        PlannedCall("get_price_history", {"symbol": request.symbol, "period": history_period}),
        PlannedCall("get_announcements", {"symbol": request.symbol, "period": request.period}),
    ]


def window_days(request: AnalyzeRequest) -> int:
    return PERIOD_DAYS[max(request.period, MIN_WINDOW_PERIOD, key=lambda p: PERIOD_DAYS[p])]


class DataFetchAgent:
    name = "data_fetcher"

    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, request: AnalyzeRequest, executor: ToolExecutor, usage: LLMUsage) -> DataBundle:
        plan = build_plan(request)
        bundle = DataBundle(symbol=request.symbol, plan=plan, mode=self.settings.data_agent_mode,
                            window_days=window_days(request))
        if self.settings.data_agent_mode == "llm":
            self._run_llm(request, plan, bundle, executor, usage)
        else:
            self._run_plan(plan, bundle, executor)
        return bundle

    # -- deterministic -----------------------------------------------------

    @staticmethod
    def _run_plan(plan: list[PlannedCall], bundle: DataBundle, executor: ToolExecutor) -> None:
        def fetch(call: PlannedCall) -> tuple[PlannedCall, Any]:
            try:
                return call, executor.call(call.tool, **call.arguments)
            except ToolError as exc:
                return call, exc

        with ThreadPoolExecutor(max_workers=len(plan) or 1, thread_name_prefix="data-agent") as pool:
            for call, outcome in pool.map(fetch, plan):
                if isinstance(outcome, ToolError):
                    bundle.errors[call.tool] = outcome
                else:
                    bundle.store(call.tool, outcome)

    # -- LLM-planned -------------------------------------------------------

    def _run_llm(self, request: AnalyzeRequest, plan: list[PlannedCall], bundle: DataBundle,
                 executor: ToolExecutor, usage: LLMUsage) -> None:
        planned = {c.tool: c for c in plan}

        def invoke(tool: str, **arguments: Any) -> str:
            try:
                params = validate_input(TOOL_SPECS[tool].input_model, tool, **arguments)
            except ToolError as exc:
                return json.dumps({"error": exc.code, "message": exc.message})
            if params.symbol != request.symbol:  # type: ignore[attr-defined]
                return json.dumps({"error": "SYMBOL_NOT_ALLOWED",
                                   "message": f"only {request.symbol} may be fetched"})
            try:
                response = executor.call(tool, **arguments)
            except ToolError as exc:
                bundle.errors.setdefault(tool, exc)
                return json.dumps({"error": exc.code, "message": exc.message})
            expected = validate_input(TOOL_SPECS[tool].input_model, tool, **planned[tool].arguments)
            if params == expected:
                bundle.store(tool, response)
            else:
                bundle.notes.append(f"{tool} called with {params.model_dump(mode='json')}, "
                                    f"plan required {expected.model_dump(mode='json')}; result ignored")
            return json.dumps({"ok": True, "source": response.source.source_id,
                               "observed_at": response.observed_at.isoformat()})

        tools = _make_agno_tools(invoke)
        prompt = (
            f"Symbol: {request.symbol}\nQuestion: {request.question}\n"
            f"Price date: {request.date.isoformat() if request.date else 'latest (omit date)'}\n"
            f"History period: {planned['get_price_history'].arguments['period']}\n"
            f"Announcements period: {request.period}"
        )
        try:
            agent = build_agent(name="data-agent", instructions=DATA_AGENT_INSTRUCTIONS,
                                settings=self.settings, tools=tools,
                                tool_call_limit=self.settings.max_tool_calls_per_request)
            run_agent(agent, prompt, usage=usage, settings=self.settings)
        except LLMError as exc:
            bundle.notes.append(f"LLM data planner unavailable ({exc}); used deterministic plan")

        # Gap-fill: execute planned calls the model skipped or whose result was not accepted.
        gaps = [c for c in plan if bundle.get(c.tool) is None and c.tool not in bundle.errors]
        if gaps:
            bundle.notes.append("deterministic gap-fill for: " + ", ".join(c.tool for c in gaps))
            self._run_plan(gaps, bundle, executor)


def _make_agno_tools(invoke: Callable[..., str]) -> list[Callable[..., str]]:
    # Plain functions with type hints + docstrings: Agno turns them into tool schemas.
    def get_stock_price(symbol: str, date: str | None = None) -> str:
        """Get the daily OHLCV price bar for a stock.

        Args:
            symbol: Ticker symbol, e.g. AAPL.
            date: Trading day as YYYY-MM-DD. Omit for the latest bar.
        """
        return invoke("get_stock_price", symbol=symbol, date=date)

    def get_price_history(symbol: str, period: str = "30d") -> str:
        """Get daily price bars for a stock over a period.

        Args:
            symbol: Ticker symbol, e.g. AAPL.
            period: One of 7d, 30d, 90d, 180d, 1y.
        """
        return invoke("get_price_history", symbol=symbol, period=period)

    def get_announcements(symbol: str, period: str = "30d") -> str:
        """Get company announcements for a stock over a period.

        Args:
            symbol: Ticker symbol, e.g. AAPL.
            period: One of 7d, 30d, 90d, 180d, 1y.
        """
        return invoke("get_announcements", symbol=symbol, period=period)

    return [get_stock_price, get_price_history, get_announcements]


__all__ = ["DATASETS", "DataFetchAgent", "build_plan"]
