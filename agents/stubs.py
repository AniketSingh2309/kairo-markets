"""Deterministic, rule-based stand-ins for the LLM agents (LLM_PROVIDER=stub).

They exercise the full pipeline -- including validation and rendering --
without network access, so unit tests and the offline eval are reproducible.
They are not meant to be insightful.
"""

from __future__ import annotations

from agents.llm import LLMUsage
from agents.prompts import AgentContext
from core.schemas import AnalysisOutput, Claim, Fact, Signal, SummaryOutput


def _find(facts: list[Fact], prefix: str) -> Fact | None:
    return next((f for f in facts if f.label.startswith(prefix)), None)


def _direction(value: float, threshold: float) -> str:
    return "bullish" if value > threshold else "bearish" if value < -threshold else "neutral"


class StubAnalysisAgent:
    name = "analysis"
    generated_by = "stub:rule-based"

    def run(self, ctx: AgentContext, usage: LLMUsage) -> AnalysisOutput:
        facts = ctx.facts
        signals: list[Signal] = []
        change = _find(facts, "Close-to-close change")
        if change is not None:
            signals.append(Signal(
                kind="technical", label="Window price trend",
                direction=_direction(float(change.value), 2.0),
                rationale=f"The close moved {change.display} across the price window.",
                citations=[change.id],
            ))
        gap = _find(facts, "Latest close relative to 20-day SMA")
        if gap is not None:
            side = "above" if float(gap.value) >= 0 else "below"
            signals.append(Signal(
                kind="technical", label=f"Close {side} 20-day average",
                direction=_direction(float(gap.value), 1.0),
                rationale=f"The latest close is {gap.display} relative to its 20-day simple moving average.",
                citations=[gap.id],
            ))
        for fact in (f for f in facts if f.id.startswith("N")):
            signals.append(Signal(
                kind="event", label="Company announcement", direction="neutral",
                rationale=f"Announcement: {fact.display}", citations=[fact.id],
            ))

        technical = [s.direction for s in signals if s.kind == "technical"]
        if not technical:
            stance = "insufficient_data"
        elif all(d == technical[0] for d in technical):
            stance = technical[0]
        else:
            stance = "mixed"
        caveats = [f"{f.code}: {f.message}" for f in ctx.flags if f.severity != "info"]
        confidence = "low" if caveats or stance == "insufficient_data" else "medium"
        return AnalysisOutput(signals=signals, overall_stance=stance, confidence=confidence,
                              caveats=caveats)


class StubSummaryAgent:
    name = "summary"
    generated_by = "stub:rule-based"

    def run(self, ctx: AgentContext, analysis: AnalysisOutput, usage: LLMUsage,
            feedback: str | None = None) -> SummaryOutput:
        facts = ctx.facts
        claims: list[Claim] = []
        closes = [f for f in facts if f.label.startswith("Close price on")]
        if closes:
            claims.append(Claim(text=f"The most recent reported close was {closes[0].display}.",
                                citations=[closes[0].id]))
        for other in closes[1:]:
            claims.append(Claim(
                text=f"A second feed reports {other.display} for the same day, so the price is disputed.",
                citations=[other.id],
            ))
        change = _find(facts, "Close-to-close change")
        if change is not None:
            claims.append(Claim(text=f"Across the price window the close changed by {change.display}.",
                                citations=[change.id]))
        gap = _find(facts, "Latest close relative to 20-day SMA")
        if gap is not None:
            claims.append(Claim(text=f"The latest close is {gap.display} versus its 20-day average.",
                                citations=[gap.id]))
        announcements = [f for f in facts if f.id.startswith("N")]
        if announcements:
            latest = announcements[-1]
            claims.append(Claim(text=f"Most recent announcement: {latest.display}.",
                                citations=[latest.id]))
        outlook = _find(facts, "Statistical ")
        if outlook is not None:
            prob = _find(facts, "Model probability of a higher close")
            hit = _find(facts, "Backtested hit rate of the forecast model")
            base = _find(facts, "Backtested hit rate of a naive baseline")
            text = f"The statistical outlook is {outlook.display}"
            cites = [outlook.id]
            if prob is not None:
                text += f" with a {prob.display} modelled chance of a higher close"
                cites.append(prob.id)
            if hit is not None and base is not None:
                text += f"; in backtesting it was right {hit.display} of the time versus {base.display} for a naive baseline"
                cites += [hit.id, base.id]
            claims.append(Claim(text=text + ".", citations=cites))
        if ctx.freshness is not None and ctx.freshness.discrepancy:
            anchors = [f.id for f in (closes[:1] + announcements[:1])] or [facts[0].id]
            claims.append(Claim(
                text="Price data and announcements were observed at different times, "
                     "so they should not be read as equally current.",
                citations=anchors,
            ))
        if not closes:
            claims.append(Claim(
                text="No usable price data was retrieved, so no price-based view is given.",
                citations=[facts[0].id],
            ))
        headline = {
            "bullish": f"{ctx.symbol} shows a positive technical picture in the available data",
            "bearish": f"{ctx.symbol} shows a negative technical picture in the available data",
            "neutral": f"{ctx.symbol} shows no strong technical direction in the available data",
            "mixed": f"{ctx.symbol} shows mixed technical signals in the available data",
            "insufficient_data": f"There is not enough price data to characterise {ctx.symbol}",
        }[analysis.overall_stance]
        return SummaryOutput(headline=headline, claims=claims[:8])
