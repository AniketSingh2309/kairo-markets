"""Output validation: citations must resolve, numbers must be grounded.

Rules applied to every piece of model-generated text:

1. Citations must reference fact ids that exist. Unknown ids are dropped; an
   item left with no valid citation is rejected.
2. Every number in the text must match a number in one of the *cited* facts
   (allowing for the rounding implied by how many decimals were written, and
   million/billion suffixes). If the number exists in a different fact, that
   fact is added to the citations (a logged auto-fix). If it exists in no fact
   at all it is treated as fabricated and the item is rejected.

This is intentionally strict: the LLM is told it may only restate numbers
that appear in facts, never compute new ones.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from core.facts import FactRegistry
from core.schemas import AnalysisOutput, Claim, Fact, RemovedItem, SummaryOutput

_NUMBER_RE = re.compile(
    r"(?<![\w.])(?P<sign>[-+])?\$?(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"(?:\s*(?P<word>million|billion|thousand|mn|bn)\b|\s?(?P<abbr>[MBK])\b)?",
    re.IGNORECASE,
)
_EMBEDDED_DIGITS_RE = re.compile(r"(?<=[A-Za-z])\d+(?:\.\d+)?")
_CITATION_RE = re.compile(r"\[?\b[PCN]\d{1,3}\b\]?")
_MULTIPLIERS = {
    "thousand": 1e3, "k": 1e3,
    "million": 1e6, "mn": 1e6, "m": 1e6,
    "billion": 1e9, "bn": 1e9, "b": 1e9,
}


@dataclass(frozen=True)
class ExtractedNumber:
    raw: str
    value: float
    decimals: int
    multiplier: float = 1.0

    @property
    def scaled(self) -> float:
        return self.value * self.multiplier

    def matches(self, target: float) -> bool:
        # Accept any value that rounds to what was written, e.g. "7%" for 7.28,
        # "246.1" for 246.10, "50.9 million" for 50,870,000. Sign is ignored
        # because "fell 3.5%" legitimately restates -3.5.
        tolerance = 0.5 * (10 ** -self.decimals) * self.multiplier + 1e-9
        return abs(abs(self.scaled) - abs(target)) <= tolerance


def strip_citations(text: str) -> str:
    return _CITATION_RE.sub("", text)


def extract_numbers(text: str) -> list[ExtractedNumber]:
    found = []
    for m in _NUMBER_RE.finditer(text):
        digits = m.group("int").replace(",", "") + (m.group("frac") or "")
        suffix = (m.group("word") or m.group("abbr") or "").lower()
        found.append(ExtractedNumber(
            raw=m.group(0).strip(),
            value=float(digits),
            decimals=len(m.group("frac")) - 1 if m.group("frac") else 0,
            multiplier=_MULTIPLIERS.get(suffix, 1.0),
        ))
    return found


def fact_numbers(fact: Fact) -> list[float]:
    nums: list[float] = []
    if isinstance(fact.value, (int, float)) and not isinstance(fact.value, bool):
        nums.append(float(fact.value))
    texts = [fact.label, fact.display, fact.detail or ""]
    if isinstance(fact.value, str):
        texts.append(fact.value)
    for text in texts:
        nums += [n.scaled for n in extract_numbers(text)]
        # Digits glued to letters ("SMA200", "FY2026") are still literally present in the
        # fact, so the model may restate them ("200-day average").
        nums += [float(d) for d in _EMBEDDED_DIGITS_RE.findall(text)]
    return nums


@dataclass
class TextCheck:
    citations: list[str]
    problems: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class Grounder:
    """Checks generated text against a fact registry."""

    def __init__(self, registry: FactRegistry, extra_texts: Iterable[str] = ()):
        self.registry = registry
        self._fact_nums = {f.id: fact_numbers(f) for f in registry.all()}
        # Numbers the model may repeat without a fact citation (e.g. from the
        # freshness notice or the user's own question).
        self._extra = [n.scaled for t in extra_texts for n in extract_numbers(t)]

    def _supporting_facts(self, num: ExtractedNumber) -> list[str]:
        hits = [fid for fid, nums in self._fact_nums.items() if any(num.matches(t) for t in nums)]
        # Prefer reported facts over computed ones when auto-citing.
        return sorted(hits, key=lambda fid: (fid[0] == "C", fid))

    def ungrounded_numbers(self, text: str) -> list[str]:
        return [
            n.raw
            for n in extract_numbers(strip_citations(text))
            if not self._supporting_facts(n) and not any(n.matches(t) for t in self._extra)
        ]

    def check(self, text: str, citations: list[str], *, require_citations: bool = True) -> TextCheck:
        unique = list(dict.fromkeys(citations))
        valid = [c for c in unique if c in self.registry]
        unknown = [c for c in unique if c not in self.registry]
        result = TextCheck(citations=valid)
        if unknown:
            result.fixes.append(f"dropped unknown citation id(s) {', '.join(unknown)}")
        if require_citations and not valid:
            result.problems.append("no valid fact citations")

        for num in extract_numbers(strip_citations(text)):
            cited = any(num.matches(t) for c in result.citations for t in self._fact_nums[c])
            if cited or any(num.matches(t) for t in self._extra):
                continue
            support = self._supporting_facts(num)
            if not support:
                result.problems.append(f"number '{num.raw}' does not appear in any source fact")
            elif require_citations:
                result.citations.append(support[0])
                result.fixes.append(f"added citation {support[0]} for number '{num.raw}'")
        return result


@dataclass
class ValidatedAnalysis:
    output: AnalysisOutput
    removed: list[RemovedItem]
    fixes: list[str]


@dataclass
class ValidatedSummary:
    headline: str | None
    claims: list[Claim]
    removed: list[RemovedItem]
    fixes: list[str]

    @property
    def problems(self) -> list[str]:
        return [f"{r.where} '{r.text[:80]}': {'; '.join(r.reasons)}" for r in self.removed]


def validate_analysis(analysis: AnalysisOutput, grounder: Grounder) -> ValidatedAnalysis:
    signals, removed, fixes = [], [], []
    for signal in analysis.signals:
        check = grounder.check(f"{signal.label}. {signal.rationale}", signal.citations)
        if check.ok:
            signals.append(signal.model_copy(update={"citations": check.citations}))
            fixes += [f"signal '{signal.label[:60]}': {fix}" for fix in check.fixes]
        else:
            removed.append(RemovedItem(where="signal", text=f"{signal.label}: {signal.rationale}",
                                       reasons=check.problems))
    caveats = []
    for caveat in analysis.caveats:
        bad = grounder.ungrounded_numbers(caveat)
        if bad:
            removed.append(RemovedItem(where="caveat", text=caveat,
                                       reasons=[f"caveat contains ungrounded number(s) {bad}"]))
        else:
            caveats.append(caveat)
    output = analysis.model_copy(update={"signals": signals, "caveats": caveats})
    return ValidatedAnalysis(output=output, removed=removed, fixes=fixes)


def validate_summary(summary: SummaryOutput, grounder: Grounder) -> ValidatedSummary:
    removed, fixes, claims = [], [], []
    headline: str | None = summary.headline
    bad = grounder.ungrounded_numbers(summary.headline)
    if bad:
        removed.append(RemovedItem(where="headline", text=summary.headline,
                                   reasons=[f"ungrounded number(s) {bad}"]))
        headline = None
    for claim in summary.claims:
        check = grounder.check(claim.text, claim.citations)
        if check.ok:
            claims.append(Claim(text=claim.text, citations=check.citations))
            fixes += [f"claim '{claim.text[:60]}': {fix}" for fix in check.fixes]
        else:
            removed.append(RemovedItem(where="claim", text=claim.text, reasons=check.problems))
    return ValidatedSummary(headline=headline, claims=claims, removed=removed, fixes=fixes)
