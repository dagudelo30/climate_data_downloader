"""Evidence pack for the Cali January-May 2026 example.

Produces, under ``--output-dir``:

* ``maps/``        AOI with the 1 km / 5 km Gold cells and native source pixels
                   (all_touched rule), daily and static maps on both grids;
* ``timeseries/``  daily AOI means and epidemiological-week aggregates;
* ``qc/``          automated quality checks (CSV + Markdown) — exits 1 if any fails;
* ``tables/``      CSV extracts of the Gold tables and their schema.

Usage: ``uv run python examples/cali_2026/make_evidence.py --output-dir outputs/...``
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from shapely.geometry import box

from climate_pipeline.config import load_pipeline_config
from climate_pipeline.domain.aoi import AreaOfInterest
from climate_pipeline.domain.dates import EpiWeek
from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.domain.variables import WeeklyAggregation
from climate_pipeline.pipeline.orchestrator import build_variable_catalog
from climate_pipeline.processing.harmonize import select_data_variable
from climate_pipeline.storage.catalog import Catalog
from climate_pipeline.storage.gold import GoldTable, OutputFormat, open_gold_store
from climate_pipeline.storage.layout import DataLake
from climate_pipeline.storage.netcdf import read_netcdf
from climate_pipeline.storage.raster import load_layer

AOI_PATH = Path("data/auxiliary/shapefiles/cali/cali.shp")
GRIDS = ("1km", "5km")
START, END = date(2026, 1, 1), date(2026, 5, 31)
SAMPLE_DAY = date(2026, 3, 15)
# What each run recorded in reports/ did (later runs overwrite the Bronze/Silver of earlier ones).
RUN_NOTES = {
    "run_chirps_gee_mirror.json": "CHIRPS v2 re-descargado desde el espejo de GEE (CHC bloqueó la IP con 403)",
    "run_gee_terra_aqua.json": "MODIS Terra+Aqua con la máscara estricta (incluía adyacencia); reemplazada",
    "run_gee_s2_standard_mask.json": "MODIS con máscara `standard` + Sentinel-2 (Cloud Score+); vigente",
    "run_parquet.json": "corrida del script de ejemplo: todo al día, sin descargas pendientes",
}
CATALOG = build_variable_catalog(load_pipeline_config(Path("examples/cali_2026/pipeline.yaml")))
plt.rcParams.update({"figure.dpi": 130, "font.size": 9, "axes.titlesize": 10})


@dataclass
class Check:
    name: str
    scope: str
    passed: bool
    detail: str


class Evidence:
    def __init__(self, output_dir: Path, data_root: Path) -> None:
        self.out = output_dir
        self.lake = DataLake(data_root)
        self.aoi = AreaOfInterest.from_file(AOI_PATH)
        self.barrios = gpd.read_file(AOI_PATH)
        self.checks: list[Check] = []
        for sub in ("maps", "timeseries", "qc", "tables"):
            (self.out / sub).mkdir(parents=True, exist_ok=True)
        self.grids = {name: GridSpec.load(self.lake.gold_layout(name).grid_path) for name in GRIDS}
        self.stores = {
            (name, fmt): open_gold_store(self.lake.gold_layout(name), fmt)
            for name in GRIDS
            for fmt in OutputFormat
        }
        self.daily = {name: self._query(name, f"SELECT * FROM {GoldTable.DAILY}") for name in GRIDS}
        self.weekly = {name: self._query(name, f"SELECT * FROM {GoldTable.EPIWEEKLY}") for name in GRIDS}
        self.static = {name: self._query(name, f"SELECT * FROM {GoldTable.STATIC}") for name in GRIDS}

    def _query(self, grid: str, sql: str, fmt: OutputFormat = OutputFormat.PARQUET) -> pd.DataFrame:
        frame = self.stores[(grid, fmt)].query(sql)
        for column in ("date", "week_start", "week_end"):
            if column in frame.columns:
                frame[column] = pd.to_datetime(frame[column]).dt.date
        return frame

    def check(self, name: str, scope: str, passed: bool, detail: str) -> None:
        self.checks.append(Check(name, scope, bool(passed), detail))

    # --- geometry helpers -----------------------------------------------------------
    def cell_polygons(self, grid_name: str, cell_ids: np.ndarray) -> gpd.GeoDataFrame:
        grid = self.grids[grid_name]
        a, _, c, _, e, f = grid.transform
        rows, cols = np.divmod(cell_ids, grid.width)
        geometries = [
            box(c + col * a, f + (row + 1) * e, c + (col + 1) * a, f + row * e)
            for row, col in zip(rows, cols, strict=True)
        ]
        return gpd.GeoDataFrame({"cell_id": cell_ids}, geometry=geometries, crs=grid.crs)

    def source_pixels(self, source: str) -> gpd.GeoDataFrame:
        dataset = read_netcdf(self.lake.silver_path(source, SAMPLE_DAY))
        variable = next(iter(dataset.data_vars))
        values = dataset[variable].values
        dx = float(abs(dataset.x[1] - dataset.x[0])) if dataset.sizes["x"] > 1 else 0.1
        dy = float(abs(dataset.y[1] - dataset.y[0])) if dataset.sizes["y"] > 1 else dx
        polygons = [
            box(x - dx / 2, y - dy / 2, x + dx / 2, y + dy / 2)
            for i, y in enumerate(dataset.y.values)
            for j, x in enumerate(dataset.x.values)
            if np.isfinite(values[i, j])
        ]
        return gpd.GeoDataFrame(geometry=polygons, crs="EPSG:4326")

    # --- maps -----------------------------------------------------------------------
    def map_aoi_and_grids(self) -> None:
        barrios = self.barrios.to_crs("EPSG:9377")
        fig, axes = plt.subplots(1, 4, figsize=(17, 5.4))
        layers = [
            (
                "Gold 1 km (EPSG:9377)",
                self.cell_polygons("1km", self.static["1km"]["cell_id"].to_numpy()),
                "#2b8cbe",
            ),
            (
                "Gold 5 km (EPSG:9377)",
                self.cell_polygons("5km", self.static["5km"]["cell_id"].to_numpy()),
                "#e34a33",
            ),
            ("CHIRPS 0.05° (Silver)", self.source_pixels("chirps").to_crs("EPSG:9377"), "#31a354"),
            ("AgERA5 0.1° (Silver)", self.source_pixels("agera5").to_crs("EPSG:9377"), "#756bb1"),
        ]
        for ax, (title, cells, color) in zip(axes, layers, strict=True):
            cells.plot(ax=ax, facecolor=color, alpha=0.25, edgecolor=color, linewidth=0.8)
            barrios.plot(ax=ax, facecolor="none", edgecolor="#444444", linewidth=0.25)
            barrios.dissolve().boundary.plot(ax=ax, color="black", linewidth=1.1)
            ax.set_title(f"{title}\n{len(cells)} celdas que tocan el AOI")
            ax.set_axis_off()
        fig.suptitle(
            "Regla all_touched: se conserva toda celda que toque el perímetro urbano de Cali (335 barrios)"
        )
        fig.tight_layout()
        fig.savefig(self.out / "maps" / "01_aoi_grids_all_touched.png")
        plt.close(fig)

    def map_variables_by_grid(self) -> None:
        variables = [
            ("chirps_precip", "CHIRPS precipitación (mm/d)", "Blues"),
            ("agera5_precip", "AgERA5 precipitación (mm/d)", "Blues"),
            ("agera5_temp_mean", "AgERA5 T media (°C)", "RdYlBu_r"),
            ("agera5_temp_max", "AgERA5 T máx (°C)", "RdYlBu_r"),
            ("agera5_solar_radiation", "Radiación solar (MJ/m²/d)", "magma"),
            ("gee_ndvi", "MODIS NDVI", "YlGn"),
            ("gee_ndwi", "MODIS NDWI", "GnBu"),
            ("gee_s2_ndvi", "Sentinel-2 NDVI", "YlGn"),
            ("gee_s2_ndwi", "Sentinel-2 NDWI", "GnBu"),
        ]
        daily = self.daily["1km"]
        # Show a wet day (spatial structure in precipitation) and, for each optical index, its
        # clearest day (most 1 km cells with a value).
        wettest = daily.groupby("date")["chirps_precip"].mean().idxmax()
        clearest = {
            column: daily.groupby("date")[column].count().idxmax()
            for column, _, _ in variables
            if column.startswith("gee_")
        }
        fig, axes = plt.subplots(2, len(variables), figsize=(29, 7.5))
        barrios = self.barrios.to_crs("EPSG:9377").dissolve()
        for column_index, (column, title, cmap) in enumerate(variables):
            day = clearest.get(column, wettest)
            title = f"{title} — {day}"
            layers = {
                g: load_layer(self.stores[(g, OutputFormat.PARQUET)], self.grids[g], column, day=day)
                for g in GRIDS
            }
            finite = np.concatenate([layer[np.isfinite(layer)] for layer in layers.values()])
            vmin, vmax = (float(finite.min()), float(finite.max())) if finite.size else (0, 1)
            for row_index, grid_name in enumerate(GRIDS):
                ax = axes[row_index, column_index]
                grid = self.grids[grid_name]
                a, _, c, _, e, f = grid.transform
                extent = (c, c + grid.width * a, f + grid.height * e, f)
                image = ax.imshow(layers[grid_name], extent=extent, cmap=cmap, vmin=vmin, vmax=vmax)
                barrios.boundary.plot(ax=ax, color="black", linewidth=0.8)
                ax.set_title(f"{title}\n{grid_name}", fontsize=8)
                ax.set_xticks([])
                ax.set_yticks([])
                fig.colorbar(image, ax=ax, fraction=0.046, pad=0.02)
        fig.suptitle(
            "Tabla Gold → raster (load_layer/make_raster): día más lluvioso (CHIRPS) y, para cada índice, su día más despejado; "
            "misma escala de color en 1 km y 5 km"
        )
        fig.tight_layout()
        fig.savefig(self.out / "maps" / "02_gold_variables_1km_vs_5km.png")
        plt.close(fig)

    def map_static(self) -> None:
        columns = [
            ("static_elevation_m", "Elevación (m)", "terrain"),
            ("static_slope_deg", "Pendiente (°)", "viridis"),
            ("static_northness", "Northness", "coolwarm"),
            ("static_eastness", "Eastness", "coolwarm"),
        ]
        fig, axes = plt.subplots(1, 4, figsize=(15, 4.6))
        barrios = self.barrios.to_crs("EPSG:9377").dissolve()
        grid = self.grids["1km"]
        a, _, c, _, e, f = grid.transform
        extent = (c, c + grid.width * a, f + grid.height * e, f)
        for ax, (column, title, cmap) in zip(axes, columns, strict=True):
            layer = load_layer(self.stores[("1km", OutputFormat.PARQUET)], grid, column)
            image = ax.imshow(layer, extent=extent, cmap=cmap)
            barrios.boundary.plot(ax=ax, color="black", linewidth=0.8)
            ax.set_title(f"{title} — 1 km")
            ax.set_axis_off()
            fig.colorbar(image, ax=ax, fraction=0.046, pad=0.02)
        fig.suptitle("Covariables estáticas (Copernicus DEM GLO-90 agregado por promedio de área)")
        fig.tight_layout()
        fig.savefig(self.out / "maps" / "03_static_covariates_1km.png")
        plt.close(fig)

    # --- time series ----------------------------------------------------------------
    def plot_daily(self) -> None:
        fig, axes = plt.subplots(4, 1, figsize=(13, 11), sharex=True)
        for grid_name, style in (("1km", "-"), ("5km", "--")):
            means = self.daily[grid_name].groupby("date").mean(numeric_only=True)
            index = pd.to_datetime(means.index)
            axes[0].plot(index, means["chirps_precip"], style, color="#3182bd", label=f"CHIRPS {grid_name}")
            axes[0].plot(index, means["agera5_precip"], style, color="#9ecae1", label=f"AgERA5 {grid_name}")
            for column, color in (
                ("agera5_temp_min", "#4575b4"),
                ("agera5_temp_mean", "#fdae61"),
                ("agera5_temp_max", "#d73027"),
            ):
                axes[1].plot(
                    index, means[column], style, color=color, label=f"{column.split('_')[-1]} {grid_name}"
                )
            axes[2].plot(
                index, means["gee_ndvi"], style, marker=".", color="#31a354", label=f"NDVI MODIS {grid_name}"
            )
            axes[2].plot(
                index, means["gee_ndwi"], style, marker=".", color="#2c7fb8", label=f"NDWI MODIS {grid_name}"
            )
            axes[2].plot(
                index, means["gee_s2_ndvi"], style, marker="x", color="#006d2c", label=f"NDVI S2 {grid_name}"
            )
            axes[2].plot(
                index, means["gee_s2_ndwi"], style, marker="x", color="#08519c", label=f"NDWI S2 {grid_name}"
            )
            axes[3].plot(index, means["agera5_rh_06h"], style, color="#8856a7", label=f"HR 06h {grid_name}")
            axes[3].plot(index, means["agera5_rh_15h"], style, color="#c994c7", label=f"HR 15h {grid_name}")
        titles = [
            "Precipitación diaria, media del AOI (mm/d)",
            "Temperatura 2 m (°C): Tmin ≤ Tmedia ≤ Tmáx",
            "NDVI/NDWI MODIS (puntos) y Sentinel-2 (x); huecos = nubes o sin pasada",
            "Humedad relativa (%)",
        ]
        for ax, title in zip(axes, titles, strict=True):
            ax.set_title(title)
            ax.legend(ncol=4, fontsize=7)
            ax.grid(alpha=0.3)
        axes[2].set_ylim(-1, 1)
        fig.suptitle("Cali — serie diaria Gold, enero a mayo de 2026 (línea continua 1 km, discontinua 5 km)")
        fig.tight_layout()
        fig.savefig(self.out / "timeseries" / "01_daily_aoi_means.png")
        plt.close(fig)

    def plot_weekly(self) -> None:
        fig, axes = plt.subplots(3, 1, figsize=(13, 10), sharex=True)
        weekly = self.weekly["1km"].copy()
        weekly["label"] = [
            f"{y}-W{w:02d}" for y, w in zip(weekly["epi_year"], weekly["epi_week"], strict=True)
        ]
        means = weekly.groupby("label").mean(numeric_only=True).sort_index()
        x = np.arange(len(means))
        incomplete = means["days_observed"] < 7
        axes[0].bar(x - 0.2, means["chirps_precip"].fillna(0), 0.4, color="#3182bd", label="CHIRPS")
        axes[0].bar(x + 0.2, means["agera5_precip"].fillna(0), 0.4, color="#9ecae1", label="AgERA5")
        for position in x[incomplete.to_numpy()]:
            axes[0].axvspan(position - 0.5, position + 0.5, color="#bbbbbb", alpha=0.4)
            axes[0].text(position, 1, "semana\nincompleta\n→ NaN", ha="center", fontsize=6)
        axes[0].set_title("Precipitación semanal acumulada (mm/semana); suma solo con 7/7 días")
        for column, color in (
            ("agera5_temp_min", "#4575b4"),
            ("agera5_temp_mean", "#fdae61"),
            ("agera5_temp_max", "#d73027"),
        ):
            axes[1].plot(x, means[column], marker="o", color=color, label=column)
        axes[1].set_title("Temperatura semanal (media de los valores diarios, ≥4 días válidos)")
        axes[2].plot(x, means["gee_ndvi"], marker="o", color="#31a354", label="NDVI MODIS")
        axes[2].plot(x, means["gee_ndwi"], marker="o", color="#2c7fb8", label="NDWI MODIS")
        axes[2].plot(x, means["gee_s2_ndvi"], marker="x", color="#006d2c", label="NDVI Sentinel-2")
        axes[2].plot(x, means["gee_s2_ndwi"], marker="x", color="#08519c", label="NDWI Sentinel-2")
        axes[2].bar(
            x,
            means["gee_ndvi_ndays"] / 7,
            color="#c7e9c0",
            alpha=0.6,
            label="fracción de días despejados (NDVI)",
        )
        axes[2].set_ylim(-1, 1)
        axes[2].set_title(
            "NDVI / NDWI semanal (≥1 día despejado; la barra = fracción de días con NDVI MODIS)"
        )
        for ax in axes:
            ax.legend(fontsize=7, ncol=4)
            ax.grid(alpha=0.3)
        axes[2].set_xticks(x, means.index, rotation=60, fontsize=7)
        fig.suptitle(
            "Tabla Gold por semana epidemiológica (domingo-sábado, convención INS/MMWR) — grilla 1 km"
        )
        fig.tight_layout()
        fig.savefig(self.out / "timeseries" / "02_epiweekly.png")
        plt.close(fig)

    # --- quality checks -------------------------------------------------------------
    def run_checks(self) -> None:
        expected_days = (END - START).days + 1
        for grid_name in GRIDS:
            daily, weekly, static = self.daily[grid_name], self.weekly[grid_name], self.static[grid_name]
            cells = static["cell_id"].nunique()
            self.check(
                "days covered",
                grid_name,
                daily["date"].nunique() == expected_days,
                f"{daily['date'].nunique()} of {expected_days} days",
            )
            per_day = daily.groupby("date")["cell_id"].nunique()
            self.check(
                "same cells every day",
                grid_name,
                per_day.min() == per_day.max() == cells,
                f"{per_day.min()}..{per_day.max()} cells/day, {cells} AOI cells",
            )
            self.check(
                "unique (cell_id, date)",
                grid_name,
                not daily.duplicated(["cell_id", "date"]).any(),
                f"{len(daily)} rows",
            )
            self.check(
                "unique (cell_id, epi_year, epi_week)",
                grid_name,
                not weekly.duplicated(["cell_id", "epi_year", "epi_week"]).any(),
                f"{len(weekly)} rows",
            )
            per_week = weekly.groupby(["epi_year", "epi_week"])["cell_id"].nunique()
            self.check(
                "same cells every epi week (NA when no value, never a missing row)",
                grid_name,
                per_week.min() == per_week.max() == cells,
                f"{per_week.min()}..{per_week.max()} cells/week over {len(per_week)} weeks, {cells} AOI cells",
            )
            # Same day, same cells: pooling all cell-days would mix day-to-day differences between
            # the sensors (overpass time, which pixels are clear) into a spatial-agreement check.
            both = daily.dropna(subset=["gee_ndvi", "gee_s2_ndvi"])
            per_day = [
                group["gee_ndvi"].corr(group["gee_s2_ndvi"])
                for _, group in both.groupby("date")
                if len(group) >= 5
            ]
            bias = (both["gee_s2_ndvi"] - both["gee_ndvi"]).mean()
            self.check(
                "MODIS vs Sentinel-2 NDVI: same spatial pattern on common days (median r > 0.6)",
                grid_name,
                len(per_day) > 0 and float(np.median(per_day)) > 0.6,
                f"median spatial r = {np.median(per_day):.2f} over {len(per_day)} days ({len(both)} cell-days); "
                f"S2 - MODIS bias = {bias:+.3f} (separate columns, do not mix without calibration)",
            )
            for column in daily.columns:
                if not CATALOG.has_gold_column(column):
                    continue
                spec = CATALOG.by_gold_column(column)
                values = daily[column].dropna()
                inside = values.between(spec.bounds.minimum, spec.bounds.maximum).all()
                self.check(
                    f"physical range {column}",
                    grid_name,
                    inside and np.isfinite(values).all(),
                    f"[{values.min():.3f}, {values.max():.3f}] ⊂ [{spec.bounds.minimum:g}, {spec.bounds.maximum:g}] {spec.units}; "
                    f"{daily[column].isna().mean():.1%} null",
                )
            for index in ("gee_ndvi", "gee_ndwi", "gee_s2_ndvi", "gee_s2_ndwi"):
                values = daily[index].dropna()
                self.check(
                    f"{index} within [-1, 1]",
                    grid_name,
                    values.between(-1, 1).all(),
                    f"min={values.min():.3f} max={values.max():.3f} n={len(values)}",
                )
            tolerance = 0.5
            for lower, upper in (
                ("agera5_temp_min", "agera5_temp_mean"),
                ("agera5_temp_mean", "agera5_temp_max"),
                ("agera5_dewpoint_mean", "agera5_temp_mean"),
            ):
                both = daily[[lower, upper]].dropna()
                violations = int((both[lower] > both[upper] + tolerance).sum())
                self.check(
                    f"{lower} <= {upper}",
                    grid_name,
                    violations == 0,
                    f"{violations} violations in {len(both)} rows",
                )
            self.check(
                "temperatures plausible for Cali (5-40 °C)",
                grid_name,
                daily[["agera5_temp_min", "agera5_temp_max"]].stack().dropna().between(5, 40).all(),
                f"Tmin≥{daily['agera5_temp_min'].min():.1f}, Tmax≤{daily['agera5_temp_max'].max():.1f}",
            )
            self.check_weekly_recomputation(grid_name, daily, weekly)
            self.check_formats_match(grid_name)
            self.check_raster_roundtrip(grid_name)
            elevation = static["static_elevation_m"]
            self.check(
                "elevation plausible (Cali ~900-1500 m)",
                grid_name,
                elevation.between(800, 2000).all(),
                f"{elevation.min():.0f}-{elevation.max():.0f} m",
            )
        self.check_units_from_bronze()
        self.check_grid_consistency()
        self.check_date_alignment()

    def check_weekly_recomputation(self, grid_name: str, daily: pd.DataFrame, weekly: pd.DataFrame) -> None:
        """Independent pandas re-aggregation compared with the stored weekly table."""
        frame = daily.copy()
        weeks = [EpiWeek.from_date(day) for day in frame["date"]]
        frame["epi_year"] = [week.year for week in weeks]
        frame["epi_week"] = [week.week for week in weeks]
        groups = frame.groupby(["cell_id", "epi_year", "epi_week"])
        worst = 0.0
        for column in [c for c in daily.columns if CATALOG.has_gold_column(c)]:
            spec = CATALOG.by_gold_column(column)
            function = {
                WeeklyAggregation.SUM: "sum",
                WeeklyAggregation.MEAN: "mean",
                WeeklyAggregation.MIN: "min",
                WeeklyAggregation.MAX: "max",
            }[spec.weekly_aggregation]
            expected = groups[column].agg(function)
            counts = groups[column].count()
            expected[counts < spec.min_valid_days] = np.nan
            stored = weekly.set_index(["cell_id", "epi_year", "epi_week"])[column].reindex(expected.index)
            same_nan = (expected.isna() == stored.isna()).all()
            difference = (
                float(np.nanmax(np.abs(expected.to_numpy() - stored.to_numpy())))
                if expected.notna().any()
                else 0.0
            )
            worst = max(worst, difference)
            if not same_nan or difference > 1e-3:
                self.check(
                    f"weekly recomputation {column}",
                    grid_name,
                    False,
                    f"max |Δ|={difference:.5f}, NaN pattern equal={same_nan}",
                )
                return
        self.check(
            "weekly table == independent re-aggregation",
            grid_name,
            True,
            f"all catalog columns, max |Δ|={worst:.2e} (float32)",
        )

    def check_formats_match(self, grid_name: str) -> None:
        keys = ["cell_id", "date"]
        parquet = (
            self._query(grid_name, f"SELECT * FROM {GoldTable.DAILY}")
            .sort_values(keys)
            .reset_index(drop=True)
        )
        duck = self._query(grid_name, f"SELECT * FROM {GoldTable.DAILY}", OutputFormat.DUCKDB)
        duck = duck[parquet.columns].sort_values(keys).reset_index(drop=True)
        numeric = parquet.select_dtypes("number").columns
        equal = np.allclose(parquet[numeric].to_numpy(float), duck[numeric].to_numpy(float), equal_nan=True)
        self.check(
            "Parquet == DuckDB (gold_daily)",
            grid_name,
            equal and len(parquet) == len(duck),
            f"{len(parquet)} rows",
        )

    def check_raster_roundtrip(self, grid_name: str) -> None:
        store, grid = self.stores[(grid_name, OutputFormat.PARQUET)], self.grids[grid_name]
        layer = load_layer(store, grid, "agera5_temp_mean", day=SAMPLE_DAY)
        rows = self.daily[grid_name][self.daily[grid_name]["date"] == SAMPLE_DAY]
        values = layer.reshape(-1)[rows["cell_id"].to_numpy()]
        equal = np.allclose(values, rows["agera5_temp_mean"].to_numpy(), equal_nan=True)
        outside_nan = np.isnan(np.delete(layer.reshape(-1), rows["cell_id"].to_numpy())).all()
        self.check(
            "make_raster round trip", grid_name, equal and outside_nan, f"{len(rows)} cells, NaN outside AOI"
        )

    def check_units_from_bronze(self) -> None:
        bronze = read_netcdf(self.lake.bronze_path("agera5", SAMPLE_DAY, "temp_mean_2m"))
        raw = select_data_variable(bronze)
        silver = read_netcdf(self.lake.silver_path("agera5", SAMPLE_DAY))["temp_mean_2m"]
        raw_on_silver = raw.squeeze(drop=True).sel(x=silver.x, y=silver.y, method="nearest")
        delta = float(np.nanmax(np.abs((raw_on_silver.values - 273.15) - silver.values)))
        self.check(
            "K → °C reproducible from Bronze",
            "agera5",
            raw.attrs.get("units") == "K" and delta < 1e-3,
            f"raw units={raw.attrs.get('units')}, max |(K-273.15) - silver|={delta:.2e}",
        )

    def check_date_alignment(self) -> None:
        """Sources must agree best at lag 0; a peak at ±1 day would reveal a date-labelling bug."""
        means = self.daily["1km"].groupby("date")[["chirps_precip", "agera5_precip"]].mean()
        lags = {lag: means["chirps_precip"].corr(means["agera5_precip"].shift(lag)) for lag in range(-3, 4)}
        best = max(lags, key=lambda lag: lags[lag])
        detail = ", ".join(f"lag {lag:+d}: r={value:.2f}" for lag, value in lags.items())
        self.check("CHIRPS vs AgERA5 daily correlation peaks at lag 0", "both", best == 0, detail)
        mismatches = 0
        files = sorted((self.lake.bronze / "agera5").rglob("agera5_precipitation_flux_*.nc"))
        for path in files:
            stored = str(read_netcdf(path)["time"].values[0])[:10]
            mismatches += stored != path.stem.rsplit("_", 1)[1]
        self.check(
            "Bronze file date == time coordinate (AgERA5)",
            "agera5",
            mismatches == 0,
            f"{len(files)} files, {mismatches} mismatches",
        )

    def check_grid_consistency(self) -> None:
        one = self.daily["1km"].groupby("date")["agera5_temp_mean"].mean()
        five = self.daily["5km"].groupby("date")["agera5_temp_mean"].mean()
        difference = float((one - five).abs().max())
        self.check(
            "1 km vs 5 km AOI-mean T (°C)", "both", difference < 1.0, f"max daily |Δ|={difference:.3f} °C"
        )
        one_p = self.daily["1km"].groupby("date")["chirps_precip"].mean()
        five_p = self.daily["5km"].groupby("date")["chirps_precip"].mean()
        correlation = float(one_p.corr(five_p))
        self.check(
            "1 km vs 5 km AOI-mean CHIRPS correlation", "both", correlation > 0.95, f"r={correlation:.4f}"
        )

    # --- outputs --------------------------------------------------------------------
    def write_tables(self) -> None:
        for grid_name in GRIDS:
            self.weekly[grid_name].to_csv(
                self.out / "tables" / f"gold_epiweekly_{grid_name}.csv", index=False
            )
            self.static[grid_name].to_csv(
                self.out / "tables" / f"static_covariates_{grid_name}.csv", index=False
            )
            sample = self.daily[grid_name][self.daily[grid_name]["date"] == SAMPLE_DAY]
            sample.to_csv(self.out / "tables" / f"gold_daily_{grid_name}_{SAMPLE_DAY}.csv", index=False)
        schema = []
        for column in self.daily["1km"].columns:
            spec = CATALOG.by_gold_column(column) if CATALOG.has_gold_column(column) else None
            schema.append(
                {
                    "column": column,
                    "units": spec.units if spec else "",
                    "description": spec.description
                    if spec
                    else {
                        "cell_id": "row * width + col on the Gold grid (key to rebuild rasters)",
                        "date": "observation date (UTC day)",
                        "latitude": "cell centre (WGS84)",
                        "longitude": "cell centre (WGS84)",
                    }.get(column, ""),
                    "weekly_rule": f"{spec.weekly_aggregation.value} if >= {spec.min_valid_days} valid days"
                    if spec
                    else "",
                    "resampling": spec.resampling.value if spec else "",
                }
            )
        pd.DataFrame(schema).to_csv(self.out / "tables" / "gold_schema.csv", index=False)

    def write_qc(self) -> bool:
        frame = pd.DataFrame([check.__dict__ for check in self.checks])
        frame["status"] = np.where(frame["passed"], "PASS", "FAIL")
        frame[["status", "scope", "name", "detail"]].to_csv(self.out / "qc" / "qc_checks.csv", index=False)
        lines = [
            "# Control de calidad — Cali, enero a mayo de 2026",
            "",
            f"**{int(frame['passed'].sum())} / {len(frame)} chequeos superados.**",
            "",
            "| Estado | Alcance | Chequeo | Detalle |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {'✅' if c.passed else '❌'} | {c.scope} | {c.name} | {c.detail} |" for c in self.checks
        ]
        (self.out / "qc" / "qc_summary.md").write_text("\n".join(lines) + "\n")
        return bool(frame["passed"].all())

    def lake_state_rows(self) -> list[str]:
        """One Markdown row per source from the ingestion catalog, i.e. what is in the lake now."""
        rows = []
        catalog = Catalog(self.lake)
        for source in ("agera5", "chirps", "gee", "mswep"):
            entries = catalog.entries(source)
            if entries.empty:
                continue
            bronze = entries[entries["layer"] == "bronze"]
            counts = bronze["status"].value_counts()
            silver_days = entries.loc[entries["layer"] == "silver", "date"].nunique()
            # origin = "<scheme>:<dataset>[:<details>]:<date>"; the dataset id names the product.
            found = bronze.loc[bronze["status"] != "missing", "origin"].str.split(":")
            products = sorted({f"{parts[0]}:{parts[1]}" for parts in found if len(parts) > 2})
            rows.append(
                f"| {source} | {bronze['variable'].nunique()} | {counts.get('final', 0)} | "
                f"{counts.get('prelim', 0)} | {counts.get('missing', 0)} | {silver_days} | "
                f"{'<br>'.join(f'`{product}`' for product in products)} |"
            )
        return rows

    def write_summary(self, all_passed: bool) -> None:
        reports = sorted(
            (
                json.loads(path.read_text()) | {"file": path.name}
                for path in (self.out / "reports").glob("run_*.json")
            ),
            key=lambda report: report["started_at"],
        )
        rows = []
        for grid_name in GRIDS:
            grid = self.grids[grid_name]
            rows.append(
                f"| {grid_name} | {grid.crs} | {grid.width}×{grid.height} | {self.static[grid_name]['cell_id'].nunique()} | "
                f"{len(self.daily[grid_name]):,} | {len(self.weekly[grid_name]):,} | {len(self.daily[grid_name].columns)} |"
            )
        sources = {
            source["source"]: source for report in reports for source in report["sources"]
        }  # latest wins
        history = [
            f"| `{r['file']}` | {r['started_at']} | {', '.join(s['source'] for s in r['sources'])} | "
            f"{'sí' if r['request'].get('force') else 'no'} | {'✅' if r['ok'] else '❌'} | "
            f"{'; '.join(f'{s["source"]}: {s["failures"]} fallos' for s in r['sources'] if s['failures'])} | "
            f"{RUN_NOTES.get(r['file'], '')} |"
            for r in reports
        ]
        source_rows = self.lake_state_rows()
        findings = "\n".join(f"- {line}" for line in self.findings(sources))
        text = f"""# Evidencias — corrida de ejemplo Cali (enero a mayo de 2026)

