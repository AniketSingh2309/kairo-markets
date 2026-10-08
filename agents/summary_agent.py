"""Summarization agent: writes the user-facing answer as cited claims."""

from __future__ import annotations

from agents.llm import LLMUsage, build_agent, run_json
from agents.prompts import SUMMARY_INSTRUCTIONS, AgentContext, render_summary_prompt
from core.config import Settings
from core.schemas import AnalysisOutput, SummaryOutput


class SummaryAgent:
    name = "summary"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.generated_by = f"groq:{settings.groq_model}"

    def run(
        self,
        ctx: AgentContext,
        analysis: AnalysisOutput,
        usage: LLMUsage,
        feedback: str | None = None,
    ) -> SummaryOutput:
        agent = build_agent(
            name="summary-agent",
            instructions=SUMMARY_INSTRUCTIONS,
            settings=self.settings,
            json_mode=True,
        )
        return run_json(agent, render_summary_prompt(ctx, analysis, feedback), SummaryOutput,
                        usage=usage, settings=self.settings)
