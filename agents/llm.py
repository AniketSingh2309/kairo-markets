"""Agno + Groq plumbing shared by the LLM-backed agents.

We parse and validate JSON ourselves (instead of relying on a framework's
structured-output mode) because Llama 3.3 on Groq supports JSON mode but not
strict schema enforcement, and because it keeps behaviour identical across
Agno versions. Every call is bounded: at most ``llm_max_attempts`` model calls
per agent invocation, with backoff on API errors and a corrective re-prompt on
schema errors.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from core.config import Settings

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)


class LLMError(Exception):
    pass


class LLMUnavailableError(LLMError):
    """Not configured, not installed, or the API kept failing."""


class LLMOutputError(LLMError):
    """The model answered but never produced schema-valid JSON."""


@dataclass
class LLMUsage:
    llm_calls: int = 0


def _construct(cls: Any, **kwargs: Any) -> Any:
    """Instantiate ``cls`` passing only the kwargs its signature accepts.

    Keeps us compatible across Agno releases that add/rename optional params.
    """
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return cls(**kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return cls(**kwargs)
    return cls(**{k: v for k, v in kwargs.items() if k in params})


def _request_params(settings: Settings, json_mode: bool) -> dict[str, Any] | None:
    params: dict[str, Any] = {}
    if json_mode:
        params["response_format"] = {"type": "json_object"}  # Groq JSON mode
    if settings.llm_reasoning_effort:
        params["reasoning_effort"] = settings.llm_reasoning_effort
    return params or None


def build_agent(
    *,
    name: str,
    instructions: list[str],
    settings: Settings,
    tools: list[Callable[..., Any]] | None = None,
    tool_call_limit: int | None = None,
    json_mode: bool = False,
) -> Any:
    if settings.groq_api_key is None:
        raise LLMUnavailableError("GROQ_API_KEY is not set (see .env.example)")
    try:
        from agno.agent import Agent
        from agno.models.groq import Groq
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise LLMUnavailableError(f"agno/groq are not installed: {exc}") from exc

    model = _construct(
        Groq,
        id=settings.groq_model,
        api_key=settings.groq_api_key.get_secret_value(),
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_seconds,
        max_retries=0,  # retries are handled (and bounded) by run_json below
        max_tokens=settings.llm_max_tokens,
        # Agno merges request_params into the chat.completions call.
        request_params=_request_params(settings, json_mode),
    )
    return _construct(
        Agent,
        name=name,
        model=model,
        instructions=instructions,
        tools=tools,
        tool_call_limit=tool_call_limit,
        markdown=False,
        telemetry=False,
    )


def _run_once(agent: Any, prompt: str) -> str:
    response = agent.run(prompt)
    status = str(getattr(response, "status", "") or "").lower()
    if status.endswith("error"):
        raise LLMUnavailableError(f"agent run failed: {getattr(response, 'content', '')}")
    content = getattr(response, "content", response)
    if isinstance(content, BaseModel):
        return content.model_dump_json()
    if isinstance(content, (dict, list)):
        return json.dumps(content)
    if content is None:
        raise LLMOutputError("model returned no content")
    return str(content)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    fenced = _FENCE_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    return json.loads(candidate[start : end + 1])


_RETRY_AFTER_RE = re.compile(r"try again in (?:(\d+)m)?([\d.]+)s", re.IGNORECASE)
_MAX_RETRY_AFTER_S = 30.0


def _retry_delay(exc: Exception, attempt: int, settings: Settings) -> float:
    """Honour a provider's "try again in Ns" rate-limit hint (capped); else exponential backoff."""
    backoff = settings.llm_backoff_base_seconds * (2 ** (attempt - 1))
    match = _RETRY_AFTER_RE.search(str(exc))
    if match:
        hinted = int(match.group(1) or 0) * 60 + float(match.group(2)) + 0.5
        return min(max(hinted, backoff), _MAX_RETRY_AFTER_S)
    return backoff


def run_agent(agent: Any, prompt: str, *, usage: LLMUsage, settings: Settings,
              sleep: Callable[[float], None] = time.sleep) -> str:
    """Free-text run with bounded retries on API errors."""
    last: Exception | None = None
    for attempt in range(1, settings.llm_max_attempts + 1):
        usage.llm_calls += 1
        try:
            return _run_once(agent, prompt)
        except Exception as exc:  # noqa: BLE001 - provider SDKs raise many types
            last = exc
            log.warning("LLM call failed (attempt %d): %s", attempt, exc)
            if attempt < settings.llm_max_attempts:
                sleep(_retry_delay(exc, attempt, settings))
    raise LLMUnavailableError(f"LLM call failed after {settings.llm_max_attempts} attempts: {last}")


def run_json(agent: Any, prompt: str, schema: type[M], *, usage: LLMUsage, settings: Settings,
             sleep: Callable[[float], None] = time.sleep) -> M:
    """Run ``agent`` and parse its reply into ``schema``; bounded retries."""
    current = prompt
    last: LLMError | None = None
    for attempt in range(1, settings.llm_max_attempts + 1):
        usage.llm_calls += 1
        try:
            text = _run_once(agent, current)
        except LLMOutputError as exc:
            last = exc
            continue
        except Exception as exc:  # noqa: BLE001
            last = LLMUnavailableError(f"LLM API error: {exc}")
            log.warning("LLM call failed (attempt %d): %s", attempt, exc)
            if attempt < settings.llm_max_attempts:
                sleep(_retry_delay(exc, attempt, settings))
            continue
        try:
            return schema.model_validate(extract_json(text))
        except (ValueError, ValidationError) as exc:
            detail = str(exc)[:600]
            last = LLMOutputError(f"output did not match {schema.__name__}: {detail}")
            log.warning("LLM output rejected (attempt %d): %s", attempt, detail)
            current = (
                f"{prompt}\n\nYOUR PREVIOUS REPLY WAS REJECTED: {detail}\n"
                "Reply again with ONLY a single JSON object that matches the schema."
            )
    assert last is not None
    raise last
