"""Coverage classification, per-source Gold bookkeeping, retry window and the declarative run."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from climate_pipeline.config import LatencyConfig, load_pipeline_config
from climate_pipeline.domain.artifacts import Layer, ProductStatus
from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.pipeline.coverage import classify_days, contiguous_ranges, summarize_source
from climate_pipeline.pipeline.latency import plan_source
from climate_pipeline.storage.catalog import ALL_SOURCES, CatalogEntry, ParquetCatalog, gold_source

D = [date(2026, 3, 1) + timedelta(days=offset) for offset in range(8)]


def test_contiguous_ranges() -> None:
    assert contiguous_ranges([D[3], D[0], D[1], D[2], D[6]]) == [(D[0], D[3]), (D[6], D[6])]
    assert contiguous_ranges([]) == []


def test_days_are_classified_by_store_and_catalog() -> None:
    states = classify_days(
        D[:6],
        valid_days={D[0]},
        gold_days={D[0], D[1]},
        bronze={
            D[1]: {ProductStatus.FINAL},
            D[2]: {ProductStatus.MISSING},
            D[3]: {ProductStatus.FINAL},
            D[4]: {ProductStatus.MISSING, ProductStatus.FINAL},
        },
        silver_days={D[1], D[3], D[4]},
    )
    assert states == {
        D[0]: "in_gold",
        D[1]: "no_valid_pixels",  # built, everything masked
        D[2]: "upstream_missing",
        D[3]: "not_built",
        D[4]: "not_built",
        D[5]: "not_requested",
    }


def test_staleness_uses_the_last_processed_day() -> None:
    states = {D[0]: "in_gold", D[1]: "upstream_missing", D[2]: "not_requested", D[3]: "not_requested"}
    coverage = summarize_source("landsat", states, expected_until=D[3], max_behind_days=1)
    assert coverage.last_valid_day == D[0] and coverage.last_processed_day == D[1]
    assert coverage.behind_days == 2 and coverage.stale
    assert coverage.gaps == [(D[2], D[3])] and coverage.gap_days == 2
    fresh = summarize_source("imerg", {D[0]: "in_gold"}, expected_until=D[0], max_behind_days=0)
    assert not fresh.stale and fresh.behind_days == 0


def test_gold_is_pending_per_source(lake) -> None:
    catalog = ParquetCatalog(lake)
    catalog.record(
        [
            CatalogEntry("imerg", "*", D[0], Layer.SILVER, ProductStatus.PRELIM),
            CatalogEntry("agera5", "*", D[0], Layer.SILVER, ProductStatus.FINAL),
        ]
    )
    # A runner that only held IMERG's Silver built the day: AgERA5 is still pending.
    catalog.mark_gold_built("1km__postgres", {D[0]: ["imerg"]})
    assert catalog.pending_gold_dates("1km__postgres") == [D[0]]
    catalog.mark_gold_built("1km__postgres", {D[0]: ["agera5"]})
    assert catalog.pending_gold_dates("1km__postgres") == []
    # New Silver of one source makes the day pending again.
    catalog.record([CatalogEntry("imerg", "*", D[0], Layer.SILVER, ProductStatus.FINAL)])
    assert catalog.pending_gold_dates("1km__postgres") == [D[0]]


def test_legacy_gold_rows_cover_every_source(lake) -> None:
    catalog = ParquetCatalog(lake)
    catalog.record([CatalogEntry("imerg", "*", D[0], Layer.SILVER, ProductStatus.FINAL)])
    catalog.record(
        [CatalogEntry(gold_source("1km__parquet"), ALL_SOURCES, D[0], Layer.GOLD, ProductStatus.FINAL)]
    )
    assert catalog.pending_gold_dates("1km__parquet") == []


def test_old_missing_days_are_not_retried_by_sync() -> None:
    latency = LatencyConfig(final_lag_days=7, lookback_days=30)
    today = date(2026, 11, 8)
    old, recent = date(2026, 6, 1), date(2026, 10, 25)
    status = {("lst", old): ProductStatus.MISSING, ("lst", recent): ProductStatus.MISSING}
    common = {"today": today, "start": old, "end": recent}
    retry_all = plan_source("landsat", latency, ["lst"], status, **common)
    assert old in retry_all.requests["lst"] and recent in retry_all.requests["lst"]
    bounded = plan_source(
        "landsat", latency, ["lst"], status, retry_since=today - timedelta(days=30), **common
    )
    assert old not in bounded.requests["lst"] and recent in bounded.requests["lst"]
    assert bounded.reasons["missing_given_up"] == 1


def test_run_section_declares_the_exercise(tmp_path) -> None:
    path = tmp_path / "pipeline.yaml"
    path.write_text(
        """
run:
  aoi_path: data/auxiliary/shapefiles/cali/cali.shp
  sources: [imerg, agera5]
  start_date: 2026-01-01
  target_grids: {1km: "EPSG:9377@1000", 5km: "EPSG:9377@5000"}
  output_format: postgres
  static: {layers: [terrain], year: 2026}
"""
    )
    run = load_pipeline_config(path).run
    assert run.start_date == date(2026, 1, 1) and run.end_date is None
    assert run.target_grids["5km"] == "EPSG:9377@5000" and run.output_format == "postgres"
    assert run.static.layers == ["terrain"]
    path.write_text("run: {sources: [era6]}")
    with pytest.raises(ConfigurationError, match="era6"):
        load_pipeline_config(path)
    path.write_text("run: {typo: 1}")
    with pytest.raises(ConfigurationError):
        load_pipeline_config(path)


def test_operational_example_config_is_valid() -> None:
    from pathlib import Path

    config = load_pipeline_config(Path("examples/cali_operational/pipeline.yaml"))
    run = config.run
    assert run.start_date == date(2026, 1, 1) and run.end_date is None  # real time: up to today
    assert set(run.target_grids) == {"100m", "500m", "1km", "5km"}
    assert run.aoi_path is not None and run.aoi_path.exists()
