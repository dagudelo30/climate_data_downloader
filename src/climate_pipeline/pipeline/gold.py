"""Silver → Gold: resampling onto a target grid, tabular fusion and weekly aggregation."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
import xarray as xr

from climate_pipeline.config import Settings
from climate_pipeline.domain.aoi import AreaOfInterest
from climate_pipeline.domain.dates import EpiWeek, epi_weeks_touched
from climate_pipeline.domain.errors import ConfigurationError, GridMismatchError
from climate_pipeline.domain.grid import GridSpec, Nesting, block_mean, nest
from climate_pipeline.domain.variables import VariableCatalog
from climate_pipeline.processing.aggregation import DAILY_KEYS, aggregate_to_epi_weeks
from climate_pipeline.processing.harmonize import NON_DATA_VARIABLES
from climate_pipeline.processing.quality import (
    DEFAULT_CONSISTENCY_RULES,
    ConsistencyRule,
    check_bounds,
    check_unique_keys,
    enforce_consistency,
)
from climate_pipeline.processing.resample import resample_to_grid
from climate_pipeline.processing.spatial import aoi_mask, standardize_spatial_dims
from climate_pipeline.storage.catalog import Catalog
from climate_pipeline.storage.gold import GoldStore, OutputFormat, open_gold_store
from climate_pipeline.storage.layout import DataLake
from climate_pipeline.storage.netcdf import read_netcdf

logger = logging.getLogger(__name__)

_METERS_PER_DEGREE = 111_320.0


@dataclass
class GoldBuildReport:
    grid_name: str
    grid_fingerprint: str
    grid_shape: tuple[int, int]
    cells_in_aoi: int
    output_format: str
    days_built: list[str] = field(default_factory=list)
    days_without_silver: list[str] = field(default_factory=list)
    weeks_built: list[str] = field(default_factory=list)
    daily_rows: int = 0
    weekly_rows: int = 0
    consistency_violations: dict[str, int] = field(default_factory=dict)
    #: Sources whose Silver was read for each built day (recorded in the catalog).
    sources_by_day: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "grid_name": self.grid_name,
            "grid_fingerprint": self.grid_fingerprint,
            "grid_shape": list(self.grid_shape),
            "cells_in_aoi": self.cells_in_aoi,
            "output_format": self.output_format,
            "days_built": len(self.days_built),
            "first_day": min(self.days_built) if self.days_built else None,
            "last_day": max(self.days_built) if self.days_built else None,
            "days_without_silver": self.days_without_silver,
            "weeks_built": self.weeks_built,
            "daily_rows": self.daily_rows,
            "weekly_rows": self.weekly_rows,
            "sources": sorted({source for sources in self.sources_by_day.values() for source in sources}),
            "consistency_violations": self.consistency_violations,
        }


class GoldBuilder:
    def __init__(
        self,
        lake: DataLake,
        catalog: Catalog,
        variables: VariableCatalog,
        aoi: AreaOfInterest,
        grid_name: str,
        output_format: OutputFormat,
        consistency_rules: Sequence[ConsistencyRule] = DEFAULT_CONSISTENCY_RULES,
        columns: set[str] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.lake = lake
        self.catalog = catalog
        self.variables = variables
        self.aoi = aoi
        self.grid_name = grid_name
        self.output_format = OutputFormat(output_format)
        self.layout = lake.gold_layout(grid_name)
        self.store: GoldStore = open_gold_store(self.layout, self.output_format, settings)
        self.consistency_rules = consistency_rules
        #: Gold columns to build (the selected variables); None = every Silver variable.
        self.columns = columns

    @property
    def build_key(self) -> str:
        """Catalog key: the same grid can be materialised in both output formats."""
        return f"{self.grid_name}__{self.output_format}"

    # --- grid -----------------------------------------------------------------------
    def resolve_grid(self, target: str | None, sample_days: Sequence[date]) -> GridSpec:
        requested = GridSpec.parse(target, self.aoi) if target else None
        return self.resolve_grid_spec(requested, sample_days, label=repr(target))

    def resolve_grid_spec(
        self, requested: GridSpec | None, sample_days: Sequence[date], label: str = "the requested grid"
    ) -> GridSpec:
        if self.layout.grid_path.exists():
            stored = GridSpec.load(self.layout.grid_path)
            if requested is not None and requested.fingerprint() != stored.fingerprint():
                raise GridMismatchError(
                    f"gold/{self.grid_name} was built on grid {stored.fingerprint()} but "
                    f"{label} resolves to {requested.fingerprint()}; use another grid name "
                    "or delete that Gold directory"
                )
            return stored
        grid = requested or self._finest_silver_grid(sample_days)
        grid.save(self.layout.grid_path)
        return grid

    def _finest_silver_grid(self, sample_days: Sequence[date]) -> GridSpec:
        """Without ``--target-grid``: the finest native grid among available Silver files."""
        for day in sample_days:
            candidates = []
            for path in self.lake.silver_files_for(day).values():
                dataset = standardize_spatial_dims(read_netcdf(path))
                grid = GridSpec.from_dataarray(dataset)
                # Compare cell areas in m² (sources mix geographic and projected grids).
                scale = _METERS_PER_DEGREE**2 if grid.is_geographic else 1.0
                candidates.append((grid.resolution[0] * grid.resolution[1] * scale, grid))
            if candidates:
                return min(candidates, key=lambda item: item[0])[1]
        raise ConfigurationError("no Silver data available to derive a default target grid")

    # --- daily ----------------------------------------------------------------------
    def load_silver(self, day: date) -> dict[str, xr.Dataset]:
        """Every Silver dataset of ``day`` (read once, shared by every target grid)."""
        return {
            source: standardize_spatial_dims(read_netcdf(path))
            for source, path in self.lake.silver_files_for(day).items()
        }

    def resample_day(
        self, day: date, grid: GridSpec, silver: dict[str, xr.Dataset] | None = None
    ) -> tuple[dict[str, np.ndarray], set[str]]:
        """Every Silver variable of ``day`` resampled onto the full ``grid`` (2-D arrays), and the
        sources whose Silver was read. ``silver`` reuses datasets already loaded for another grid."""
        silver = self.load_silver(day) if silver is None else silver
        layers: dict[str, np.ndarray] = {}
        for source, dataset in silver.items():
            for name in dataset.data_vars:
                if str(name).lower() in NON_DATA_VARIABLES:
                    continue
                if not self.variables.has(source, str(name)):
                    raise ConfigurationError(
                        f"Silver of {source} on {day} holds {name}, unknown to the variable catalog: "
                        "run with the same --config used for ingestion"
                    )
                spec = self.variables.get(source, str(name))
                if self.columns is not None and spec.gold_column not in self.columns:
                    continue  # stale Silver of a variable that is no longer selected
                resampled = resample_to_grid(dataset[name], grid, spec.resampling)
                layers[spec.gold_column] = resampled.values.astype("float32")
        return layers, set(silver)

    def frame_from_layers(
        self,
        day: date,
        layers: dict[str, np.ndarray],
        cell_ids: np.ndarray,
        lon: np.ndarray,
        lat: np.ndarray,
    ) -> tuple[pd.DataFrame, dict[str, int]]:
        """Daily Gold rows of the AOI cells from full-grid 2-D layers, with QC applied.

        File back-ends rewrite a whole partition, so every selected column is written (NA when
        its source has not published that day yet): every partition has the same schema. The
        Postgres store merges columns instead, so it only receives the columns of this run.
        """
        columns = {name: values.reshape(-1)[cell_ids].astype("float32") for name, values in layers.items()}
        if not columns:
            return pd.DataFrame(), {}
        if self.columns is not None and not self.store.merges_columns:
            for name in self.columns - columns.keys():
                columns[name] = np.full(cell_ids.size, np.nan, dtype="float32")

        frame = pd.DataFrame(
            {
                "cell_id": cell_ids.astype("int64"),
                "date": day,
                "latitude": lat,
                "longitude": lon,
                **{name: columns[name] for name in sorted(columns)},
            }
        )
        # Complete panel: every AOI cell gets a row for every built day, even when no variable has
        # a value there (e.g. a cloud-masked index on a GEE-only run). Missing values are NA.
        frame, consistency = enforce_consistency(frame, self.consistency_rules)
        check_unique_keys(frame, DAILY_KEYS, "gold_daily")
        check_bounds(frame, self.variables, "gold_daily")
        return frame, consistency.violations

    def aoi_cells(self, grid: GridSpec) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """``cell_id``, longitude and latitude of every cell touching the AOI (all_touched)."""
        mask = aoi_mask(grid.template(), self.aoi, all_touched=True).reshape(-1)
        cell_ids = np.flatnonzero(mask)
        lon_grid, lat_grid = grid.lonlat()
        return cell_ids, lon_grid.reshape(-1)[cell_ids], lat_grid.reshape(-1)[cell_ids]

    def new_report(self, grid: GridSpec, cells: int) -> GoldBuildReport:
        return GoldBuildReport(
            grid_name=self.grid_name,
            grid_fingerprint=grid.fingerprint(),
            grid_shape=grid.shape,
            cells_in_aoi=cells,
            output_format=self.output_format.value,
        )

    def write_day(
        self,
        day: date,
        frame: pd.DataFrame,
        violations: dict[str, int],
        report: GoldBuildReport,
        sources: set[str],
    ) -> bool:
        if frame.empty:
            report.days_without_silver.append(day.isoformat())
            return False
        self.store.upsert_daily(day, frame)
        report.days_built.append(day.isoformat())
        report.sources_by_day[day.isoformat()] = sorted(sources)
        report.daily_rows += len(frame)
        for rule, count in violations.items():
            report.consistency_violations[rule] = report.consistency_violations.get(rule, 0) + count
        return True

    def build(self, days: Iterable[date], target: str | None = None) -> GoldBuildReport:
        days = sorted(set(days))
        grid = self.resolve_grid(target, days)
        cell_ids, lon, lat = self.aoi_cells(grid)
        report = self.new_report(grid, int(cell_ids.size))
        for day in days:
            layers, sources = self.resample_day(day, grid)
            frame, violations = self.frame_from_layers(day, layers, cell_ids, lon, lat)
            self.write_day(day, frame, violations, report, sources)
        return self.finish(report)

    def finish(self, report: GoldBuildReport) -> GoldBuildReport:
        """Epidemiological weeks of the built days (plus any week the store reports as stale),
        catalog bookkeeping and log."""
        built = [date.fromisoformat(day) for day in report.days_built]
        for week in sorted({*epi_weeks_touched(built), *self.store.stale_weeks()}):
            weekly_rows = self.build_week(week)
            if weekly_rows:
                report.weeks_built.append(week.label)
                report.weekly_rows += weekly_rows

        self.catalog.mark_gold_built(
            self.build_key,
            {date.fromisoformat(day): sources for day, sources in report.sources_by_day.items()},
        )
        logger.info(
            "gold/%s: %d days, %d epi weeks, %d daily rows",
            self.grid_name,
            len(built),
            len(report.weeks_built),
            report.daily_rows,
        )
        return report

    # --- weekly ---------------------------------------------------------------------
    def build_week(self, week: EpiWeek) -> int:
        """Recompute one epi week from every daily row available for its 7 days."""
        daily = self.store.read_daily(week.days)
        if daily.empty:
            return 0
        daily["date"] = pd.to_datetime(daily["date"]).dt.date
        weekly = aggregate_to_epi_weeks(daily, self.variables)
        check_bounds(weekly, self.variables, "gold_epiweekly")
        self.store.upsert_weekly(week, weekly)
        return len(weekly)

    def close(self) -> None:
        self.store.close()

    def refresh_weeks(self, weeks: Iterable[EpiWeek]) -> list[str]:
        """Rebuild the given epi weeks from the daily rows in the store (e.g. ``stale_weeks()``)."""
        return [week.label for week in sorted(set(weeks)) if self.build_week(week)]


@dataclass(frozen=True)
class NestedGrid:
    name: str
    grid: GridSpec
    nesting: Nesting | None  # None for the finest (template) grid


def plan_nested_grids(targets: Sequence[tuple[str, str]], aoi: AreaOfInterest) -> list[NestedGrid]:
    """Order ``(name, '<CRS>@<resolution>')`` targets from finest to coarsest and nest them.

    The finest grid is the **template**: it is extended to the extent of the coarsest grid,
    so every coarse cell is an exact block of fine cells (``nest`` validates it). Grids are
    snapped to multiples of their resolution, so 100 m, 1 km and 5 km grids in the same
    projected CRS always nest.
    """
    if len(targets) < 2:
        raise ConfigurationError("nested Gold needs at least two target grids")
    parsed = []
    for name, spec in targets:
        if "@" not in spec:
            raise ConfigurationError(f"nested grids must be '<CRS>@<resolution>', got {spec!r} for {name}")
        parsed.append((name, GridSpec.parse(spec, aoi)))
    parsed.sort(key=lambda item: item[1].resolution[0])
    fine_name, fine = parsed[0]
    coarsest = parsed[-1][1]
    template = GridSpec.from_bounds(fine.crs, fine.resolution[0], coarsest.bounds)
    grids = [NestedGrid(fine_name, template, None)]
    grids.extend(NestedGrid(name, grid, nest(template, grid)) for name, grid in parsed[1:])
    return grids


class NestedGoldBuilder:
    """Gold on a hierarchy of nested grids, all derived from the finest one.

    Silver is resampled **once**, onto the finest grid (the template); each coarser grid is
    the block mean of the template cells it contains. All grids therefore share the same
    fine-scale information and are mutually consistent (the mean of the 100 m cells of a
    1 km cell *is* the 1 km value), while every grid keeps its own Gold store, AOI cells,
    QC and epidemiological weeks.
    """

    def __init__(
        self,
        lake: DataLake,
        catalog: Catalog,
        variables: VariableCatalog,
        aoi: AreaOfInterest,
        grids: Sequence[NestedGrid],
        output_format: OutputFormat,
        columns: set[str] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.grids = list(grids)
        self.builders = [
            GoldBuilder(
                lake,
                catalog,
                variables,
                aoi,
                grid.name,
                output_format,
                columns=columns,
                settings=settings,
            )
            for grid in self.grids
        ]
        self.catalog = catalog

    def pending_days(self) -> list[date]:
        days: set[date] = set()
        for builder in self.builders:
            days.update(self.catalog.pending_gold_dates(builder.build_key))
        return sorted(days)

    def build(self, days: Iterable[date]) -> list[GoldBuildReport]:
        days = sorted(set(days))
        resolved = []
        for nested, builder in zip(self.grids, self.builders, strict=True):
            grid = builder.resolve_grid_spec(nested.grid, days, label=f"nested grid {nested.name}")
            cell_ids, lon, lat = builder.aoi_cells(grid)
            resolved.append(
                (nested, builder, grid, cell_ids, lon, lat, builder.new_report(grid, int(cell_ids.size)))
            )

        template_builder, template_grid = resolved[0][1], resolved[0][2]
        for day in days:
            fine_layers, sources = template_builder.resample_day(day, template_grid)
            for nested, builder, grid, cell_ids, lon, lat, report in resolved:
                if nested.nesting is None:
                    layers = fine_layers
                else:
                    layers = {
                        name: block_mean(values, nested.nesting, grid.shape)
                        for name, values in fine_layers.items()
                    }
                frame, violations = builder.frame_from_layers(day, layers, cell_ids, lon, lat)
                builder.write_day(day, frame, violations, report, sources)
        return [builder.finish(report) for _, builder, *_, report in resolved]

    def close(self) -> None:
        for builder in self.builders:
            builder.close()
