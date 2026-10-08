"""Analysis agent: interprets facts into technical / fundamental / event signals."""

from __future__ import annotations

from agents.llm import LLMUsage, build_agent, run_json
from agents.prompts import ANALYSIS_INSTRUCTIONS, AgentContext, render_analysis_prompt
from core.config import Settings
from core.schemas import AnalysisOutput


class AnalysisAgent:
    name = "analysis"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.generated_by = f"groq:{settings.groq_model}"

    def run(self, ctx: AgentContext, usage: LLMUsage) -> AnalysisOutput:
        agent = build_agent(
            name="analysis-agent",
            instructions=ANALYSIS_INSTRUCTIONS,
            settings=self.settings,
            json_mode=True,
        )
        return run_json(agent, render_analysis_prompt(ctx), AnalysisOutput,
                        usage=usage, settings=self.settings)
