"""Prompt text and prompt rendering for the LLM-backed agents."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from core.schemas import AnalysisOutput, Fact, Flag, FreshnessReport

ANALYSIS_SCHEMA = (
    '{"signals": [{"kind": "technical|fundamental|event", "label": "short name", '
    '"direction": "bullish|bearish|neutral", "rationale": "one or two sentences", '
    '"citations": ["C2", "P1"]}], '
    '"overall_stance": "bullish|bearish|neutral|mixed|insufficient_data", '
    '"confidence": "low|medium|high", "caveats": ["..."]}'
)

SUMMARY_SCHEMA = (
    '{"headline": "one sentence, no numbers", '
    '"claims": [{"text": "one short statement", "citations": ["P1", "C3"]}]}'
)

NUMBERS_RULE = (
    "NUMBERS RULE: you may only write a number if it appears in one of the facts you cite "
    "(you may round it). Never calculate new numbers - no differences, ratios, targets or "
    "forecasts. If you cannot support a number with a fact, describe the point qualitatively."
)

FORECAST_RULE = (
    "FORECAST RULE: facts such as 'Statistical N-day direction outlook', 'Model probability of a "
    "higher close', 'Forecast confidence', 'Expected price range' and the indicator readings (RSI, "
    "MACD, ADX, Bollinger, Stochastic, moving averages) come from a deterministic technical-analysis "
    "model with a walk-forward backtest. For anything about future direction use ONLY those facts. "
    "Always state the outlook together with its probability AND the model's backtested hit rate "
    "versus the naive baseline. Never state or imply certainty, never invent price targets, and if "
    "confidence is low say plainly that the model has no demonstrated predictive edge for this stock. "
    "If there is no outlook fact, say that no forecast is available."
)

ANALYSIS_INSTRUCTIONS = [
    "You are the Analysis agent in a stock-analysis pipeline. You interpret data; you never supply data.",
    "You receive FACTS with ids (P1, C2, N1...). They are the ONLY information you may use. "
    "Do not use memory or outside knowledge about this company, its prices or its news.",
    "P = reported price fields, N = reported company announcements, "
    "C = metrics computed by code from the reported data.",
    "Derive technical signals from price facts and computed metrics, and event signals from "
    "announcements. Use kind 'fundamental' only for announcements about earnings, guidance or "
    "dividends. There are no financial statements: never mention P/E, revenue, margins or valuation.",
    "Every signal MUST list the ids of the facts it relies on in 'citations'.",
    NUMBERS_RULE,
    FORECAST_RULE,
    "If DATA QUALITY FLAGS mention missing, stale, contradictory or mismatched-timestamp data, "
    "say so in 'caveats' and lower 'confidence'. Never resolve a contradiction by picking a side.",
    f"Respond with ONLY a JSON object of this shape: {ANALYSIS_SCHEMA}",
]

SUMMARY_INSTRUCTIONS = [
    "You are the Summarization agent in a stock-analysis pipeline. You write the final answer to "
    "the user's question, in plain language, for a non-expert reader.",
    "Use ONLY the FACTS and the ANALYSIS provided. Do not use outside knowledge.",
    "'headline': one sentence that directly answers the question. Do not put numbers in the headline.",
    "'claims': 2 to 7 short statements. Each MUST cite the fact ids it relies on in 'citations'. "
    "Put ids only in 'citations', never inside the text.",
    NUMBERS_RULE,
    FORECAST_RULE,
    "If the question asks for something the facts cannot answer (valuation, forecasts, price "
    "targets, anything outside the data), say plainly that the available data does not cover it.",
    "If a FRESHNESS NOTICE is present, include a claim stating that the datasets were observed at "
    "different times, so they should not be read as equally current.",
    "If DATA QUALITY FLAGS report missing or contradictory data, say so instead of filling the gap.",
    "Do not give personalised investment advice or tell the reader to buy or sell.",
    f"Respond with ONLY a JSON object of this shape: {SUMMARY_SCHEMA}",
]


@dataclass
class AgentContext:
    symbol: str
    question: str
    facts: list[Fact]
    freshness: FreshnessReport | None
    flags: list[Flag]
    missing: list[str] = field(default_factory=list)


def _fact_line(f: Fact) -> str:
    value = f'"{f.display}"' if isinstance(f.value, str) else f.display
    if f.detail:
        value += f" - {f.detail}"
    return (
        f"{f.id} | {f.kind} | {f.label} | {value} | "
        f"observed {f.observed_at.strftime('%Y-%m-%d %H:%M UTC')}"
    )


def render_context(ctx: AgentContext) -> str:
    lines = [f"SYMBOL: {ctx.symbol}", f"QUESTION: {ctx.question}"]
    if ctx.freshness is not None:
        lines.append(f"REFERENCE TIME: {ctx.freshness.reference_time.strftime('%Y-%m-%d %H:%M UTC')}")
        lines.append(f"FRESHNESS NOTICE: {ctx.freshness.notice or 'none'}")
    lines.append("DATASETS NOT AVAILABLE: " + ("; ".join(ctx.missing) if ctx.missing else "none"))
    lines.append("DATA QUALITY FLAGS:")
    lines += [f"- [{f.severity}] {f.code}: {f.message}" for f in ctx.flags] or ["- none"]
    lines.append("FACTS:")
    lines += [_fact_line(f) for f in ctx.facts]
    return "\n".join(lines)


def render_analysis_prompt(ctx: AgentContext) -> str:
    return render_context(ctx) + "\n\nReturn the analysis JSON now."


def render_summary_prompt(ctx: AgentContext, analysis: AnalysisOutput, feedback: str | None) -> str:
    parts = [
        render_context(ctx),
        "ANALYSIS (validated output of the Analysis agent):",
        json.dumps(analysis.model_dump(), indent=1),
    ]
    if feedback:
        parts.append(
            "REVISION REQUIRED - your previous answer had these problems, fix all of them:\n" + feedback
        )
    parts.append("Return the summary JSON now.")
    return "\n\n".join(parts)
