"""Coverage: what the Gold store holds between two dates, per source, and what is missing.

Every day of ``[start, min(end, today − latency)]`` is classified per source:

===================  ==============================================================
``in_gold``          at least one value of the source's columns in ``gold_daily``
``no_valid_pixels``  built, but every value is NA (clouds, no overpass inside the AOI)
``upstream_missing`` the provider had not published it (catalog ``missing``)
``not_built``        Bronze/Silver exist but the day has no Gold row (Gold pending or failed)
``not_requested``    never asked for: the next ``sync`` / ``run`` fetches it
===================  ==============================================================

``not_built`` and ``not_requested`` are the **gaps**. A source is **stale** when its last
processed day (``in_gold``, ``no_valid_pixels`` or ``upstream_missing``) is more than
``max_behind_days`` behind the last day the provider could already have published.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import pandas as pd

from climate_pipeline.config import PipelineConfig
from climate_pipeline.domain.artifacts import Layer, ProductStatus
from climate_pipeline.domain.dates import date_range
from climate_pipeline.domain.variables import VariableCatalog
from climate_pipeline.storage.catalog import Catalog
from climate_pipeline.storage.gold import GoldStore

GAP_STATES = ("not_built", "not_requested")
PROCESSED_STATES = ("in_gold", "no_valid_pixels", "upstream_missing")


def contiguous_ranges(days: Iterable[date]) -> list[tuple[date, date]]:
    """``[d1, d2, d3, d7]`` → ``[(d1, d3), (d7, d7)]``."""
    ranges: list[tuple[date, date]] = []
    for day in sorted(set(days)):
        if ranges and day == ranges[-1][1] + timedelta(days=1):
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


@dataclass
class SourceCoverage:
    source: str
    expected_until: date
    days_expected: int
    states: dict[str, int] = field(default_factory=dict)
    gaps: list[tuple[date, date]] = field(default_factory=list)
    last_valid_day: date | None = None
    last_processed_day: date | None = None
    behind_days: int | None = None
    stale: bool = False

    @property
    def gap_days(self) -> int:
        return sum(self.states.get(state, 0) for state in GAP_STATES)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["gap_days"] = self.gap_days
        payload["gaps"] = [[start.isoformat(), end.isoformat()] for start, end in self.gaps]
        return payload


def classify_days(
    days: Sequence[date],
    *,
    valid_days: set[date],
    gold_days: set[date],
    bronze: Mapping[date, set[ProductStatus]],
    silver_days: set[date],
) -> dict[date, str]:
    """State of each day for one source (see the module docstring)."""
    states = {}
    for day in days:
        statuses = bronze.get(day, set())
        if day in valid_days:
            states[day] = "in_gold"
        elif day in gold_days and day in silver_days:
            states[day] = "no_valid_pixels"
        elif statuses and statuses == {ProductStatus.MISSING} and day not in silver_days:
            states[day] = "upstream_missing"
        elif day in silver_days or statuses - {ProductStatus.MISSING}:
            states[day] = "not_built"
        else:
            states[day] = "not_requested"
    return states


def summarize_source(
    source: str,
    states: Mapping[date, str],
    expected_until: date,
    max_behind_days: int,
) -> SourceCoverage:
    days = sorted(states)
    valid = [day for day in days if states[day] == "in_gold"]
    processed = [day for day in days if states[day] in PROCESSED_STATES]
    coverage = SourceCoverage(
        source=source,
        expected_until=expected_until,
        days_expected=len(days),
        states=dict(Counter(states.values())),
        gaps=contiguous_ranges(day for day in days if states[day] in GAP_STATES),
        last_valid_day=max(valid) if valid else None,
        last_processed_day=max(processed) if processed else None,
    )
    if days:
        reference = coverage.last_processed_day
        coverage.behind_days = (expected_until - reference).days if reference else len(days)
        coverage.stale = coverage.behind_days > max_behind_days
    return coverage


def _catalog_days(catalog: Catalog, source: str) -> tuple[dict[date, set[ProductStatus]], set[date]]:
    bronze: dict[date, set[ProductStatus]] = {}
    frame = catalog.entries(source, Layer.BRONZE)
    for day, status in zip(frame["date"], frame["status"], strict=True):
        bronze.setdefault(date.fromisoformat(day), set()).add(ProductStatus(status))
    silver = catalog.entries(source, Layer.SILVER)
    silver_days = {
        date.fromisoformat(day)
        for day, status in zip(silver["date"], silver["status"], strict=True)
        if status != ProductStatus.MISSING
    }
    return bronze, silver_days


def coverage_report(
    store: GoldStore,
    catalog: Catalog,
    variables: VariableCatalog,
    config: PipelineConfig,
    sources: Sequence[str],
    *,
    start: date,
    end: date,
    today: date,
    max_behind_days: int = 3,
) -> dict:
    """Coverage of every source on one Gold store (JSON-ready)."""
    columns_by_source = {
        source: [spec.gold_column for spec in variables.for_source(source, config.source(source).variables)]
        for source in sources
    }
    counts = store.daily_value_counts(start, end, [c for cols in columns_by_source.values() for c in cols])
    gold_days = set(counts.loc[counts["n_rows"] > 0, "date"]) if not counts.empty else set()

    report = []
    for source in sources:
        expected_until = min(end, today - timedelta(days=config.source(source).latency.earliest_lag_days))
        days = date_range(start, expected_until) if expected_until >= start else []
        present = [column for column in columns_by_source[source] if column in counts.columns]
        valid_days = set(counts.loc[(counts[present] > 0).any(axis=1), "date"]) if present else set()
        bronze, silver_days = _catalog_days(catalog, source)
        states = classify_days(
            days, valid_days=valid_days, gold_days=gold_days, bronze=bronze, silver_days=silver_days
        )
        report.append(summarize_source(source, states, expected_until, max_behind_days))

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "today": today.isoformat(),
        "max_behind_days": max_behind_days,
        "ok": not any(item.gap_days or item.stale for item in report),
        "sources": [item.to_dict() for item in report],
    }


def coverage_table(report: dict) -> pd.DataFrame:
    """One line per source, for terminals and job summaries."""
    rows = []
    for item in report["sources"]:
        states = item["states"]
        rows.append(
            {
                "source": item["source"],
                "expected_until": item["expected_until"],
                "days": item["days_expected"],
                **{state: states.get(state, 0) for state in (*PROCESSED_STATES, *GAP_STATES)},
                "last_valid": item["last_valid_day"],
                "behind_days": item["behind_days"],
                "stale": item["stale"],
            }
        )
    return pd.DataFrame(rows)
