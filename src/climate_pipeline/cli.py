"""Command line interface: ``uv run climate-pipeline --help``.

Every option of ``run``, ``plan``, ``build-gold``, ``build-static``, ``coverage`` and ``sync``
falls back to the ``run:`` section of ``--config`` (AOI, sources, dates, grids, back-end,
static layers), so a declared exercise runs with ``climate-pipeline sync --config X``.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import typer

from climate_pipeline.config import PipelineConfig, Settings, load_pipeline_config
from climate_pipeline.domain.aoi import AreaOfInterest
from climate_pipeline.domain.dates import EpiWeek
from climate_pipeline.domain.errors import ClimatePipelineError, ConfigurationError
from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.pipeline.orchestrator import NamedGrid, Pipeline, RunRequest
from climate_pipeline.storage.gold import OutputFormat, open_gold_store
from climate_pipeline.storage.layout import DataLake
from climate_pipeline.storage.raster import make_raster

app = typer.Typer(
    name="climate-pipeline",
    help="Medallion pipeline (Bronze → Silver → Gold) for daily climate and satellite sources.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
db_app = typer.Typer(help="PostgreSQL: schema, status and import of a local lake.", no_args_is_help=True)
app.add_typer(db_app, name="db")

DateOption = Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], show_default=False)]
TodayOption = Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], hidden=True)]
AoiOption = Annotated[
    Path | None,
    typer.Option(
        "--aoi-path",
        exists=True,
        dir_okay=False,
        help="Shapefile/GeoJSON/GPKG of the AOI (default: run.aoi_path).",
    ),
]
SourcesOption = Annotated[
    list[str] | None,
    typer.Option(
        "--sources",
        help="Sources (repeat or comma-separate): chirps, mswep, agera5, gee, era5land, imerg, gsmap, "
        "modis, viirs, smap, landsat, goes, sentinel1. Default: run.sources, else every source with "
        "selected columns.",
    ),
]
GridOption = Annotated[
    list[str] | None,
    typer.Option(
        "--target-grid",
        help="Target grid, repeatable. 'name=EPSG:9377@1000', 'EPSG:4326@0.05' or a template raster. "
        "Each grid gets its own Gold store. Default: run.target_grids, else the finest native grid.",
    ),
]
FormatOption = Annotated[
    OutputFormat | None,
    typer.Option("--output-format", help="Gold back-end (default: run.output_format, else parquet)."),
]
NestedOption = Annotated[
    bool | None,
    typer.Option(
        "--nested/--independent",
        help="--nested: resample Silver once onto the finest --target-grid and derive every coarser grid "
        "as exact block means of it. --independent: resample Silver onto each grid. Default: run.nested.",
        show_default=False,
    ),
]
ConfigOption = Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)]
DataRootOption = Annotated[Path | None, typer.Option("--data-root", help="Overrides CLIMATE_DATA_ROOT.")]
LogOption = Annotated[str, typer.Option("--log-level")]
ReportOption = Annotated[Path | None, typer.Option("--report-path", help="Write the JSON report here.")]


# --- shared setup --------------------------------------------------------------------------
def _logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    for noisy in ("httpx", "httpcore", "rasterio", "urllib3", "googleapiclient", "cdsapi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _settings(data_root: Path | None) -> Settings:
    settings = Settings()
    if data_root is not None:
        settings = settings.model_copy(update={"climate_data_root": data_root})
    return settings


def _split(values: list[str] | None) -> list[str]:
    return [item.strip().lower() for value in values or [] for item in value.split(",") if item.strip()]


def _to_date(value: datetime | None) -> date | None:
    return value.date() if value else None


def _fail(exc: Exception) -> None:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2) from exc


@dataclass
class Exercise:
    """CLI options merged with the ``run:`` section of the configuration."""

    config: PipelineConfig
    settings: Settings
    aoi_path: Path | None
    sources: list[str]
    start: date | None
    end: date | None
    grids: list[NamedGrid]
    nested: bool
    output_format: OutputFormat
    today: date

    @classmethod
    def resolve(
        cls,
        config_path: Path | None,
        data_root: Path | None,
        *,
        aoi_path: Path | None = None,
        sources: list[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        target_grid: list[str] | None = None,
        nested: bool | None = None,
        output_format: OutputFormat | None = None,
        today: datetime | None = None,
    ) -> Exercise:
        config = load_pipeline_config(config_path)
        defaults = config.run
        if target_grid:
            grids = [NamedGrid.parse(value) for value in target_grid]
        elif defaults.target_grids:
            grids = [NamedGrid.parse(f"{name}={spec}") for name, spec in defaults.target_grids.items()]
        else:
            grids = [NamedGrid.parse(None)]
        names = [grid.name for grid in grids]
        if len(set(names)) != len(names):
            raise typer.BadParameter(f"duplicated grid names: {names}", param_hint="--target-grid")
        return cls(
            config=config,
            settings=_settings(data_root),
            aoi_path=aoi_path or defaults.aoi_path,
            sources=_split(sources) or list(defaults.sources),
            start=_to_date(start) or defaults.start_date,
            end=_to_date(end) or defaults.end_date,
            grids=grids,
            nested=defaults.nested if nested is None else nested,
            output_format=output_format or OutputFormat(defaults.output_format),
            today=_to_date(today) or datetime.now(UTC).date(),
        )

    def pipeline(self) -> Pipeline:
        return Pipeline(self.settings, self.config, output_format=self.output_format)

    def require_aoi(self) -> Path:
        if self.aoi_path is None:
            raise typer.BadParameter(
                "pass --aoi-path or set run.aoi_path in --config", param_hint="--aoi-path"
            )
        if not self.aoi_path.exists():
            raise typer.BadParameter(f"AOI not found: {self.aoi_path}", param_hint="--aoi-path")
        return self.aoi_path

    def sources_or_selected(self, pipeline: Pipeline) -> list[str]:
        """Explicit sources, else every configured source that contributes Gold columns."""
        if self.sources:
            return self.sources
        return [
            source
            for source, source_config in self.config.sources.items()
            if pipeline.variables.for_source(source, source_config.variables)
        ]

    def request(
        self,
        pipeline: Pipeline,
        *,
        incremental: bool,
        force: bool = False,
        build_gold: bool = True,
        retry_old_missing: bool = True,
    ) -> RunRequest:
        return RunRequest(
            aoi_path=self.require_aoi(),
            sources=self.sources_or_selected(pipeline),
            start_date=self.start,
            end_date=self.end or (None if incremental else self.today),
            target_grids=self.grids,
            output_format=self.output_format,
            incremental=incremental,
            force=force,
            build_gold=build_gold,
            today=self.today,
            nested=self.nested,
            retry_within_lookback=not retry_old_missing,
        )


def _write_json(path: Path | None, payload: dict) -> None:
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str))


# --- commands ------------------------------------------------------------------------------
@app.command()
def run(
    aoi_path: AoiOption = None,
    sources: SourcesOption = None,
    start_date: DateOption = None,
    end_date: DateOption = None,
    target_grid: GridOption = None,
    output_format: FormatOption = None,
    nested: NestedOption = None,
    incremental: Annotated[bool, typer.Option(help="Latency-aware window instead of fixed dates.")] = False,
    force: Annotated[bool, typer.Option(help="Re-download even if Bronze is final.")] = False,
    skip_gold: Annotated[bool, typer.Option(help="Stop after Silver (CI ingest jobs).")] = False,
    retry_old_missing: Annotated[
        bool,
        typer.Option(
            help="Retry every day recorded as missing; --no-retry-old-missing only retries those inside "
            "each source's lookback_days (what `sync` does)."
        ),
    ] = True,
    report_path: ReportOption = None,
    today: TodayOption = None,
    config: ConfigOption = None,
    data_root: DataRootOption = None,
    log_level: LogOption = "INFO",
) -> None:
    """Run Bronze → Silver → Gold for the requested sources and dates (end date: today)."""
    _logging(log_level)
    try:
        exercise = Exercise.resolve(
            config,
            data_root,
            aoi_path=aoi_path,
            sources=sources,
            start=start_date,
            end=end_date,
            target_grid=target_grid,
            nested=nested,
            output_format=output_format,
            today=today,
        )
        pipeline = exercise.pipeline()
        try:
            report = pipeline.run(
                exercise.request(
                    pipeline,
                    incremental=incremental,
                    force=force,
                    build_gold=not skip_gold,
                    retry_old_missing=retry_old_missing,
                )
            )
        finally:
            pipeline.close()
    except ClimatePipelineError as exc:
        _fail(exc)
        return
    payload = report.to_dict()
    if report_path:
        report.save(report_path)
    typer.echo(json.dumps(_summary(payload), indent=2, default=str))
    if not report.ok:
        raise typer.Exit(code=1)


def _summary(payload: dict) -> dict:
    return {
        "ok": payload["ok"],
        "sources": {
            source["source"]: {
                key: source[key]
                for key in ("fetched", "final", "prelim", "missing", "failures", "silver_days", "error")
            }
            for source in payload["sources"]
        },
        "gold": {
            gold["grid_name"]: {
                key: gold[key]
                for key in (
                    "grid_shape",
                    "cells_in_aoi",
                    "days_built",
                    "weeks_built",
                    "daily_rows",
                    "weekly_rows",
                )
            }
            for gold in payload["gold"]
        },
        "gold_errors": payload["gold_errors"],
    }


@app.command()
def plan(
    aoi_path: AoiOption = None,
    sources: SourcesOption = None,
    start_date: DateOption = None,
    end_date: DateOption = None,
    output_format: FormatOption = None,
    incremental: bool = True,
    force: bool = False,
    retry_old_missing: Annotated[
        bool,
        typer.Option(
            help="Retry every day recorded as missing; --no-retry-old-missing only retries those inside "
            "each source's lookback_days (what `sync` does)."
        ),
    ] = True,
    today: TodayOption = None,
    github_matrix: Annotated[
        bool, typer.Option(help="Print a GitHub Actions matrix of sources with work.")
    ] = False,
    config: ConfigOption = None,
    data_root: DataRootOption = None,
    log_level: LogOption = "WARNING",
) -> None:
    """Show which (variable, date) pairs each source would fetch, given its latency and the catalog
    of the chosen back-end (Postgres: what the database is missing)."""
    _logging(log_level)
    try:
        exercise = Exercise.resolve(
            config,
            data_root,
            aoi_path=aoi_path,
            sources=sources,
            start=start_date,
            end=end_date,
            output_format=output_format,
            today=today,
        )
        pipeline = exercise.pipeline()
        try:
            plans = pipeline.plan(
                exercise.request(
                    pipeline, incremental=incremental, force=force, retry_old_missing=retry_old_missing
                )
            )
        finally:
            pipeline.close()
    except ClimatePipelineError as exc:
        _fail(exc)
        return
    if github_matrix:
        pending = [name for name, item in plans.items() if item.total_requests > 0]
        typer.echo(json.dumps({"source": pending}))
        return
    typer.echo(json.dumps({name: item.to_dict() for name, item in plans.items()}, indent=2))


@app.command("build-gold")
def build_gold(
    aoi_path: AoiOption = None,
    target_grid: GridOption = None,
    output_format: FormatOption = None,
    nested: NestedOption = None,
    start_date: DateOption = None,
    end_date: DateOption = None,
    report_path: ReportOption = None,
    config: ConfigOption = None,
    data_root: DataRootOption = None,
    log_level: LogOption = "INFO",
) -> None:
    """Rebuild Gold for every date whose Silver changed since the last Gold build."""
    _logging(log_level)
    try:
        exercise = Exercise.resolve(
            config,
            data_root,
            aoi_path=aoi_path,
            start=start_date,
            end=end_date,
            target_grid=target_grid,
            nested=nested,
            output_format=output_format,
        )
        pipeline = exercise.pipeline()
        try:
            request = exercise.request(pipeline, incremental=start_date is None)
            reports, errors = pipeline.build_gold(request, AreaOfInterest.from_file(request.aoi_path))
        finally:
            pipeline.close()
    except ClimatePipelineError as exc:
        _fail(exc)
        return
    payload = {"gold": [item.to_dict() for item in reports], "gold_errors": errors}
    _write_json(report_path, payload)
    typer.echo(json.dumps(payload, indent=2, default=str))
    if errors:
        raise typer.Exit(code=1)


@app.command("build-static")
def build_static(
    aoi_path: AoiOption = None,
    grid_name: Annotated[
        list[str] | None,
        typer.Option(help="Gold store (grid) name, repeatable. Default: every run.target_grids."),
    ] = None,
    layers: Annotated[
        list[str] | None,
        typer.Option(
            "--layers", help="terrain (Copernicus DEM), urban (Earth Engine), osm. Default: run.static."
        ),
    ] = None,
    year: Annotated[
        int | None,
        typer.Option(help="Year of the urban composites (default: run.static.year, else this year)."),
    ] = None,
    output_format: FormatOption = None,
    nested: NestedOption = None,
    config: ConfigOption = None,
    data_root: DataRootOption = None,
    log_level: LogOption = "INFO",
) -> None:
    """Static covariates (terrain, urban form, OSM roads) on one or several Gold grids."""
    from climate_pipeline.pipeline.static import build_static_covariates

    _logging(log_level)
    try:
        exercise = Exercise.resolve(
            config, data_root, aoi_path=aoi_path, nested=nested, output_format=output_format
        )
        static = exercise.config.run.static
        names = _split(grid_name) or [grid.name for grid in exercise.grids if grid.spec is not None]
        if not names:
            raise typer.BadParameter("pass --grid-name or set run.target_grids", param_hint="--grid-name")
        frames = build_static_covariates(
            DataLake(exercise.settings.climate_data_root),
            names,
            AreaOfInterest.from_file(exercise.require_aoi()),
            exercise.output_format,
            layers=_split(layers) or list(static.layers),
            year=year or static.year or (exercise.end or exercise.today).year,
            settings=exercise.settings,
            nested=exercise.nested,
        )
    except ClimatePipelineError as exc:
        _fail(exc)
        return
    for name, frame in frames.items():
        typer.echo(f"== {name}: {len(frame)} cells")
        typer.echo(frame.drop(columns=["cell_id"]).describe().round(3).T.to_string())


@app.command("make-raster")
def make_raster_command(
    grid_name: Annotated[str, typer.Option(help="Gold store (grid) name.")],
    variable: Annotated[str, typer.Option(help="Gold column, e.g. imerg_precip.")],
    output: Annotated[Path, typer.Option(help="Output .tif path.")],
    day: DateOption = None,
    epi_week: Annotated[str | None, typer.Option(help="Epidemiological week as YYYY-Www.")] = None,
    template: Annotated[str | None, typer.Option(help="Optional template grid to resample to.")] = None,
    aoi_path: Annotated[
        Path | None, typer.Option(exists=True, help="Needed only with '@' templates.")
    ] = None,
    output_format: Annotated[OutputFormat, typer.Option("--output-format")] = OutputFormat.PARQUET,
    cog: Annotated[bool, typer.Option(help="Write a Cloud-Optimized GeoTIFF.")] = True,
    data_root: DataRootOption = None,
    log_level: LogOption = "WARNING",
) -> None:
    """Rebuild a GeoTIFF/COG from a Gold table (daily, epi-weekly or static)."""
    _logging(log_level)
    settings = _settings(data_root)
    store = None
    try:
        week = None
        if epi_week:
            year, _, number = epi_week.upper().partition("-W")
            week = EpiWeek(int(year), int(number))
        template_grid = None
        if template:
            aoi = AreaOfInterest.from_file(aoi_path) if aoi_path else None
            if "@" in template and aoi is None:
                raise typer.BadParameter("'@' templates need --aoi-path", param_hint="--template")
            template_grid = GridSpec.parse(template, aoi) if aoi else GridSpec.from_raster(template)
        pipeline = Pipeline(settings, load_pipeline_config(None), lake=DataLake(settings.climate_data_root))
        store = open_gold_store(pipeline.lake.gold_layout(grid_name), output_format, settings)
        path = make_raster(
            store,
            variable,
            output,
            day=_to_date(day),
            week=week,
            template=template_grid,
            catalog=pipeline.variables,
            as_cog=cog,
        )
    except (ClimatePipelineError, ValueError) as exc:
        _fail(exc)
        return
    finally:
        if store is not None:
            store.close()
    typer.echo(str(path))


@app.command()
def availability(
    aoi_path: AoiOption = None,
    sources: SourcesOption = None,
    output: Annotated[Path | None, typer.Option(help="Write the JSON report here.")] = None,
    today: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
    config: ConfigOption = None,
    log_level: LogOption = "WARNING",
) -> None:
    """Latest complete day available per source today (live probe), its lag and the last
    complete epidemiological week: the operational horizon of the model."""
    from climate_pipeline.pipeline.availability import availability_report

    _logging(log_level)
    try:
        exercise = Exercise.resolve(config, None, aoi_path=aoi_path, sources=sources, today=today)
        pipeline = Pipeline(exercise.settings, exercise.config)
        report = availability_report(
            exercise.settings,
            exercise.config,
            pipeline.variables,
            AreaOfInterest.from_file(exercise.require_aoi()),
            exercise.sources_or_selected(pipeline),
            today=exercise.today,
        )
    except ClimatePipelineError as exc:
        _fail(exc)
        return
    _write_json(output, report)
    for item in report["sources"]:
        typer.echo(
            f"{item['source']:<10} latest={item['latest_day']} lag={item['lag_days']}d "
            f"week={item['last_complete_epiweek']} status={item['status']} ({item['method']})"
            + (f" ERROR {item['error']}" if item.get("error") else "")
        )
    typer.echo(
        f"all sources complete through {report['all_sources_complete_through']} "
        f"(epi week {report['all_sources_last_complete_epiweek']})"
    )


@app.command()
def coverage(
    sources: SourcesOption = None,
    start_date: DateOption = None,
    end_date: DateOption = None,
    target_grid: GridOption = None,
    output_format: FormatOption = None,
    max_behind_days: Annotated[int | None, typer.Option(help="Default: run.max_behind_days.")] = None,
    fail_on_gaps: Annotated[bool, typer.Option(help="Exit 1 when a source has gaps or is stale.")] = False,
    report_path: ReportOption = None,
    today: TodayOption = None,
    config: ConfigOption = None,
    data_root: DataRootOption = None,
    log_level: LogOption = "WARNING",
) -> None:
    """What Gold holds between --start-date and --end-date (default: today), per source and grid:
    days with data, days the provider never published, gaps and how far behind real time."""
    from climate_pipeline.pipeline.coverage import coverage_report, coverage_table

    _logging(log_level)
    try:
        exercise = Exercise.resolve(
            config,
            data_root,
            sources=sources,
            start=start_date,
            end=end_date,
            target_grid=target_grid,
            output_format=output_format,
            today=today,
        )
        if exercise.start is None:
            raise typer.BadParameter("pass --start-date or set run.start_date", param_hint="--start-date")
        pipeline = exercise.pipeline()
        reports = {}
        try:
            for grid in exercise.grids:
                store = open_gold_store(
                    pipeline.lake.gold_layout(grid.name),
                    exercise.output_format,
                    exercise.settings,
                )
                try:
                    reports[grid.name] = coverage_report(
                        store,
                        pipeline.catalog,
                        pipeline.variables,
                        exercise.config,
                        exercise.sources_or_selected(pipeline),
                        start=exercise.start,
                        end=exercise.end or exercise.today,
                        today=exercise.today,
                        max_behind_days=max_behind_days
                        if max_behind_days is not None
                        else exercise.config.run.max_behind_days,
                    )
                finally:
                    store.close()
        finally:
            pipeline.close()
    except ClimatePipelineError as exc:
        _fail(exc)
        return
    _write_json(report_path, reports)
    for name, report in reports.items():
        typer.echo(f"== {name} ({report['start']} → {report['end']}, today {report['today']})")
        typer.echo(coverage_table(report).to_string(index=False))
    if fail_on_gaps and not all(report["ok"] for report in reports.values()):
        raise typer.Exit(code=1)


@app.command()
def sync(
    aoi_path: AoiOption = None,
    sources: SourcesOption = None,
    start_date: DateOption = None,
    end_date: DateOption = None,
    target_grid: GridOption = None,
    nested: NestedOption = None,
    static: Annotated[
        bool, typer.Option(help="Build static covariates on grids that do not have them yet.")
    ] = True,
    fail_on_gaps: Annotated[
        bool, typer.Option(help="Exit 1 when coverage reports gaps or stale sources.")
    ] = False,
    report_path: ReportOption = None,
    today: TodayOption = None,
    config: ConfigOption = None,
    data_root: DataRootOption = None,
    log_level: LogOption = "INFO",
) -> None:
    """Keep PostgreSQL complete from run.start_date to today: ask the database what is missing,
    fetch only that, upsert Gold on every grid, add static covariates and report coverage."""
    from climate_pipeline.pipeline.coverage import coverage_table
    from climate_pipeline.pipeline.sync import SyncRequest
    from climate_pipeline.pipeline.sync import sync as run_sync

    _logging(log_level)
    try:
        exercise = Exercise.resolve(
            config,
            data_root,
            aoi_path=aoi_path,
            sources=sources,
            start=start_date,
            end=end_date,
            target_grid=target_grid,
            nested=nested,
            output_format=OutputFormat.POSTGRES,
            today=today,
        )
        if exercise.start is None:
            raise typer.BadParameter("pass --start-date or set run.start_date", param_hint="--start-date")
        if any(grid.spec is None for grid in exercise.grids):
            raise typer.BadParameter(
                "sync needs explicit grids (run.target_grids)", param_hint="--target-grid"
            )
        selected = exercise.sources_or_selected(Pipeline(exercise.settings, exercise.config))
        static_config = exercise.config.run.static
        result = run_sync(
            exercise.settings,
            exercise.config,
            SyncRequest(
                aoi_path=exercise.require_aoi(),
                sources=selected,
                start=exercise.start,
                end=exercise.end,
                target_grids=exercise.grids,
                nested=exercise.nested,
                static_layers=list(static_config.layers) if static else [],
                static_year=static_config.year,
                max_behind_days=exercise.config.run.max_behind_days,
                today=exercise.today,
            ),
        )
    except ClimatePipelineError as exc:
        _fail(exc)
        return
    _write_json(report_path, result)
    typer.echo(json.dumps(_summary(result["run"]), indent=2, default=str))
    for name, report in result["coverage"].items():
        typer.echo(f"== coverage {name} ({report['start']} → {report['end']})")
        typer.echo(coverage_table(report).to_string(index=False))
    if not result["ok"] or (fail_on_gaps and not result["coverage_ok"]):
        raise typer.Exit(code=1)


@app.command()
def snapshot(
    tag: Annotated[str | None, typer.Option(help="Snapshot name (default: today, YYYY-MM-DD).")] = None,
    archive: Annotated[bool, typer.Option(help="Also write <tag>.zip next to the folder.")] = False,
    config: ConfigOption = None,
    data_root: DataRootOption = None,
    log_level: LogOption = "INFO",
) -> None:
    """Freeze Gold (every grid), product status and catalog under <lake>/snapshots/<tag>/ with
    SHA-256 checksums, git commit and configuration: the exact data a model was trained on."""
    from climate_pipeline.pipeline.provenance import create_snapshot

    _logging(log_level)
    try:
        exercise = Exercise.resolve(config, data_root, output_format=OutputFormat.PARQUET)
        pipeline = exercise.pipeline()
        manifest = create_snapshot(pipeline.lake, pipeline.catalog, tag, config_path=config, archive=archive)
    except ClimatePipelineError as exc:
        _fail(exc)
        return
    typer.echo(
        json.dumps(
            {key: manifest[key] for key in ("tag", "period", "git_commit", "rows")}, indent=2, default=str
        )
    )


@app.command()
def variables(config: ConfigOption = None) -> None:
    """List the variable catalog (names, units, bounds, resampling, weekly rule) and the
    columns dropped as redundant (finer resolution, then latency, then the sensible product)."""
    from climate_pipeline.pipeline.orchestrator import redundancy_decisions

    config_model = load_pipeline_config(config)
    pipeline = Pipeline(Settings(), config_model, lake=DataLake(Path("data")))
    for spec in pipeline.variables.all():
        typer.echo(
            f"{spec.gold_column:<32} {spec.units:<12} [{spec.bounds.minimum:g}, {spec.bounds.maximum:g}] "
            f"{spec.resampling.value:<8} weekly={spec.weekly_aggregation.value}"
            f"(>={spec.min_valid_days}d) default={spec.default}"
        )
    decisions = redundancy_decisions(config_model)
    if decisions:
        typer.echo(f"\nRedundancy (deduplicate={config_model.deduplicate}): kept ← dropped (reason)")
        for decision in decisions:
            for column, reason in decision.dropped.items():
                typer.echo(f"  {decision.kept:<34} ← {column:<30} ({reason})")


@app.command()
def catalog(
    output_format: FormatOption = None,
    config: ConfigOption = None,
    data_root: DataRootOption = None,
) -> None:
    """Summarise the ingestion catalog by source, layer and status (Parquet manifests or Postgres)."""
    try:
        exercise = Exercise.resolve(config, data_root, output_format=output_format)
        pipeline = exercise.pipeline()
        try:
            typer.echo(pipeline.catalog.summary().to_string(index=False))
        finally:
            pipeline.close()
    except ClimatePipelineError as exc:
        _fail(exc)


# --- db --------------------------------------------------------------------------------------
def _database(config: Path | None = None):
    from climate_pipeline.pipeline.sync import open_database

    return open_database(Settings()), load_pipeline_config(config)


@db_app.command("init")
def db_init(config: ConfigOption = None, log_level: LogOption = "INFO") -> None:
    """Apply pending migrations and load the variable dictionary (idempotent)."""
    _logging(log_level)
    try:
        database, config_model = _database(config)
        try:
            applied = database.migrate()
            pipeline = Pipeline(Settings(), config_model, lake=DataLake(Path("data")))
            count = database.sync_variables(pipeline.variables, pipeline.gold_columns())
            postgis = database.has_postgis()
        finally:
            database.close()
    except (ClimatePipelineError, ConfigurationError) as exc:
        _fail(exc)
        return
    typer.echo(
        f"schema {database.schema}: migrations applied now {applied or 'none'}; "
        f"{count} variables; PostGIS {'on' if postgis else 'off'}"
    )


@db_app.command("status")
def db_status() -> None:
    """Grids, Gold row counts and freshness of each source in the database."""
    try:
        database, _ = _database()
        try:
            tables = database.status()
        finally:
            database.close()
    except ClimatePipelineError as exc:
        _fail(exc)
        return
    for name, frame in tables.items():
        typer.echo(f"== {name}")
        typer.echo(frame.to_string(index=False) if not frame.empty else "(empty)")


@db_app.command("import-lake")
def db_import_lake(
    grid_name: Annotated[list[str] | None, typer.Option(help="Grids to import (default: all).")] = None,
    data_root: DataRootOption = None,
    log_level: LogOption = "INFO",
) -> None:
    """Copy a local lake (Parquet Gold of every grid + manifests) into PostgreSQL."""
    from climate_pipeline.pipeline.sync import import_lake

    _logging(log_level)
    try:
        database, _ = _database()
        try:
            result = import_lake(
                DataLake(_settings(data_root).climate_data_root), database, _split(grid_name)
            )
        finally:
            database.close()
    except ClimatePipelineError as exc:
        _fail(exc)
        return
    typer.echo(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    app()