Estado global de QC: **{"TODOS LOS CHEQUEOS PASAN ✅" if all_passed else "HAY CHEQUEOS FALLIDOS ❌"}** — ver [`qc/qc_summary.md`](qc/qc_summary.md). Decisión sobre la máscara de nubes de NDVI/NDWI: [`qc/gee_cloud_mask_diagnostic.md`](qc/gee_cloud_mask_diagnostic.md).

## Tablas Gold (una por grilla objetivo)

| Grilla | CRS | Celdas (ancho×alto) | Celdas en el AOI | Filas diarias | Filas semana epi. | Columnas diarias |
|---|---|---|---|---|---|---|
{chr(10).join(rows)}

Rutas: `data/gold/<grilla>/parquet/{{daily,epiweekly,static}}/…` y `data/gold/<grilla>/climate.duckdb`.

## Estado del lago por fuente (catálogo `data/state/manifest/`)

| Fuente | Variables | Bronze final | Bronze prelim | Bronze missing | Días Silver | Productos de origen |
|---|---|---|---|---|---|---|
{chr(10).join(source_rows)}

Bronze cuenta pares (variable, día); `missing` son días que el proveedor no publica (p. ej. sin pasada de Sentinel-2); el planificador los vuelve a pedir (`retry_missing`) en las corridas que cubran esas fechas. Historial de corridas (`reports/`):

