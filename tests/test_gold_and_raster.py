from datetime import date

import numpy as np
import pandas as pd
import pytest
import rasterio

from climate_pipeline.domain.dates import EpiWeek
from climate_pipeline.domain.errors import ConfigurationError, DataNotAvailableError
from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.storage.gold import GoldTable, OutputFormat, open_gold_store
from climate_pipeline.storage.raster import load_layer, make_raster

GRID = GridSpec("EPSG:9377", (1000.0, 0.0, 4_600_000.0, 0.0, -1000.0, 1_945_000.0), 4, 3)


def _frame(day: date, value: float, with_extra: bool = False) -> pd.DataFrame:
    cells = np.array([0, 1, 5, 11])
    frame = pd.DataFrame(
        {
            "cell_id": cells,
            "date": day,
            "latitude": 3.4,
            "longitude": -76.5,
            "chirps_precip": np.float32(value) + cells.astype("float32"),
        }
    )
    if with_extra:
        frame["agera5_temp_mean"] = np.float32(24.0)
    return frame


@pytest.fixture(params=[OutputFormat.PARQUET, OutputFormat.DUCKDB])
def store(request, lake):
    layout = lake.gold_layout("1km")
    GRID.save(layout.grid_path)
    return open_gold_store(layout, request.param)


def test_daily_upsert_is_idempotent(store) -> None:
    day = date(2026, 1, 5)
    store.upsert_daily(day, _frame(day, 1.0))
    store.upsert_daily(day, _frame(day, 2.0))  # same partition rewritten, not appended
    result = store.read_daily([day])
    assert len(result) == 4
    assert sorted(result["chirps_precip"].tolist()) == [2.0, 3.0, 7.0, 13.0]


def test_schema_evolves_when_a_new_source_arrives(store) -> None:
    store.upsert_daily(date(2026, 1, 5), _frame(date(2026, 1, 5), 1.0))
    store.upsert_daily(date(2026, 1, 6), _frame(date(2026, 1, 6), 1.0, with_extra=True))
    both = store.read_daily([date(2026, 1, 5), date(2026, 1, 6)])
    assert len(both) == 8
    assert both.loc[pd.to_datetime(both["date"]).dt.day == 5, "agera5_temp_mean"].isna().all()
    assert (both.loc[pd.to_datetime(both["date"]).dt.day == 6, "agera5_temp_mean"] == 24.0).all()


def test_query_and_weekly_partition(store) -> None:
    week = EpiWeek(2026, 2)
    weekly = pd.DataFrame({"cell_id": [0, 1], "epi_year": 2026, "epi_week": 2, "chirps_precip": [10.0, 20.0]})
    store.upsert_weekly(week, weekly)
    store.upsert_weekly(week, weekly.assign(chirps_precip=[11.0, 21.0]))
    result = store.query(f"SELECT sum(chirps_precip) AS total FROM {GoldTable.EPIWEEKLY}")
    assert float(result["total"].iloc[0]) == pytest.approx(32.0)


def test_make_raster_roundtrip_is_exact(store, tmp_path) -> None:
    day = date(2026, 1, 5)
    store.upsert_daily(day, _frame(day, 1.0))
    layer = load_layer(store, GRID, "chirps_precip", day=day)
    expected = np.full(12, np.nan, dtype="float32")
    expected[[0, 1, 5, 11]] = [1.0, 2.0, 6.0, 12.0]
    np.testing.assert_array_equal(layer.reshape(-1), expected)

    path = make_raster(store, "chirps_precip", tmp_path / "out.tif", day=day)
    with rasterio.open(path) as raster:
        assert raster.crs.to_epsg() == 9377
        assert raster.shape == GRID.shape
        assert tuple(raster.transform)[:6] == GRID.transform
        assert raster.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
        np.testing.assert_array_equal(raster.read(1).reshape(-1), expected)


def test_make_raster_resamples_to_a_different_template(store, tmp_path) -> None:
    day = date(2026, 1, 5)
    frame = pd.DataFrame(
        {"cell_id": np.arange(12), "date": day, "latitude": 3.4, "longitude": -76.5, "chirps_precip": 6.0}
    )
    store.upsert_daily(day, frame)
    template = GridSpec("EPSG:9377", (2000.0, 0.0, 4_600_000.0, 0.0, -2000.0, 1_945_000.0), 2, 1)
    path = make_raster(store, "chirps_precip", tmp_path / "coarse.tif", day=day, template=template)
    with rasterio.open(path) as raster:
        assert raster.shape == (1, 2)
        np.testing.assert_allclose(raster.read(1), 6.0)


def test_make_raster_errors(store, tmp_path) -> None:
    with pytest.raises(DataNotAvailableError):
        make_raster(store, "chirps_precip", tmp_path / "none.tif", day=date(2030, 1, 1))
    with pytest.raises(ConfigurationError):
        load_layer(store, GRID, "chirps_precip; DROP TABLE gold_daily", day=date(2026, 1, 5))
    with pytest.raises(ConfigurationError):
        load_layer(store, GRID, "cell_id", day=date(2026, 1, 5))


def test_catalog_paths_are_relative_to_the_lake_root(tmp_path) -> None:
    from climate_pipeline.storage.layout import DataLake

    lake = DataLake(tmp_path / "lake")
    path = lake.silver_path("chirps", date(2026, 1, 5))
    stored = lake.relative(path)
    assert stored == "silver/chirps/2026/01/chirps_2026-01-05.nc"
    moved = DataLake(tmp_path / "elsewhere")
    assert moved.resolve(stored) == tmp_path / "elsewhere" / stored
