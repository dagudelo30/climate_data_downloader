"""End-to-end: local CHIRPS mirror + fake CDS → Bronze → Silver → Gold (1 km and 5 km)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
import rasterio

from climate_pipeline.domain.artifacts import Layer, ProductStatus
from climate_pipeline.domain.dates import EpiWeek, date_range
from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.extractors.agera5 import Agera5Extractor
from climate_pipeline.pipeline.orchestrator import NamedGrid, Pipeline, RunRequest
from climate_pipeline.storage.gold import GoldTable, OutputFormat, open_gold_store
from climate_pipeline.storage.raster import load_layer, make_raster

START, END = date(2026, 1, 4), date(2026, 1, 17)  # exactly epi weeks 2026-W01 and W02
GRIDS = [NamedGrid("1km", "EPSG:9377@1000"), NamedGrid("5km", "EPSG:9377@5000")]


@pytest.fixture
def pipeline(settings, pipeline_config, lake, fake_cds, chirps_mirror):
    for day in date_range(START, END):
        chirps_mirror["publish"](day, "final")
    pipeline_config.sources["agera5"].variables = [
        "temp_mean_2m",
        "temp_max_2m",
        "temp_min_2m",
        "precipitation_flux",
    ]

    def agera5_factory(*args, **kwargs):
        return Agera5Extractor(*args, client_factory=lambda: fake_cds, **kwargs)

    return Pipeline(settings, pipeline_config, lake=lake, extractor_factories={"agera5": agera5_factory})


def _request(aoi_file, output_format=OutputFormat.PARQUET, **kwargs) -> RunRequest:
    return RunRequest(
        aoi_path=aoi_file,
        sources=["chirps", "agera5"],
        start_date=START,
        end_date=END,
        target_grids=GRIDS,
        output_format=output_format,
        today=date(2026, 10, 27),
        **kwargs,
    )


@pytest.mark.parametrize("output_format", [OutputFormat.PARQUET, OutputFormat.DUCKDB])
def test_full_run_produces_consistent_gold_tables(pipeline, aoi_file, lake, output_format, tmp_path):
    report = pipeline.run(_request(aoi_file, output_format))
    assert report.ok, report.to_dict()
    sources = {source.source: source for source in report.sources}
    assert sources["chirps"].fetched == 14 and sources["chirps"].silver_days == 14
    assert sources["agera5"].fetched == 14 * 4 and sources["agera5"].silver_days == 14

    for gold in report.gold:
        store = open_gold_store(lake.gold_layout(gold.grid_name), output_format)
        daily = store.query(f"SELECT * FROM {GoldTable.DAILY}")
        weekly = store.query(f"SELECT * FROM {GoldTable.EPIWEEKLY}")
        grid = GridSpec.load(lake.gold_layout(gold.grid_name).grid_path)

        # Shape: one row per AOI cell and day; every touched cell is present every day.
        assert len(daily) == gold.cells_in_aoi * 14
        assert daily.groupby("date")["cell_id"].nunique().eq(gold.cells_in_aoi).all()
        assert not daily.duplicated(["cell_id", "date"]).any()
        assert daily["cell_id"].between(0, grid.width * grid.height - 1).all()

        # Physical sanity after unit conversion (fake AgERA5 serves Kelvin and J m-2).
        assert daily["agera5_temp_mean"].between(15, 35).all()
        assert (daily["agera5_temp_min"] <= daily["agera5_temp_mean"]).all()
        assert (daily["agera5_temp_mean"] <= daily["agera5_temp_max"]).all()
        assert (daily["chirps_precip"] >= 0).all() and (daily["agera5_precip"] >= 0).all()

        # Weekly: two complete epi weeks, sums equal the sum of daily values per cell.
        assert sorted(set(zip(weekly["epi_year"], weekly["epi_week"], strict=True))) == [(2026, 1), (2026, 2)]
        assert (weekly["days_observed"] == 7).all()
        daily["week"] = [EpiWeek.from_date(pd.Timestamp(day).date()).week for day in daily["date"]]
        expected = daily.groupby(["cell_id", "week"])["chirps_precip"].sum().rename("expected").reset_index()
        merged = weekly.merge(expected, left_on=["cell_id", "epi_week"], right_on=["cell_id", "week"])
        np.testing.assert_allclose(merged["chirps_precip"], merged["expected"], rtol=1e-5)
        expected_mean = (
            daily.groupby(["cell_id", "week"])["agera5_temp_mean"].mean().rename("tm").reset_index()
        )
        merged = weekly.merge(expected_mean, left_on=["cell_id", "epi_week"], right_on=["cell_id", "week"])
        np.testing.assert_allclose(merged["agera5_temp_mean"], merged["tm"], rtol=1e-5)

        # Gold → raster round trip reproduces the table exactly.
        path = make_raster(store, "chirps_precip", tmp_path / f"{gold.grid_name}.tif", day=START)
        layer = load_layer(store, grid, "chirps_precip", day=START)
        with rasterio.open(path) as raster:
            np.testing.assert_array_equal(raster.read(1), layer)
        day_rows = daily[pd.to_datetime(daily["date"]).dt.date == START]
        np.testing.assert_allclose(
            layer.reshape(-1)[day_rows["cell_id"].to_numpy()], day_rows["chirps_precip"].to_numpy(), rtol=1e-6
        )

    one_km, five_km = (r for r in sorted(report.gold, key=lambda r: r.grid_name))
    assert one_km.cells_in_aoi > five_km.cells_in_aoi


def test_rerun_is_idempotent_and_fetches_nothing(pipeline, aoi_file, fake_cds):
    pipeline.run(_request(aoi_file))
    requests_after_first_run = len(fake_cds.requests)
    second = pipeline.run(_request(aoi_file))
    assert second.ok
    assert len(fake_cds.requests) == requests_after_first_run
    assert all(source.fetched == 0 for source in second.sources)
    assert second.gold == []  # nothing changed in Silver → no Gold rebuild


def test_prelim_data_is_upgraded_when_final_appears(settings, pipeline_config, lake, chirps_mirror, aoi_file):
    day = date(2026, 1, 5)
    chirps_mirror["publish"](day, "prelim")
    pipeline = Pipeline(settings, pipeline_config, lake=lake)
    request = RunRequest(
        aoi_path=aoi_file,
        sources=["chirps"],
        start_date=day,
        end_date=day,
        target_grids=[GRIDS[1]],
        today=date(2026, 3, 30),
    )
    first = pipeline.run(request)
    assert first.sources[0].prelim == 1
    assert (
        pipeline.catalog.status_map("chirps", Layer.BRONZE)[("precipitation_flux", day)]
        is ProductStatus.PRELIM
    )

    chirps_mirror["publish"](day, "final")
    second = pipeline.run(request)  # 84 days later than the date: past CHIRPS final latency
    assert second.sources[0].final == 1
    assert (
        pipeline.catalog.status_map("chirps", Layer.BRONZE)[("precipitation_flux", day)]
        is ProductStatus.FINAL
    )
    assert [g.grid_name for g in second.gold] == ["5km"]  # Gold rebuilt with the final values


def test_source_failures_do_not_stop_other_sources(settings, pipeline_config, lake, chirps_mirror, aoi_file):
    chirps_mirror["publish"](START, "final")

    class BrokenClient:
        def retrieve(self, *args, **kwargs):
            raise PermissionError("401 Unauthorized")

    pipeline = Pipeline(
        settings,
        pipeline_config,
        lake=lake,
        extractor_factories={"agera5": lambda *a, **k: Agera5Extractor(*a, client_factory=BrokenClient, **k)},
    )
    request = RunRequest(
        aoi_path=aoi_file,
        sources=["chirps", "agera5"],
        start_date=START,
        end_date=START,
        target_grids=[GRIDS[1]],
        today=date(2026, 10, 27),
    )
    report = pipeline.run(request)
    assert not report.ok  # CLI exits 1
    by_source = {source.source: source for source in report.sources}
    assert by_source["chirps"].ok and by_source["chirps"].fetched == 1
    assert by_source["agera5"].failures > 0
    assert report.gold and report.gold[0].days_built == [START.isoformat()]  # CHIRPS still reached Gold


def test_gold_keeps_every_aoi_cell_when_a_cell_has_no_value(
    settings, pipeline_config, lake, chirps_mirror, aoi_file
):
    # Like a cloud-masked optical index: no value over the western part of the AOI.
    path = chirps_mirror["publish"](START, "final")
    with rasterio.open(path, "r+") as raster:
        values = raster.read(1)
        values[:, :9] = -9999.0
        raster.write(values, 1)
    request = RunRequest(
        aoi_path=aoi_file,
        sources=["chirps"],
        start_date=START,
        end_date=START,
        target_grids=[GRIDS[0]],
        today=date(2026, 10, 27),
    )
    report = Pipeline(settings, pipeline_config, lake=lake).run(request)
    assert report.ok, report.to_dict()
    gold = report.gold[0]
    store = open_gold_store(lake.gold_layout(gold.grid_name), OutputFormat.PARQUET)
    daily = store.query(f"SELECT * FROM {GoldTable.DAILY}")
    weekly = store.query(f"SELECT * FROM {GoldTable.EPIWEEKLY}")
    # The panel stays complete: missing values are NA, never missing rows.
    assert len(daily) == gold.cells_in_aoi and daily["cell_id"].is_unique
    assert daily["chirps_precip"].isna().any() and daily["chirps_precip"].notna().any()
    assert daily[["latitude", "longitude"]].notna().all().all()
    assert len(weekly) == gold.cells_in_aoi


def test_every_partition_has_every_selected_column(settings, pipeline_config, lake, chirps_mirror, aoi_file):
    """A day where only one source published still carries all selected columns (NA)."""
    chirps_mirror["publish"](START, "final")
    pipeline = Pipeline(settings, pipeline_config, lake=lake)
    request = RunRequest(
        aoi_path=aoi_file,
        sources=["chirps"],
        start_date=START,
        end_date=START,
        target_grids=[GRIDS[1]],
        today=date(2026, 10, 27),
    )
    assert pipeline.run(request).ok
    store = open_gold_store(lake.gold_layout("5km"), OutputFormat.PARQUET)
    daily = store.read_daily([START])
    selected = pipeline.gold_columns()
    assert selected <= set(daily.columns) and len(selected) > 50
    assert daily["chirps_precip"].notna().all() and daily["agera5_temp_mean"].isna().all()
    weekly = store.query(f"SELECT * FROM {GoldTable.EPIWEEKLY}")
    assert {f"{column}_ndays" for column in selected} <= set(weekly.columns)
    assert (weekly["agera5_temp_mean_ndays"] == 0).all()