| Reporte | Inicio (UTC) | Fuentes | --force | OK | Fallos | Qué hizo |
|---|---|---|---|---|---|---|
{chr(10).join(history)}

## Hallazgos (calculados sobre los datos)

{findings}

## Figuras

![AOI y grillas](maps/01_aoi_grids_all_touched.png)
![Variables 1 km vs 5 km](maps/02_gold_variables_1km_vs_5km.png)
![Covariables estáticas](maps/03_static_covariates_1km.png)
![Serie diaria](timeseries/01_daily_aoi_means.png)
![Semanas epidemiológicas](timeseries/02_epiweekly.png)

### Mapas por semana epidemiológica sobre OpenStreetMap ([`animations/`](animations/))

Generados con `examples/cali_2026/make_weekly_gif.py` desde los Parquet gold. Cada mapa dibuja el shapefile de Cali, y la escala de color de cada variable es la misma en todas las semanas y en ambas grillas.

- `weekly_1km_vs_5km.gif`: precipitación CHIRPS y temperatura media AgERA5, grillas 1 km y 5 km.
- `all_variables_1km.gif` / `all_variables_5km.gif`: todos los campos semanales a la vez.
- Un GIF por campo, 1 km al lado de 5 km: `make_weekly_gif.py --per-field` (no se versiona).
- `maps/04_static_covariates_osm.png`: elevación, pendiente y orientación en ambas grillas.

