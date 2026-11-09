"""PostgreSQL back-end: pure helpers (always) and the store/catalog against a real server
(``postgres`` marker, needs ``TEST_DATABASE_URL``)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from shapely import wkt

from climate_pipeline.domain.artifacts import Layer, ProductStatus
from climate_pipeline.domain.dates import EpiWeek
from climate_pipeline.domain.errors import ConfigurationError, GridMismatchError
from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.storage.catalog import CatalogEntry, ParquetCatalog
from climate_pipeline.storage.gold import GoldTable
from climate_pipeline.storage.layout import GoldLayout
from climate_pipeline.storage.postgres import (
    PostgresCatalog,
    PostgresGoldStore,
    _partition_name,
    _to_pyformat,
    cell_polygons_wkt,
    identifier,
    migrations,
    pg_type,
)

GRID = GridSpec("EPSG:9377", (1000.0, 0.0, 4601000.0, 0.0, -1000.0, 1946000.0), 15, 20)
DAY = date(2026, 1, 6)


def _daily(cells, day=DAY, **columns) -> pd.DataFrame:
    cells = np.asarray(cells, dtype="int64")
    return pd.DataFrame(
        {
            "cell_id": cells,
            "date": day,
            "latitude": 3.4 + cells * 1e-3,
            "longitude": -76.5 - cells * 1e-3,
            **{name: np.asarray(values, dtype="float32") for name, values in columns.items()},
        }
    )


# --- pure helpers ---------------------------------------------------------------------------
def test_pg_types_follow_pandas_dtypes() -> None:
    frame = pd.DataFrame(
        {
            "f4": np.array([1.0], "float32"),
            "f8": [1.0],
            "i1": np.array([1], "int8"),
            "i8": [1],
            "b": [True],
            "d": [date(2026, 1, 1)],
            "s": ["x"],
        }
    )
    assert {column: pg_type(frame[column]) for column in frame} == {
        "f4": "real",
        "f8": "double precision",
        "i1": "smallint",
        "i8": "bigint",
        "b": "boolean",
        "d": "date",
        "s": "text",
    }


def test_duckdb_placeholders_become_psycopg_ones() -> None:
    statement = "SELECT * FROM t WHERE a = ? AND b LIKE '%x?' AND c = ?;"
    assert _to_pyformat(statement) == "SELECT * FROM t WHERE a = %s AND b LIKE '%%x?' AND c = %s"


def test_identifiers_are_validated() -> None:
    assert identifier("gold_daily")
    for bad in ('x"; DROP TABLE y; --', "1abc", "a" * 64):
        with pytest.raises(ConfigurationError):
            identifier(bad)


def test_partition_names_are_stable_and_distinct() -> None:
    assert _partition_name("gold_daily", "100m") == "gold_daily__100m"
    assert _partition_name("gold_daily", "1.km") != _partition_name("gold_daily", "1_km")
    assert _partition_name("gold_daily", "1.km") == _partition_name("gold_daily", "1.km")


def test_cell_polygons_are_wgs84_squares_of_the_grid() -> None:
    polygons = [wkt.loads(text) for text in cell_polygons_wkt(GRID, np.array([0, 16]))]
    for polygon in polygons:
        minx, miny, maxx, maxy = polygon.bounds
        assert -77 < minx < maxx < -76 and 3 < miny < maxy < 4
        assert 0.008 < maxx - minx < 0.01  # ~1 km in degrees near Cali
    # cell 16 is one row down and one column right of cell 0
    assert polygons[1].centroid.x > polygons[0].centroid.x
    assert polygons[1].centroid.y < polygons[0].centroid.y


def test_migrations_are_packaged_in_order() -> None:
    versions = [version for version, _ in migrations()]
    assert versions == sorted(versions) and versions[0] == "0001_core"


# --- database -------------------------------------------------------------------------------


@pytest.fixture
def store(pg_database, tmp_path) -> PostgresGoldStore:
    layout = GoldLayout(tmp_path / "gold" / "1km")
    GRID.save(layout.grid_path)
    return PostgresGoldStore(layout, pg_database)


@pytest.mark.postgres
def test_migrations_are_idempotent(pg_database) -> None:
    assert pg_database.migrate() == []
    tables = {
        row[0]
        for row in pg_database.connection().execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", [pg_database.schema]
        )
    }
    assert {"ingest_manifest", "grid", "cell", "variable", "gold_daily", "gold_epiweekly"} <= tables


@pytest.mark.postgres
def test_daily_upserts_merge_columns_and_keep_nulls(store) -> None:
    store.upsert_daily(DAY, _daily([0, 1, 16], imerg_precip=[1.5, np.nan, 3.0]))
    # A second source written later (another runner) must not erase imerg_precip.
    store.upsert_daily(DAY, _daily([0, 1, 16], agera5_temp_mean=[20, 21, 22]))
    # Re-writing a column replaces it.
    store.upsert_daily(DAY, _daily([0, 1, 16], imerg_precip=[2.5, 0.0, np.nan]))

    daily = store.read_daily([DAY])
    assert list(daily["cell_id"]) == [0, 1, 16]
    np.testing.assert_allclose(daily["imerg_precip"], [2.5, 0.0, np.nan])
    np.testing.assert_allclose(daily["agera5_temp_mean"], [20, 21, 22])
    assert daily["imerg_precip"].dtype == np.float32
    assert {"latitude", "longitude"} <= set(daily.columns)


@pytest.mark.postgres
def test_query_api_matches_the_file_back_ends(store) -> None:
    store.upsert_daily(DAY, _daily([0, 1], imerg_precip=[1.0, 5.0]))
    rows = store.query(
        f'SELECT cell_id FROM {GoldTable.DAILY} WHERE "date" = ? AND imerg_precip > ?', [DAY, 2]
    )
    assert rows["cell_id"].tolist() == [1]
    assert store.columns(GoldTable.DAILY)[:4] == ["cell_id", "date", "latitude", "longitude"]
    counts = store.daily_value_counts(DAY, DAY, ["imerg_precip", "not_a_column"])
    assert counts.to_dict("records") == [{"date": DAY, "n_rows": 2, "imerg_precip": 2}]


@pytest.mark.postgres
def test_grid_is_shared_through_the_database(store, pg_database, tmp_path) -> None:
    store.upsert_daily(DAY, _daily([0], imerg_precip=[1.0]))
    # A fresh runner without grid.json gets it back from the database.
    fresh = PostgresGoldStore(GoldLayout(tmp_path / "runner" / "1km"), pg_database)
    assert GridSpec.load(fresh.layout.grid_path).fingerprint() == GRID.fingerprint()
    # A different grid under the same name is refused.
    other = GoldLayout(tmp_path / "other" / "1km")
    GridSpec("EPSG:9377", (500.0, 0.0, 4601000.0, 0.0, -500.0, 1946000.0), 30, 40).save(other.grid_path)
    with pytest.raises(GridMismatchError):
        PostgresGoldStore(other, pg_database).upsert_daily(DAY, _daily([0], imerg_precip=[1.0]))


@pytest.mark.postgres
def test_cells_carry_geometry_when_postgis_is_available(store, pg_database) -> None:
    store.upsert_daily(DAY, _daily([0, 16], imerg_precip=[1.0, 2.0]))
    rows = pg_database.connection().execute("SELECT cell_id, row_index, col_index FROM cell ORDER BY 1")
    assert rows.fetchall() == [(0, 0, 0), (16, 1, 1)]
    if pg_database.has_postgis():
        area = (
            pg_database.connection().execute("SELECT min(ST_Area(geom::geography)) FROM cell").fetchone()[0]
        )
        assert area == pytest.approx(1e6, rel=0.01)  # 1 km × 1 km


@pytest.mark.postgres
def test_weekly_static_and_stale_weeks(store) -> None:
    week = EpiWeek.from_date(DAY)
    store.upsert_daily(DAY, _daily([0, 1], imerg_precip=[1.0, 2.0]))
    assert store.stale_weeks() == [week]
    weekly = pd.DataFrame(
        {
            "cell_id": [0, 1],
            "epi_year": week.year,
            "epi_week": week.week,
            "week_start": week.start,
            "week_end": week.end,
            "latitude": [3.4, 3.401],
            "longitude": [-76.5, -76.501],
            "days_observed": np.array([1, 1], "int8"),
            "imerg_precip": np.array([np.nan, np.nan], "float32"),
            "imerg_precip_ndays": np.array([1, 1], "int8"),
        }
    )
    store.upsert_weekly(week, weekly)
    assert store.stale_weeks() == []
    stored = store.query(f"SELECT * FROM {GoldTable.EPIWEEKLY} ORDER BY cell_id")
    assert stored["imerg_precip_ndays"].tolist() == [1, 1] and stored["imerg_precip"].isna().all()

    static = pd.DataFrame(
        {
            "cell_id": [0, 1],
            "latitude": [3.4, 3.401],
            "longitude": [-76.5, -76.501],
            "static_elevation_m": [1000.0, 1001.0],
        }
    ).astype({"static_elevation_m": "float32"})
    store.write_static(static)
    store.write_static(static.iloc[[1]])  # replace semantics for the static table
    assert store.query(f"SELECT cell_id FROM {GoldTable.STATIC}")["cell_id"].tolist() == [1]


@pytest.mark.postgres
def test_catalog_contract_is_the_same_on_both_back_ends(pg_database, lake) -> None:
    entries = [
        CatalogEntry("imerg", "precip", DAY, Layer.BRONZE, ProductStatus.PRELIM, "bronze/x.nc"),
        CatalogEntry("imerg", "precip", DAY, Layer.SILVER, ProductStatus.PRELIM, "silver/x.nc"),
        CatalogEntry("landsat", "lst", DAY, Layer.BRONZE, ProductStatus.MISSING, origin="no overpass"),
    ]
    results = []
    for catalog in (ParquetCatalog(lake), PostgresCatalog(pg_database)):
        catalog.record(entries)
        catalog.record([entries[0]])  # re-recording updates in place
        results.append(
            (
                catalog.sources(),
                catalog.status_map("imerg"),
                catalog.bronze_artifacts("landsat", DAY).empty,
                catalog.pending_gold_dates("1km__x"),
                catalog.summary()[["source", "layer", "status", "count"]].to_dict("records"),
            )
        )
    assert results[0] == results[1]
    assert results[0][0] == ["imerg", "landsat"] and results[0][3] == [DAY]


@pytest.mark.postgres
def test_manifest_import_never_replaces_newer_rows(pg_database) -> None:
    catalog = PostgresCatalog(pg_database)
    catalog.record([CatalogEntry("imerg", "precip", DAY, Layer.BRONZE, ProductStatus.FINAL)])
    stale = catalog.entries("imerg").assign(status="prelim", updated_ns=1)
    catalog.import_rows(stale)
    assert catalog.status_map("imerg")[("precip", DAY)] is ProductStatus.FINAL
