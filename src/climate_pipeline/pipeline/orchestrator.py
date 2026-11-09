"""End-to-end orchestration: plan → Bronze → Silver → Gold (one store per target grid)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from climate_pipeline.config import PipelineConfig, Settings
from climate_pipeline.domain.aoi import AreaOfInterest
from climate_pipeline.domain.artifacts import BronzeArtifact, Layer, MissingData, ProductStatus
from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.domain.redundancy import Decision, decide, dropped_columns
from climate_pipeline.domain.variables import VariableCatalog, default_specs
from climate_pipeline.extractors import EXTRACTORS, BaseExtractor
from climate_pipeline.extractors.base import ExtractionFailure
from climate_pipeline.extractors.gee import gee_variable_specs
from climate_pipeline.extractors.gee_datasets import gee_dataset_specs
from climate_pipeline.pipeline.gold import GoldBuilder, GoldBuildReport, NestedGoldBuilder, plan_nested_grids
from climate_pipeline.pipeline.latency import SourcePlan, plan_source
from climate_pipeline.processing.harmonize import SilverInput, build_silver_dataset
from climate_pipeline.processing.quality import BoundsReport
from climate_pipeline.storage.catalog import Catalog, CatalogEntry, ParquetCatalog
from climate_pipeline.storage.gold import OutputFormat
from climate_pipeline.storage.layout import DataLake, validate_grid_name
from climate_pipeline.storage.netcdf import read_netcdf, write_netcdf_atomic

logger = logging.getLogger(__name__)

ExtractorFactory = Callable[..., BaseExtractor]
_CATALOG_BATCH = 50
_MAX_REPORTED_FAILURES = 20


@dataclass(frozen=True)
class NamedGrid:
    """A Gold target grid: ``name`` selects ``gold/{name}``; ``spec`` is ``--target-grid``."""

    name: str
    spec: str | None

    @classmethod
    def parse(cls, value: str | None) -> NamedGrid:
        """``1km=EPSG:9377@1000`` | ``EPSG:4326@0.05`` | ``path/grid.tif`` | ``None``."""
        if value is None:
            return cls("native", None)
        name, separator, spec = value.partition("=")
        if separator:
            return cls(validate_grid_name(name.strip()), spec.strip())
        is_resolution_spec = "@" in value and not Path(value).exists()
        slug = re.sub(r"[^A-Za-z0-9.]+", "_", value) if is_resolution_spec else Path(value).stem
        return cls(validate_grid_name(slug.strip("_").lower()), value)


@dataclass
class RunRequest:
    aoi_path: Path
    sources: list[str]
    start_date: date | None = None
    end_date: date | None = None
    target_grids: list[NamedGrid] = field(default_factory=lambda: [NamedGrid.parse(None)])
    output_format: OutputFormat = OutputFormat.PARQUET
    incremental: bool = False
    force: bool = False
    build_gold: bool = True
    today: date | None = None
    #: Derive every grid from the finest one (block means) instead of resampling each.
    nested: bool = False
    #: Retry ``missing`` days only inside each source's ``lookback_days`` (``sync`` over long
    #: windows); older ones were not published (no overpass) and are not asked for again.
    retry_within_lookback: bool = False

    @property
    def effective_today(self) -> date:
        return self.today or datetime.now(UTC).date()


@dataclass
class SourceReport:
    source: str
    plan: dict[str, Any] = field(default_factory=dict)
    fetched: int = 0
    final: int = 0
    prelim: int = 0
    missing: int = 0
    failures: int = 0
    failure_samples: list[str] = field(default_factory=list)
    silver_days: int = 0
    silver_failures: int = 0
    quality: dict[str, dict[str, int]] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.failures == 0 and self.silver_failures == 0

    def add_quality(self, report: BoundsReport) -> None:
        current = self.quality.get(report.variable)
        merged = report if current is None else BoundsReport(report.variable, **current).merge(report)
        self.quality[report.variable] = {
            "total_values": merged.total_values,
            "valid_values": merged.valid_values,
            "non_finite": merged.non_finite,
            "below_minimum": merged.below_minimum,
            "above_maximum": merged.above_maximum,
            "clipped": merged.clipped,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "ok": self.ok}


@dataclass
class RunReport:
    started_at: str
    request: dict[str, Any]
    sources: list[SourceReport] = field(default_factory=list)
    gold: list[GoldBuildReport] = field(default_factory=list)
    gold_errors: dict[str, str] = field(default_factory=dict)
    finished_at: str | None = None

    @property
    def ok(self) -> bool:
        return all(source.ok for source in self.sources) and not self.gold_errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "request": self.request,
            "sources": [source.to_dict() for source in self.sources],
            "gold": [report.to_dict() for report in self.gold],
            "gold_errors": self.gold_errors,
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        return path


def _all_specs(config: PipelineConfig) -> list:
    gee_products = config.sources["gee"].options.get("products", []) if "gee" in config.sources else []
    return [*default_specs(), *gee_variable_specs(gee_products), *gee_dataset_specs()]


def redundancy_decisions(config: PipelineConfig) -> list[Decision]:
    """Which default columns duplicate a better one (finer, then faster, then more sensible)."""
    available = [
        spec.gold_column for spec in _all_specs(config) if spec.default and spec.source in config.sources
    ]

    def latency_of(source: str) -> int | None:
        return config.sources[source].latency.earliest_lag_days if source in config.sources else None

    return decide(available, latency_of)


def build_variable_catalog(config: PipelineConfig) -> VariableCatalog:
    specs = _all_specs(config)
    if config.deduplicate:
        dropped = dropped_columns(redundancy_decisions(config))
        specs = [replace(spec, default=False) if spec.gold_column in dropped else spec for spec in specs]
    return VariableCatalog(specs)


def open_catalog(settings: Settings, lake: DataLake, output_format: OutputFormat | str) -> Catalog:
    """The catalog lives with Gold: Postgres Gold → ``ingest_manifest``; file Gold → Parquet manifests."""
    if OutputFormat(output_format) is OutputFormat.POSTGRES:
        from climate_pipeline.storage.postgres import PostgresCatalog, database_from

        return PostgresCatalog(database_from(settings))
    return ParquetCatalog(lake)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        config: PipelineConfig,
        lake: DataLake | None = None,
        extractor_factories: dict[str, ExtractorFactory] | None = None,
        catalog: Catalog | None = None,
        output_format: OutputFormat | str = OutputFormat.PARQUET,
    ) -> None:
        self.settings = settings
        self.config = config
        self.lake = lake or DataLake(settings.climate_data_root)
        self.output_format = OutputFormat(output_format)
        self.catalog = catalog or open_catalog(settings, self.lake, self.output_format)
        self.variables = build_variable_catalog(config)
        self._factories: dict[str, ExtractorFactory] = {**EXTRACTORS, **(extractor_factories or {})}

    # --- planning -------------------------------------------------------------------
    def _validate_sources(self, sources: Sequence[str]) -> None:
        unknown = sorted(set(sources) - set(self._factories))
        if unknown:
            raise ConfigurationError(f"unknown sources {unknown}; available: {sorted(self._factories)}")

    def gold_columns(self) -> set[str]:
        """Selected Gold columns: each configured source's explicit or default variables."""
        return {
            spec.gold_column
            for source, source_config in self.config.sources.items()
            for spec in self.variables.for_source(source, source_config.variables)
        }

    def source_variables(self, source: str) -> list[str]:
        source_config = self.config.source(source)
        return [spec.name for spec in self.variables.for_source(source, source_config.variables)]

    def plan(self, request: RunRequest) -> dict[str, SourcePlan]:
        self._validate_sources(request.sources)
        return {
            source: plan_source(
                source,
                self.config.source(source).latency,
                self.source_variables(source),
                self.catalog.status_map(source, Layer.BRONZE),
                today=request.effective_today,
                start=request.start_date,
                end=request.end_date,
                incremental=request.incremental,
                force=request.force,
                retry_since=(
                    request.effective_today - timedelta(days=self.config.source(source).latency.lookback_days)
                    if request.retry_within_lookback
                    else None
                ),
            )
            for source in request.sources
        }

    # --- Bronze + Silver ------------------------------------------------------------
    def ingest(self, request: RunRequest, aoi: AreaOfInterest) -> list[SourceReport]:
        """Bronze → Silver for every source, ``config.parallel_sources`` sources at a time.

        Providers are independent (the CDS queue of AgERA5 says nothing about Earth Engine), so
        running sources side by side cuts the wall time to roughly that of the slowest source.
        """
        plans = self.plan(request)
        reports = {source: SourceReport(source=source, plan=plan.to_dict()) for source, plan in plans.items()}

        def ingest_source(source: str) -> None:
            plan, report = plans[source], reports[source]
            if plan.total_requests == 0:
                logger.info("%s: nothing to fetch (%s)", source, dict(plan.reasons))
                return
            try:
                touched = self._extract(source, plan, aoi, report)
                self._build_silver(source, sorted(touched), aoi, report)
            except Exception as exc:  # source-level failure (credentials, config...)
                logger.exception("%s: ingestion aborted", source)
                report.error = repr(exc)

        workers = max(1, min(self.config.parallel_sources, len(plans)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="source") as pool:
            list(pool.map(ingest_source, plans))
        return list(reports.values())

    def _extract(self, source: str, plan: SourcePlan, aoi: AreaOfInterest, report: SourceReport) -> set[date]:
        specs = [self.variables.get(source, name) for name in plan.requests]
        extractor = self._factories[source](self.lake, aoi, self.config.source(source), self.settings, specs)
        logger.info("%s: fetching %d variable-days (%d days)", source, plan.total_requests, len(plan.days))
        touched: set[date] = set()
        pending: list[CatalogEntry] = []
        try:
            for outcome in extractor.fetch(plan.requests):
                if isinstance(outcome, BronzeArtifact):
                    report.fetched += 1
                    if outcome.status is ProductStatus.FINAL:
                        report.final += 1
                    else:
                        report.prelim += 1
                    touched.add(outcome.day)
                    pending.append(
                        CatalogEntry(
                            source,
                            outcome.variable,
                            outcome.day,
                            Layer.BRONZE,
                            outcome.status,
                            self.lake.relative(outcome.path),
                            outcome.origin,
                        )
                    )
                elif isinstance(outcome, MissingData):
                    report.missing += 1
                    pending.append(
                        CatalogEntry(
                            source,
                            outcome.variable,
                            outcome.day,
                            Layer.BRONZE,
                            ProductStatus.MISSING,
                            origin=outcome.reason,
                        )
                    )
                elif isinstance(outcome, ExtractionFailure):
                    report.failures += 1
                    if len(report.failure_samples) < _MAX_REPORTED_FAILURES:
                        report.failure_samples.append(f"{outcome.variable} {outcome.day}: {outcome.error}")
                if len(pending) >= _CATALOG_BATCH:
                    self.catalog.record(pending)
                    pending = []
        finally:
            if pending:
                self.catalog.record(pending)
            extractor.close()
        return touched

    def _build_silver(self, source: str, days: list[date], aoi: AreaOfInterest, report: SourceReport) -> None:
        entries = []
        for day in days:
            try:
                rows = self.catalog.bronze_artifacts(source, day)
                inputs = [
                    SilverInput(
                        read_netcdf(self.lake.resolve(row.path)),
                        self.variables.get(source, row.variable),
                        row.status,
                    )
                    for row in rows.itertuples(index=False)
                ]
                if not inputs:
                    continue
                dataset, quality = build_silver_dataset(source, day, inputs, aoi, self.config.clip_mode)
                path = write_netcdf_atomic(dataset, self.lake.silver_path(source, day))
                status = ProductStatus.worst([ProductStatus(item.status) for item in inputs])
                entries.append(CatalogEntry(source, "*", day, Layer.SILVER, status, self.lake.relative(path)))
                report.silver_days += 1
                for item in quality:
                    report.add_quality(item)
            except Exception as exc:
                logger.exception("%s: silver failed for %s", source, day)
                report.silver_failures += 1
                if len(report.failure_samples) < _MAX_REPORTED_FAILURES:
                    report.failure_samples.append(f"silver {day}: {exc!r}")
        self.catalog.record(entries)

    # --- Gold -----------------------------------------------------------------------
    def build_gold(
        self, request: RunRequest, aoi: AreaOfInterest
    ) -> tuple[list[GoldBuildReport], dict[str, str]]:
        """Gold on every target grid. Days are the outer loop: each day's Silver is read once and
        resampled onto every grid that needs that day (grids fail independently)."""
        if request.nested:
            return self._build_nested_gold(request, aoi)
        reports, errors = [], {}
        builders: dict[str, GoldBuilder] = {}
        pending: dict[str, set[date]] = {}
        states: dict[str, tuple] = {}
        try:
            for grid in request.target_grids:
                builder = GoldBuilder(
                    self.lake,
                    self.catalog,
                    self.variables,
                    aoi,
                    grid.name,
                    request.output_format,
                    columns=self.gold_columns(),
                    settings=self.settings,
                )
                builders[grid.name] = builder
                try:
                    days = self._window(request, self.catalog.pending_gold_dates(builder.build_key))
                    if days:
                        resolved = builder.resolve_grid(grid.spec, days)
                        cells = builder.aoi_cells(resolved)
                        states[grid.name] = (
                            resolved,
                            cells,
                            builder.new_report(resolved, int(cells[0].size)),
                        )
                        pending[grid.name] = set(days)
                    elif weeks := builder.refresh_weeks(builder.store.stale_weeks()):
                        logger.info("gold/%s: rebuilt stale epi weeks %s", grid.name, weeks)
                    else:
                        logger.info("gold/%s: up to date", grid.name)
                except Exception as exc:
                    logger.exception("gold/%s failed", grid.name)
                    errors[grid.name] = repr(exc)

            for day in sorted(set().union(*pending.values())):
                silver = None
                for name in [name for name in states if day in pending[name]]:
                    builder, (resolved, cells, report) = builders[name], states[name]
                    try:
                        silver = builder.load_silver(day) if silver is None else silver
                        layers, sources = builder.resample_day(day, resolved, silver)
                        frame, violations = builder.frame_from_layers(day, layers, *cells)
                        builder.write_day(day, frame, violations, report, sources)
                    except Exception as exc:
                        logger.exception("gold/%s failed on %s", name, day)
                        errors[name] = repr(exc)
                        del states[name]

            for name, (_, _, report) in states.items():
                try:
                    reports.append(builders[name].finish(report))
                except Exception as exc:
                    logger.exception("gold/%s failed", name)
                    errors[name] = repr(exc)
        finally:
            for builder in builders.values():
                builder.close()
        if reports and request.output_format is not OutputFormat.POSTGRES:
            from climate_pipeline.pipeline.provenance import write_product_status

            write_product_status(self.lake, self.catalog)  # Postgres: views over ingest_manifest
        return reports, errors

    @staticmethod
    def _window(request: RunRequest, days: list[date]) -> list[date]:
        """Pending days restricted to the request's explicit dates (all of them when incremental)."""
        if request.incremental or not (request.start_date and request.end_date):
            return days
        return [day for day in days if request.start_date <= day <= request.end_date]

    def _build_nested_gold(
        self, request: RunRequest, aoi: AreaOfInterest
    ) -> tuple[list[GoldBuildReport], dict[str, str]]:
        targets = []
        for grid in request.target_grids:
            if grid.spec is None:
                raise ConfigurationError("--nested needs explicit grids such as 100m=EPSG:9377@100")
            targets.append((grid.name, grid.spec))
        grids = plan_nested_grids(targets, aoi)
        builder = NestedGoldBuilder(
            self.lake,
            self.catalog,
            self.variables,
            aoi,
            grids,
            request.output_format,
            columns=self.gold_columns(),
            settings=self.settings,
        )
        try:
            days = self._window(request, builder.pending_days())
            if not days:
                logger.info("gold (nested %s): up to date", [grid.name for grid in grids])
                return [], {}
            logger.info(
                "gold (nested): template %s, derived %s, %d days",
                grids[0].name,
                [grid.name for grid in grids[1:]],
                len(days),
            )
            return builder.build(days), {}
        except Exception as exc:
            logger.exception("nested gold failed")
            return [], {"+".join(grid.name for grid in grids): repr(exc)}
        finally:
            builder.close()

    def run(self, request: RunRequest) -> RunReport:
        aoi = AreaOfInterest.from_file(request.aoi_path)
        report = RunReport(
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            request={
                "aoi": str(request.aoi_path),
                "aoi_bounds": [round(value, 5) for value in aoi.bounds],
                "sources": request.sources,
                "start_date": request.start_date,
                "end_date": request.end_date,
                "target_grids": [grid.__dict__ for grid in request.target_grids],
                "output_format": request.output_format.value,
                "incremental": request.incremental,
                "nested": request.nested,
                "force": request.force,
                "retry_within_lookback": request.retry_within_lookback,
                "today": request.effective_today,
            },
        )
        report.sources = self.ingest(request, aoi)
        if request.build_gold:
            report.gold, report.gold_errors = self.build_gold(request, aoi)
        report.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        return report

    def close(self) -> None:
        self.catalog.close()