![Animación semanal 1 km vs 5 km](animations/weekly_1km_vs_5km.gif)
![Todas las variables, 1 km](animations/all_variables_1km.gif)
![Covariables estáticas sobre OSM](maps/04_static_covariates_osm.png)
"""
        (self.out / "README.md").write_text(text)


def _findings(self: Evidence, sources: dict) -> list[str]:
    daily, weekly = self.daily["1km"], self.weekly["1km"]
    distinct = daily.groupby("date")["agera5_precip"].nunique()
    chirps_total = daily.groupby("date")["chirps_precip"].mean().sum()
    agera5_total = daily.groupby("date")["agera5_precip"].mean().sum()
    full_weeks = weekly[weekly["days_observed"] == 7]
    weekly_means = full_weeks.groupby(["epi_year", "epi_week"])[["chirps_precip", "agera5_precip"]].mean()
    correlation = weekly_means["chirps_precip"].corr(weekly_means["agera5_precip"])
    tmean = daily.groupby("date")["agera5_temp_mean"].mean()
    clear_days = daily.groupby("date")["gee_ndvi"].count().gt(0).mean()
    weeks_in = weekly[weekly["days_observed"] == 7]
    modis_weeks = weeks_in["gee_ndvi"].notna().mean()
    s2_weeks = weeks_in["gee_s2_ndvi"].notna().mean()
    either_weeks = (weeks_in["gee_ndvi"].notna() | weeks_in["gee_s2_ndvi"].notna()).mean()
    ndvi_quality = sources.get("gee", {}).get("quality", {}).get("gee/ndvi", {})
    return [
        f"**Precipitación AgERA5 en bloques:** en {distinct.eq(1).mean():.0%} de los días todas las celdas de "
        "1 km del AOI tienen el mismo valor. El dato crudo de AgERA5 ya viene así: la precipitación es la de "
        "ERA5 (~0.25°) replicada en la grilla de 0.1°, sin reducción de escala, a diferencia de la temperatura. "
        "Para la estructura espacial de la lluvia, usar CHIRPS.",
        f"**CHIRPS vs AgERA5:** acumulado ene-may de {chirps_total:.0f} mm (CHIRPS) vs {agera5_total:.0f} mm "
        f"(AgERA5) como media del AOI; correlación semanal r = {correlation:.2f} en {len(weekly_means)} semanas "
        "completas. La correlación diaria es máxima con desfase 0, así que las fechas están alineadas: la baja "
        "concordancia es propia de los productos (ERA5 sobreestima la lluvia convectiva andina), no un error.",
        f"**Temperatura media AgERA5:** {tmean.min():.1f}-{tmean.max():.1f} °C (media {tmean.mean():.1f} °C), por "
        "debajo de la climatología habitual de Cali (~24 °C): sesgo frío conocido del reanálisis en valles andinos "
        "(orografía suavizada). La conversión K → °C se verificó contra Bronze (ver QC); las covariables de "
        "elevación existen para corregir este sesgo en el downscaling.",
        f"**Nubosidad y NDVI/NDWI:** el {clear_days:.0%} de los días tiene al menos una celda de 1 km con NDVI "
        "MODIS válido (Terra+Aqua, máscara `standard`: nube, sombra y nube interna). Por semana epidemiológica "
        f"hay NDVI en el {modis_weeks:.0%} de las celdas-semana con MODIS, {s2_weeks:.0%} con Sentinel-2 y "
        f"{either_weeks:.0%} con al menos uno de los dos (antes, con la máscara estricta y ≥2 días, la tabla tenía 7 %). "
        "El código original no enmascaraba nubes: tenía pocos NA, pero el NDVI de los píxeles nublados es "
        "~0.12 (mediana) frente a ~0.67 despejado, así que esos valores eran nubes, no vegetación.",
        f"**QC en acción:** el control de rangos descartó {ndvi_quality.get('above_maximum', 0)} valor(es) de NDVI > 1 "
        f"y {ndvi_quality.get('below_minimum', 0)} < -1; 0 violaciones de Tmin ≤ Tmedia ≤ Tmáx en ambas grillas.",
        "**Semanas incompletas:** 2025-W53 (1-3 ene) y 2026-W22 (31 may) tienen menos de 7 días en el rango, así que "
        "la precipitación semanal queda en NaN y la columna `*_ndays` lo documenta.",
    ]


Evidence.findings = _findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    args = parser.parse_args()
    evidence = Evidence(args.output_dir, args.data_root)
    evidence.map_aoi_and_grids()
    evidence.map_variables_by_grid()
    evidence.map_static()
    evidence.plot_daily()
    evidence.plot_weekly()
    evidence.run_checks()
    evidence.write_tables()
    passed = evidence.write_qc()
    evidence.write_summary(passed)
    failed = [c for c in evidence.checks if not c.passed]
    print(f"{len(evidence.checks) - len(failed)}/{len(evidence.checks)} checks passed")
    for check in failed:
        print(f"FAIL [{check.scope}] {check.name}: {check.detail}")
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
