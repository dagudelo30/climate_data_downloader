"""Latency-aware ingestion planning.

Each source publishes with a different delay and some publish a preliminary version
that is later replaced. Instead of failing when data is not there yet, the planner
decides, per ``(variable, date)``, whether to fetch:

==================  ===========================================================
reason              when
==================  ===========================================================
``new``             never attempted
``retry_missing``   attempted before, not published then, now inside the window
``prelim_upgrade``  stored as prelim and old enough for the final to exist
``forced``          ``--force``
==================  ===========================================================

With ``retry_since`` (used by ``sync`` over long windows), a day recorded as ``missing``
before that date is not asked for again (``missing_given_up``): a Landsat day without an
overpass stays without one, and a daily run over a whole year would otherwise re-query
hundreds of such days.

Dates newer than ``today - earliest_lag`` are not requested at all (counted as
``not_yet_expected``), so a scheduled run never asks a provider for data it cannot
have yet.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from climate_pipeline.config import LatencyConfig
from climate_pipeline.domain.artifacts import ProductStatus
from climate_pipeline.domain.dates import date_range
from climate_pipeline.domain.errors import ConfigurationError


@dataclass
class SourcePlan:
    source: str
    available_until: date
    final_until: date
    window_start: date | None = None
    window_end: date | None = None
    requests: dict[str, list[date]] = field(default_factory=dict)
    reasons: Counter[str] = field(default_factory=Counter)

    @property
    def total_requests(self) -> int:
        return sum(len(days) for days in self.requests.values())

    @property
    def days(self) -> list[date]:
        return sorted({day for days in self.requests.values() for day in days})

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "available_until": self.available_until.isoformat(),
            "final_until": self.final_until.isoformat(),
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "requests": self.total_requests,
            "days": len(self.days),
            "reasons": dict(self.reasons),
        }


def plan_source(
    source: str,
    latency: LatencyConfig,
    variables: list[str],
    status_map: dict[tuple[str, date], ProductStatus],
    *,
    today: date,
    start: date | None = None,
    end: date | None = None,
    incremental: bool = False,
    force: bool = False,
    retry_since: date | None = None,
) -> SourcePlan:
    available_until = today - timedelta(days=latency.earliest_lag_days)
    final_until = today - timedelta(days=latency.final_lag_days)
    plan = SourcePlan(source=source, available_until=available_until, final_until=final_until)

    if incremental:
        start = start or today - timedelta(days=latency.lookback_days)
        end = end or available_until
    elif start is None or end is None:
        raise ConfigurationError("start and end dates are required unless --incremental is used")
    if end < start:
        raise ConfigurationError(f"end date {end} is before start date {start}")

    effective_end = min(end, available_until)
    first_unavailable = max(start, available_until + timedelta(days=1))
    if end >= first_unavailable:
        plan.reasons["not_yet_expected"] = ((end - first_unavailable).days + 1) * len(variables)
    if effective_end < start:
        return plan
    plan.window_start, plan.window_end = start, effective_end

    for day in date_range(start, effective_end):
        for variable in variables:
            status = status_map.get((variable, day))
            if force:
                reason = "forced"
            elif status is None:
                reason = "new"
            elif status is ProductStatus.MISSING:
                if retry_since is not None and day < retry_since:
                    plan.reasons["missing_given_up"] += 1
                    continue
                reason = "retry_missing"
            elif status is ProductStatus.PRELIM and latency.reprocess_prelim and day <= final_until:
                reason = "prelim_upgrade"
            else:
                plan.reasons["up_to_date"] += 1
                continue
            plan.reasons[reason] += 1
            plan.requests.setdefault(variable, []).append(day)
    return plan
