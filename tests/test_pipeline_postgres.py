"""End-to-end on PostgreSQL: local CHIRPS mirror + fake CDS → Bronze → Silver → Gold in the
database, the ``sync`` loop and an ephemeral CI runner that only holds some sources."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from climate_pipeline.domain.dates import date_range
from climate_pipeline.extractors.agera5 import Agera5Extractor
from climate_pipeline.pipeline.orchestrator import NamedGrid, Pipeline, RunRequest
from climate_pipeline.pipeline.sync import SyncRequest, import_lake, sync
from climate_pipeline.storage.gold import GoldTable, OutputFormat, open_gold_store
from climate_pipeline.storage.layout import DataLake
from climate_pipeline.storage.postgres import PostgresCatalog, PostgresGoldStore

pytestmark = pytest.mark.postgres

START, END = date(2026, 1, 4), date(2026, 1, 17)  # epi weeks 2026-W01 and W02
TODAY = date(2026, 10, 27)
GRIDS = [NamedGrid("1km", "EPSG:9377@1000"), NamedGrid("5km", "EPSG:9377@5000")]


@pytest.fixture
def factories(fake_cds):
    def agera5_factory(*args, **kwargs):
        return Agera5Extractor(*args, client_factory=lambda: fake_cds, **kwargs)

    return {"agera5": agera5_factory}


@pytest.fixture
def config(pipeline_config, chirps_mirror):
    for day in date_range(START, END):
        chirps_mirror["publish"](day, "final")
    pipeline_config.sources["agera5"].variables = ["temp_mean_2m", "temp_max_2m", "temp_min_2m"]
    return pipeline_config


def _sync_request(aoi_file, sources) -> SyncRequest:
    return SyncRequest(
        aoi_path=aoi_file, sources=sources, start=START, end=END, target_grids=GRIDS, today=TODAY
    )


def test_postgres_gold_equals_parquet_gold(pg_settings, config, factories, aoi_file, tmp_path):
    def run(output_format, root):
        lake = DataLake(root)
        pipeline = Pipeline(
            pg_settings, config, lake=lake, extractor_factories=factories, output_format=output_format
        )
        request = RunRequest(
            aoi_path=aoi_file,
            sources=["chirps", "agera5"],
            start_date=START,
            end_date=END,
            target_grids=GRIDS,
            output_format=output_format,
            today=TODAY,
        )
        report = pipeline.run(request)
        pipeline.close()
        assert report.ok, report.to_dict()
        return lake

    parquet_lake = run(OutputFormat.PARQUET, tmp_path / "parquet")
    postgres_lake = run(OutputFormat.POSTGRES, tmp_path / "postgres")
    for grid in GRIDS:
        expected = open_gold_store(parquet_lake.gold_layout(grid.name), OutputFormat.PARQUET)
        actual = open_gold_store(postgres_lake.gold_layout(grid.name), OutputFormat.POSTGRES, pg_settings)
        for table, order in ((GoldTable.DAILY, "date, cell_id"), (GoldTable.EPIWEEKLY, "epi_week, cell_id")):
            left = expected.query(f"SELECT * FROM {table} ORDER BY {order}")
            right = actual.query(f"SELECT * FROM {table} ORDER BY {order}")
            assert len(left) == len(right) > 0
            # Parquet partitions carry every selected column; columns of sources that were not
            # run are NA there (and absent from Postgres until `db init` / `sync` creates them).
            for column in set(left.columns) - set(right.columns):
                assert (
                    left[column].isna().all() if not column.endswith("_ndays") else (left[column] == 0).all()
                )
            for column in [c for c in left.columns if c in right.columns]:
                if left[column].dtype.kind == "f":
                    np.testing.assert_allclose(right[column], left[column], rtol=1e-6, err_msg=column)
                else:
                    assert left[column].astype(str).tolist() == right[column].astype(str).tolist(), column
        actual.close()


def test_sync_fills_the_window_once_and_reports_coverage(
    pg_settings, config, factories, aoi_file, tmp_path, fake_cds
):
    lake = DataLake(tmp_path / "runner")
    first = sync(
        pg_settings,
        config,
        _sync_request(aoi_file, ["chirps", "agera5"]),
        lake=lake,
        extractor_factories=factories,
    )
    assert first["ok"], first["run"]
    assert first["migrations_applied"] == []  # the fixture already migrated the schema
    coverage = first["coverage"]["1km"]
    assert coverage["ok"], coverage
    by_source = {item["source"]: item for item in coverage["sources"]}
    assert by_source["chirps"]["states"] == {"in_gold": 14}
    assert by_source["agera5"]["states"] == {"in_gold": 14}

    requests = len(fake_cds.requests)
    second = sync(
        pg_settings,
        config,
        _sync_request(aoi_file, ["chirps", "agera5"]),
        lake=lake,
        extractor_factories=factories,
    )
    assert len(fake_cds.requests) == requests  # the database says nothing is missing
    assert all(source["fetched"] == 0 for source in second["run"]["sources"])
    assert second["run"]["gold"] == []


def test_ephemeral_runners_never_erase_other_sources(pg_settings, config, factories, aoi_file, tmp_path):
    # Runner 1 ingests CHIRPS; runner 2 starts from an empty disk and ingests AgERA5.
    sync(pg_settings, config, _sync_request(aoi_file, ["chirps"]), lake=DataLake(tmp_path / "r1"))
    result = sync(
        pg_settings,
        config,
        _sync_request(aoi_file, ["agera5"]),
        lake=DataLake(tmp_path / "r2"),
        extractor_factories=factories,
    )
    assert result["ok"], result["run"]
    # A third, empty runner reads the result straight from the database.
    store = open_gold_store(DataLake(tmp_path / "r3").gold_layout("5km"), OutputFormat.POSTGRES, pg_settings)
    daily = store.query(f"SELECT * FROM {GoldTable.DAILY}")
    weekly = store.query(f"SELECT * FROM {GoldTable.EPIWEEKLY}")
    assert daily["chirps_precip"].notna().all() and daily["agera5_temp_mean"].notna().all()
    assert (weekly["chirps_precip_ndays"] == 7).all() and (weekly["agera5_temp_mean_ndays"] == 7).all()
    store.close()


def test_import_lake_seeds_the_database(pg_settings, pg_database, config, factories, aoi_file, tmp_path):
    lake = DataLake(tmp_path / "local")
    pipeline = Pipeline(pg_settings, config, lake=lake, extractor_factories=factories)
    pipeline.run(
        RunRequest(
            aoi_path=aoi_file,
            sources=["chirps"],
            start_date=START,
            end_date=END,
            target_grids=GRIDS,
            today=TODAY,
        )
    )
    imported = import_lake(lake, pg_database)
    assert imported["grids"]["1km"] == {"gold_daily": 14, "gold_epiweekly": 2, "static_covariates": 0}
    catalog = PostgresCatalog(pg_database)
    assert catalog.sources() == ["chirps"]
    # Gold builds of the Parquet store count for the Postgres store: nothing to rebuild.
    assert catalog.pending_gold_dates("1km__postgres") == []
    store = PostgresGoldStore(lake.gold_layout("1km"), pg_database)
    assert len(store.read_daily([START])) == len(
        open_gold_store(lake.gold_layout("1km"), OutputFormat.PARQUET).read_daily([START])
    )
