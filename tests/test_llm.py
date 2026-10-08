"""LLM plumbing: JSON extraction, bounded retries, kwarg filtering (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.llm import (
    LLMOutputError,
    LLMUnavailableError,
    LLMUsage,
    _construct,
    extract_json,
    run_json,
)
from core.schemas import SummaryOutput

GOOD = '{"headline": "Up", "claims": [{"text": "Closed higher.", "citations": ["P1"]}]}'


class ScriptedAgent:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def run(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=reply, status="COMPLETED")


@pytest.mark.parametrize("text", [GOOD, f"```json\n{GOOD}\n```", f"Sure! Here it is:\n{GOOD}\nThanks"])
def test_extract_json(text):
    assert extract_json(text)["headline"] == "Up"


def test_extract_json_rejects_non_json():
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_run_json_reprompts_after_schema_error(settings):
    agent = ScriptedAgent('{"headline": "Up"}', GOOD)
    usage = LLMUsage()
    out = run_json(agent, "PROMPT", SummaryOutput, usage=usage, settings=settings, sleep=lambda _s: None)
    assert out.claims[0].citations == ["P1"]
    assert usage.llm_calls == 2
    assert "REJECTED" in agent.prompts[1]


def test_run_json_gives_up_after_max_attempts(settings):
    agent = ScriptedAgent("nope", "still nope", GOOD)
    with pytest.raises(LLMOutputError):
        run_json(agent, "P", SummaryOutput, usage=LLMUsage(), settings=settings, sleep=lambda _s: None)
    assert len(agent.prompts) == settings.llm_max_attempts


def test_run_json_backs_off_on_api_errors(settings):
    sleeps: list[float] = []
    agent = ScriptedAgent(RuntimeError("429"), RuntimeError("429"))
    with pytest.raises(LLMUnavailableError):
        run_json(agent, "P", SummaryOutput, usage=LLMUsage(), settings=settings, sleep=sleeps.append)
    assert sleeps == [settings.llm_backoff_base_seconds]


def test_construct_drops_unknown_and_none_kwargs():
    class Target:
        def __init__(self, a, b=1):
            self.a, self.b = a, b

    obj = _construct(Target, a=5, b=None, not_a_param="x")
    assert (obj.a, obj.b) == (5, 1)


def test_rate_limit_hint_is_honoured(settings):
    sleeps: list[float] = []
    agent = ScriptedAgent(RuntimeError("Rate limit reached. Please try again in 11.97s."), GOOD)
    run_json(agent, "P", SummaryOutput, usage=LLMUsage(), settings=settings, sleep=sleeps.append)
    assert sleeps == [pytest.approx(12.47)]


def test_rate_limit_hint_is_capped(settings):
    sleeps: list[float] = []
    agent = ScriptedAgent(RuntimeError("try again in 5m3s"), GOOD)
    run_json(agent, "P", SummaryOutput, usage=LLMUsage(), settings=settings, sleep=sleeps.append)
    assert sleeps == [30.0]
