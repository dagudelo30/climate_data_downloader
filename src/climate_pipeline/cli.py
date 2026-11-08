"""Command line interface: ``uv run climate-pipeline --help``."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import typer

from climate_pipeline.config import Settings, load_pipeline_config
from climate_pipeline.domain.aoi import AreaOfInterest
from climate_pipeline.domain.dates import EpiWeek
from climate_pipeline.domain.errors import ClimatePipelineError
from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.pipeline.orchestrator import NamedGrid, Pipeline, RunRequest
from climate_pipeline.pipeline.static import build_static_covariates
from climate_pipeline.storage.gold import OutputFormat, open_gold_store
from climate_pipeline.storage.layout import DataLake
from climate_pipeline.storage.raster import make_raster

app = typer.Typer(
    name="climate-pipeline",
    help="Medallion pipeline (Bronze → Silver → Gold) for daily climate and satellite sources.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

DateOption = Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], show_default=False)]
AoiOption = Annotated[
    Path, typer.Option("--aoi-path", exists=True, dir_okay=False, help="Shapefile/GeoJSON/GPKG of the AOI.")
]
SourcesOption = Annotated[
    list[str],
    typer.Option(
        "--sources",
        help="Sources (repeat or comma-separate): chirps, mswep, agera5, gee, era5land, imerg, gsmap, "
        "modis, viirs, smap, landsat, goes, sentinel1.",
    ),
]
GridOption = Annotated[
    list[str] | None,
    typer.Option(
        "--target-grid",
        help="Target grid, repeatable. 'name=EPSG:9377@1000', 'EPSG:4326@0.05' or a template raster. "
        "Each grid gets its own Gold store under gold/<name>/. Omit for the finest native grid.",
    ),
]
FormatOption = Annotated[OutputFormat, typer.Option("--output-format", help="Gold storage back-end.")]
NestedOption = Annotated[
    bool,
    typer.Option(
        "--nested/--independent",
        help="--nested: resample Silver once onto the finest --target-grid (the template) and derive "
        "every coarser grid as exact block means of it (grids must be '<CRS>@<res>' with resolutions "
        "multiple of the finest). --independent: resample Silver onto each grid separately.",
    ),
]
ConfigOption = Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)]
DataRootOption = Annotated[Path | None, typer.Option("--data-root", help="Overrides CLIMATE_DATA_ROOT.")]
LogOption = Annotated[str, typer.Option("--log-level")]


def _setup(log_level: str, data_root: Path | None, config_path: Path | None) -> Pipeline:
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    for noisy in ("httpx", "httpcore", "rasterio", "urllib3", "googleapiclient", "cdsapi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = Settings()
    if data_root is not None:
        settings = settings.model_copy(update={"climate_data_root": data_root})
    return Pipeline(settings, load_pipeline_config(config_path))


def _split(values: list[str]) -> list[str]:
    return [item.strip().lower() for value in values for item in value.split(",") if item.strip()]


def _to_date(value: datetime | None) -> date | None:
    return value.date() if value else None


def _request(
    aoi_path: Path,
    sources: list[str],
    start_date: datetime | None,
    end_date: datetime | None,
    target_grid: list[str] | None,
    output_format: OutputFormat,
    incremental: bool,
    force: bool,
    build_gold: bool,
    today: datetime | None,
    nested: bool = False,
) -> RunRequest:
    grids = [NamedGrid.parse(value) for value in target_grid] if target_grid else [NamedGrid.parse(None)]
    names = [grid.name for grid in grids]
    if len(set(names)) != len(names):
        raise typer.BadParameter(f"duplicated grid names: {names}", param_hint="--target-grid")
    return RunRequest(
        aoi_path=aoi_path,
        sources=_split(sources),
        start_date=_to_date(start_date),
        end_date=_to_date(end_date),
        target_grids=grids,
        output_format=output_format,
        incremental=incremental,
        force=force,
        build_gold=build_gold,
        today=_to_date(today),
        nested=nested,
    )


def _fail(exc: ClimatePipelineError) -> None:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2) from exc


@app.command()
def run(
    aoi_path: AoiOption,
    sources: SourcesOption = ["chirps", "agera5"],  # noqa: B006
    start_date: DateOption = None,
    end_date: DateOption = None,
    target_grid: GridOption = None,
    output_format: FormatOption = OutputFormat.PARQUET,
    nested: NestedOption = False,
    incremental: Annotated[bool, typer.Option(help="Latency-aware window instead of fixed dates.")] = False,
    force: Annotated[bool, typer.Option(help="Re-download even if Bronze is final.")] = False,
    skip_gold: Annotated[bool, typer.Option(help="Stop after Silver (CI ingest jobs).")] = False,
    report_path: Annotated[Path | None, typer.Option(help="Write the JSON run report here.")] = None,
    today: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], hidden=True)] = None,
    config: ConfigOption = None,
    data_root: DataRootOption = None,
    log_level: LogOption = "INFO",
) -> None:
    """Run Bronze → Silver → Gold for the requested sources and dates."""
    pipeline = _setup(log_level, data_root, config)
    try:
        request = _request(
            aoi_path,
            sources,
            start_date,
            end_date,
            target_grid,
            output_format,
            incremental,
            force,
            not skip_gold,
            today,
            nested,
        )
        report = pipeline.run(request)
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
    aoi_path: AoiOption,
    sources: SourcesOption = ["chirps", "agera5"],  # noqa: B006
    start_date: DateOption = None,
    end_date: DateOption = None,
    incremental: bool = True,
    force: bool = False,
    today: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], hidden=True)] = None,
    github_matrix: Annotated[
        bool, typer.Option(help="Print a GitHub Actions matrix of sources with work.")
    ] = False,
    config: ConfigOption = None,
    data_root: DataRootOption = None,
    log_level: LogOption = "WARNING",
) -> None:
    """Show which (variable, date) pairs each source would fetch, given its latency."""
    pipeline = _setup(log_level, data_root, config)
    try:
        request = _request(
            aoi_path,
            sources,
            start_date,
            end_date,
            None,
            OutputFormat.PARQUET,
            incremental,
            force,
            False,
            today,
        )
        plans = pipeline.plan(request)
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
    aoi_path: AoiOption,
    target_grid: GridOption = None,
    output_format: FormatOption = OutputFormat.PARQUET,
    nested: NestedOption = False,
    start_date: DateOption = None,
    end_date: DateOption = None,
    report_path: Annotated[Path | None, typer.Option()] = None,
    config: ConfigOption = None,
    data_root: DataRootOption = None,
    log_level: LogOption = "INFO",
) -> None:
    """Rebuild Gold for every date whose Silver changed since the last Gold build."""
    pipeline = _setup(log_level, data_root, config)
    try:
        request = _request(
            aoi_path,
            [],
            start_date,
            end_date,
            target_grid,
            output_format,
            start_date is None,
            False,
            True,
            None,
            nested,
        )
        aoi = AreaOfInterest.from_file(aoi_path)
        reports, errors = pipeline.build_gold(request, aoi)
    except ClimatePipelineError as exc:
        _fail(exc)
        return
    payload = {"gold": [item.to_dict() for item in reports], "gold_errors": errors}
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, indent=2, default=str))
    typer.echo(json.dumps(payload, indent=2, default=str))
    if errors:
        raise typer.Exit(code=1)


@app.command("build-static")
def build_static(
    aoi_path: AoiOption,
    grid_name: Annotated[
        list[str],
        typer.Option(
            help="Gold store (grid) name, repeatable. Each grid is computed from the native layers "
            "(--nested: on the finest grid, block-averaged onto the others)."
        ),
    ],
    layers: Annotated[
        list[str], typer.Option("--layers", help="terrain (Copernicus DEM), urban (Earth Engine), osm.")
    ] = ["terrain"],  # noqa: B006
    year: Annotated[
        int | None, typer.Option(help="Year of the urban composites (default: last year).")
    ] = None,
    output_format: FormatOption = OutputFormat.PARQUET,
    nested: NestedOption = False,
    data_root: DataRootOption = None,
    log_level: LogOption = "INFO",
) -> None:
    """Static covariates (terrain, urban form, OSM roads) on one or several Gold grids."""
    pipeline = _setup(log_level, data_root, None)
    try:
        frames = build_static_covariates(
            pipeline.lake,
            _split(grid_name),
            AreaOfInterest.from_file(aoi_path),
            output_format,
            layers=_split(layers),
            year=year,
            settings=pipeline.settings,
            nested=nested,
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
    variable: Annotated[str, typer.Option(help="Gold column, e.g. chirps_precip.")],
    output: Annotated[Path, typer.Option(help="Output .tif path.")],
    day: DateOption = None,
    epi_week: Annotated[str | None, typer.Option(help="Epidemiological week as YYYY-Www.")] = None,
    template: Annotated[str | None, typer.Option(help="Optional template grid to resample to.")] = None,
    aoi_path: Annotated[
        Path | None, typer.Option(exists=True, help="Needed only with '@' templates.")
    ] = None,
    output_format: FormatOption = OutputFormat.PARQUET,
    cog: Annotated[bool, typer.Option(help="Write a Cloud-Optimized GeoTIFF.")] = True,
    data_root: DataRootOption = None,
    log_level: LogOption = "WARNING",
) -> None:
    """Rebuild a GeoTIFF/COG from a Gold table (daily, epi-weekly or static)."""
    pipeline = _setup(log_level, data_root, None)
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
        store = open_gold_store(pipeline.lake.gold_layout(grid_name), output_format)
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
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(str(path))


ALL_SOURCES = "chirps,agera5,mswep,gee,era5land,imerg,gsmap,modis,viirs,smap,landsat,goes,sentinel1"


@app.command()
def availability(
    aoi_path: AoiOption,
    sources: SourcesOption = [ALL_SOURCES],  # noqa: B006
    output: Annotated[Path | None, typer.Option(help="Write the JSON report here.")] = None,
    today: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
    config: ConfigOption = None,
    log_level: LogOption = "WARNING",
) -> None:
    """Latest complete day available per source today (live probe), its lag and the last
    complete epidemiological week: the operational horizon of the model."""
    from climate_pipeline.pipeline.availability import availability_report

    pipeline = _setup(log_level, None, config)
    try:
        report = availability_report(
            pipeline.settings,
            pipeline.config,
            pipeline.variables,
            AreaOfInterest.from_file(aoi_path),
            _split(sources),
            today=_to_date(today),
        )
    except ClimatePipelineError as exc:
        _fail(exc)
        return
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, default=str))
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
def variables(
    config: ConfigOption = None,
) -> None:
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
def catalog(data_root: DataRootOption = None) -> None:
    """Summarise the ingestion catalog by source, layer and status."""
    pipeline = _setup("WARNING", data_root, None)
    typer.echo(pipeline.catalog.summary().to_string(index=False))


if __name__ == "__main__":
    app()
