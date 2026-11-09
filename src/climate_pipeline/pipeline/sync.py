"""Database-driven operation: keep PostgreSQL complete and current between two dates.

``sync`` is the scheduled job (``.github/workflows/db-sync.yml``):

1. apply pending migrations and refresh the ``variable`` dictionary;
2. ask the database (``ingest_manifest``) which ``(variable, day)`` are new, missing or still
   preliminary between ``start`` and ``end`` (default: today), honouring each source's
   publication latency; ``missing`` days older than ``lookback_days`` are not retried;
3. fetch only those (Bronze → Silver on the runner's disk) and upsert Gold on every grid,
   column by column, plus the epi weeks they touch;
4. build the static covariates of grids that do not have them yet;
5. report coverage: per source and grid, days in Gold, gaps and how far behind real time.

``import_lake`` seeds the database from an existing local lake (Parquet Gold + manifests),
so a historical backfill does not have to be downloaded again.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pandas as pd

from climate_pipeline.config import PipelineConfig, Settings
from climate_pipeline.domain.aoi import AreaOfInterest
from climate_pipeline.domain.dates import EpiWeek
from climate_pipeline.pipeline.coverage import coverage_report
from climate_pipeline.pipeline.orchestrator import ExtractorFactory, NamedGrid, Pipeline, RunRequest
from climate_pipeline.storage.catalog import GOLD_PREFIX, ParquetCatalog
from climate_pipeline.storage.gold import GoldTable, OutputFormat
from climate_pipeline.storage.layout import DataLake
from climate_pipeline.storage.postgres import Database, PostgresCatalog, PostgresGoldStore, database_from

logger = logging.getLogger(__name__)


@dataclass
class SyncRequest:
    aoi_path: Path
    sources: list[str]
    start: date
    end: date | None
    target_grids: list[NamedGrid]
    nested: bool = False
    static_layers: list[str] = field(default_factory=list)
    static_year: int | None = None
    max_behind_days: int = 3
    today: date | None = None

    @property
    def effective_today(self) -> date:
        return self.today or datetime.now(UTC).date()

    @property
    def effective_end(self) -> date:
        return self.end or self.effective_today


def open_database(settings: Settings) -> Database:
    return database_from(settings)


def _static_missing(store: PostgresGoldStore) -> bool:
    return int(store.query(f"SELECT count(*) AS n FROM {GoldTable.STATIC}")["n"].iloc[0]) == 0


def sync(
    settings: Settings,
    config: PipelineConfig,
    request: SyncRequest,
    *,
    lake: DataLake | None = None,
    extractor_factories: dict[str, ExtractorFactory] | None = None,
) -> dict:
    database = open_database(settings)
    applied = database.migrate()
    pipeline = Pipeline(
        settings,
        config,
        lake=lake,
        extractor_factories=extractor_factories,
        catalog=PostgresCatalog(database),
        output_format=OutputFormat.POSTGRES,
    )
    try:
        database.sync_variables(pipeline.variables, pipeline.gold_columns())
        run = pipeline.run(
            RunRequest(
                aoi_path=request.aoi_path,
                sources=request.sources,
                start_date=request.start,
                end_date=request.effective_end,
                target_grids=request.target_grids,
                output_format=OutputFormat.POSTGRES,
                today=request.effective_today,
                nested=request.nested,
                retry_within_lookback=True,
            )
        )

        stores = {
            grid.name: PostgresGoldStore(pipeline.lake.gold_layout(grid.name), database)
            for grid in request.target_grids
        }
        static_report: dict[str, object] = {}
        pending_static = [name for name, store in stores.items() if store.layout.grid_path.exists()]
        pending_static = [name for name in pending_static if _static_missing(stores[name])]
        if request.static_layers and pending_static:
            from climate_pipeline.pipeline.static import build_static_covariates

            try:
                frames = build_static_covariates(
                    pipeline.lake,
                    pending_static,
                    AreaOfInterest.from_file(request.aoi_path),
                    OutputFormat.POSTGRES,
                    layers=request.static_layers,
                    year=request.static_year or request.effective_end.year,
                    settings=settings,
                    nested=request.nested,
                )
                static_report = {name: len(frame) for name, frame in frames.items()}
            except Exception as exc:  # static layers are not worth failing the daily data for
                logger.exception("static covariates failed")
                static_report = {"error": repr(exc)}

        coverage = {
            name: coverage_report(
                store,
                pipeline.catalog,
                pipeline.variables,
                config,
                request.sources,
                start=request.start,
                end=request.effective_end,
                today=request.effective_today,
                max_behind_days=request.max_behind_days,
            )
            for name, store in stores.items()
        }
        return {
            "ok": run.ok and "error" not in static_report,
            "coverage_ok": all(item["ok"] for item in coverage.values()),
            "migrations_applied": applied,
            "run": run.to_dict(),
            "static": static_report,
            "coverage": coverage,
            "database": {name: frame.to_dict(orient="records") for name, frame in database.status().items()},
        }
    finally:
        pipeline.close()


def _parquet_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.parquet")) if directory.exists() else []


def _read_parquet(path: Path) -> pd.DataFrame:
    with duckdb.connect() as connection:
        return connection.execute(
            "SELECT * FROM read_parquet(?, hive_partitioning = false)", [str(path)]
        ).df()


def import_lake(
    lake: DataLake,
    database: Database,
    grid_names: Sequence[str] | None = None,
    *,
    tables: Sequence[GoldTable] = tuple(GoldTable),
) -> dict:
    """Copy a local lake (Parquet Gold of each grid + Parquet manifests) into PostgreSQL.

    Idempotent: Gold rows are upserted and manifest rows only replace older versions. Gold
    builds recorded for the Parquet store are recorded for the Postgres store too, so the next
    ``sync`` does not rebuild what was imported.
    """
    database.migrate()
    catalog = PostgresCatalog(database)
    manifests = ParquetCatalog(lake)
    imported: dict[str, object] = {"manifest_rows": 0, "grids": {}}

    rows = [manifests.entries(source) for source in manifests.sources()]
    directory = lake.state / "manifest"
    for path in sorted(directory.glob(f"{GOLD_PREFIX}*__parquet.parquet")) if directory.exists() else []:
        frame = pd.read_parquet(path)
        frame["source"] = frame["source"].str.replace(r"__parquet$", "__postgres", regex=True)
        rows.append(frame)
    rows = [frame for frame in rows if not frame.empty]
    if rows:
        imported["manifest_rows"] = catalog.import_rows(pd.concat(rows, ignore_index=True))

    found = (
        sorted(p.name for p in lake.gold.iterdir() if (p / "grid.json").exists())
        if lake.gold.exists()
        else []
    )
    names = list(grid_names or found)
    for name in names:
        layout = lake.gold_layout(name)
        store = PostgresGoldStore(layout, database)
        counts = {}
        root = layout.parquet_root
        for table, folder in (
            (GoldTable.DAILY, root / "daily"),
            (GoldTable.EPIWEEKLY, root / "epiweekly"),
            (GoldTable.STATIC, root / "static"),
        ):
            if table not in tables:
                continue
            files = _parquet_files(folder)
            for path in files:
                frame = _read_parquet(path)
                if table is GoldTable.DAILY:
                    frame["date"] = pd.to_datetime(frame["date"]).dt.date
                    store.upsert_daily(frame["date"].iloc[0], frame)
                elif table is GoldTable.EPIWEEKLY:
                    for column in ("week_start", "week_end"):
                        frame[column] = pd.to_datetime(frame[column]).dt.date
                    store.upsert_weekly(
                        EpiWeek(int(frame["epi_year"].iloc[0]), int(frame["epi_week"].iloc[0])), frame
                    )
                else:
                    store.write_static(frame)
            counts[str(table)] = len(files)
            logger.info("import: %s/%s ← %d Parquet files", name, table, len(files))
        imported["grids"][name] = counts  # type: ignore[index]
    return imported
