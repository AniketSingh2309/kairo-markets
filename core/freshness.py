"""Data-freshness assessment.

Two independent checks, both judged on each dataset's ``observed_at``:

* staleness  -- a dataset older than ``stale_after_hours`` relative to the
  request's reference time;
* skew       -- datasets whose observation times differ by more than
  ``max_skew_hours``. Skewed data must not be presented as equally current,
  so a human-readable notice is generated here (by code, not the LLM) and is
  shown verbatim at the top of the final report.
"""

from __future__ import annotations

import datetime as dt

from core.bundle import DATASETS, DataBundle
from core.config import Settings
from core.schemas import DatasetFreshness, Flag, FreshnessReport


def _fmt(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _span(hours: float) -> str:
    return f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.1f} hours"


def assess_freshness(
    bundle: DataBundle, reference_time: dt.datetime, settings: Settings
) -> tuple[FreshnessReport, list[Flag]]:
    datasets: list[DatasetFreshness] = []
    flags: list[Flag] = []

    for tool, (_, label) in DATASETS.items():
        response = bundle.get(tool)
        if response is None:
            continue
        age = (reference_time - response.observed_at).total_seconds() / 3600
        # A quote for an explicitly requested past date is old by design.
        historical = tool == "get_stock_price" and response.query.get("date") is not None
        stale = not historical and age > settings.stale_after_hours
        datasets.append(DatasetFreshness(
            dataset=label,
            source_id=response.source.source_id,
            observed_at=response.observed_at,
            age_hours=round(age, 1),
            stale=stale,
            historical=historical,
        ))
        if stale:
            flags.append(Flag(
                code="STALE_DATA", severity="warning", dataset=label,
                message=(
                    f"The {label} was last observed at {_fmt(response.observed_at)}, "
                    f"{_span(age)} before this request (threshold {settings.stale_after_hours:g} hours)."
                ),
            ))

    report = FreshnessReport(reference_time=reference_time, datasets=datasets)
    current = [d for d in datasets if not d.historical]
    if len(current) >= 2:
        newest = max(current, key=lambda d: d.observed_at)
        oldest = min(current, key=lambda d: d.observed_at)
        skew = (newest.observed_at - oldest.observed_at).total_seconds() / 3600
        report.max_skew_hours = round(skew, 1)
        if skew > settings.max_skew_hours:
            report.discrepancy = True
            report.notice = (
                f"DATA TIMESTAMP MISMATCH: the {newest.dataset} is as of {_fmt(newest.observed_at)} "
                f"but the {oldest.dataset} is only as of {_fmt(oldest.observed_at)} "
                f"({_span(skew)} older). These datasets are not equally current; anything that "
                f"happened after {_fmt(oldest.observed_at)} is missing from the {oldest.dataset}."
            )
            flags.append(Flag(
                code="TIMESTAMP_DISCREPANCY", severity="warning", dataset=oldest.dataset,
                message=report.notice,
            ))
    return report, flags
