"""Render the final user-facing report.

The layout is produced by code so the separation between source data and
model output cannot be blurred by the model:

  1. freshness notice (verbatim, if any)       -- code
  2. data-quality flags                         -- code
  3. generated interpretation (headline/claims) -- LLM, every line cited
  4. reported facts table                       -- tool data
  5. computed metrics table                     -- code
  6. sources
"""

from __future__ import annotations

from core.schemas import AnalyzeResponse, Fact

_SEVERITY_ICON = {"critical": "[CRITICAL]", "warning": "[WARNING]", "info": "[INFO]"}


def _cite(ids: list[str]) -> str:
    return " " + "".join(f"[{i}]" for i in ids) if ids else ""


def _fact_table(facts: list[Fact], with_method: bool = False) -> list[str]:
    head = "| ID | Fact | Value | Source | Observed at |"
    sep = "|---|---|---|---|---|"
    if with_method:
        head = "| ID | Metric | Value | Method | Source | Observed at |"
        sep = "|---|---|---|---|---|---|"
    lines = [head, sep]
    for f in facts:
        value = f.display.replace("|", "/")
        if f.detail:
            value += f" - {f.detail.replace('|', '/')}"
        observed = f.observed_at.strftime("%Y-%m-%d %H:%M UTC")
        if with_method:
            lines.append(f"| {f.id} | {f.label} | {value} | {f.method or ''} | `{f.source_id}` | {observed} |")
        else:
            lines.append(f"| {f.id} | {f.label} | {value} | `{f.source_id}` | {observed} |")
    return lines


def render_markdown(resp: AnalyzeResponse) -> str:
    out: list[str] = [f"# {resp.symbol}: {resp.question}", ""]
    out.append(f"_Status: **{resp.status}** | request `{resp.request_id}`_")
    out.append("")

    if resp.freshness and resp.freshness.notice:
        out += [f"> **{resp.freshness.notice}**", ""]

    if resp.flags:
        out.append("## Data quality flags")
        out += [f"- {_SEVERITY_ICON[f.severity]} `{f.code}`: {f.message}" for f in resp.flags]
        out.append("")

    out.append("## Answer (generated interpretation)")
    if resp.answer:
        a = resp.answer
        out.append(
            f"_Written by `{a.generated_by}` from the facts below. This is interpretation, "
            "not source data; each statement cites the facts it relies on._"
        )
        out += ["", f"**{a.headline}**", ""]
        out += [f"- {c.text}{_cite(c.citations)}" for c in a.claims]
        if a.signals:
            out += ["", f"Signals (overall stance: **{a.overall_stance}**, confidence: **{a.confidence}**):"]
            out += [
                f"- _{s.kind}_ / {s.direction}: {s.label} - {s.rationale}{_cite(s.citations)}"
                for s in a.signals
            ]
        if a.caveats:
            out += ["", "Caveats:"] + [f"- {c}" for c in a.caveats]
    else:
        out.append("_No generated interpretation is available for this request (see flags)._")
    out.append("")

    fc = resp.forecast
    if fc is not None:
        out += [f"## {fc.horizon_days}-day directional outlook (statistical model, not a guarantee)", ""]
        out.append(f"- Outlook as of {fc.as_of}: **{fc.direction.replace('_', ' ').upper()}**, "
                   f"P(higher close) = {fc.probability_up:.1%}, confidence **{fc.confidence}** "
                   f"({fc.confidence_reason})")
        out.append(f"- Expected range (1 sigma): {fc.expected_low:.2f} to {fc.expected_high:.2f} "
                   f"{fc.currency} (+/-{fc.sigma_pct:.2f}%)")
        out.append(f"- Model: {fc.model_used}; indicator consensus {fc.consensus_score:+.2f}")
        if fc.backtest is not None:
            bt = fc.backtest
            rates = ", ".join(f"{m.model} {m.hit_rate:.1%} (95% CI {m.ci_low:.1%}-{m.ci_high:.1%})"
                              for m in bt.models if m.hit_rate is not None)
            out.append(f"- Backtest ({bt.method}; {bt.models[0].predictions} predictions, "
                       f"{bt.first_test_date} to {bt.last_test_date}): {rates}; naive baseline "
                       f"{bt.baseline_hit_rate:.1%}; stock rose in {bt.up_rate:.1%} of windows")
        out += ["", "| Technique | Value | Reading | Vote | Weight here | Hit rate here |", "|---|---|---|---|---|---|"]
        for r in fc.indicators:
            out.append(f"| {r.name} | {r.value} | {r.reading} | {r.vote:+.2f} | "
                       f"{'' if r.weight is None else f'{r.weight:+.2f}'} | "
                       f"{'' if r.hit_rate is None else f'{r.hit_rate:.0%} (n={r.samples})'} |")
        out += ["", f"_{fc.disclaimer}_", ""]

    if resp.reported_facts:
        out += ["## Reported facts (from source data)", ""]
        out += _fact_table(resp.reported_facts)
        out.append("")
    if resp.computed_metrics:
        out += ["## Computed metrics (deterministic, derived by code from reported data)", ""]
        out += _fact_table(resp.computed_metrics, with_method=True)
        out.append("")

    if resp.freshness and resp.freshness.datasets:
        out += ["## Data freshness", "", "| Dataset | Observed at | Age (hours) | Stale |", "|---|---|---|---|"]
        for d in resp.freshness.datasets:
            out.append(
                f"| {d.dataset} | {d.observed_at.strftime('%Y-%m-%d %H:%M UTC')} | {d.age_hours} | "
                f"{'historical (requested date)' if d.historical else 'yes' if d.stale else 'no'} |"
            )
        out.append("")

    if resp.sources:
        out += ["## Sources", ""] + [f"- `{s.source_id}` ({s.provider})" for s in resp.sources]
        out.append("")
    return "\n".join(out)
