"""Figures of the operational run on every Gold grid (100 m, 500 m, 1 km, 5 km).

Writes under ``--output-dir`` (default ``outputs/cali_operational``):

* ``maps/resoluciones_semana.png``     key variables of one epidemiological week, one column per grid;
* ``maps/variables_<grid>.png``         every family of variables of that week on one grid;
* ``maps/estaticas_resoluciones.png``  terrain and urban covariates, one column per grid;
* ``maps/semanas/resoluciones_<YYYY>-W<ww>.png`` the same four variables for every complete week,
  colour scales fixed over all weeks;
* ``animations/semanal_resoluciones.gif`` those weekly maps as an animation;
* ``timeseries/series_diarias.png``    daily AOI means of one variable per family (1 km grid).

Colour scales are one hue per family and are shared across grids (and weeks in the GIF), so
resolutions are directly comparable. Cells are drawn from ``cell_id`` on each grid's
``grid.json``; the Cali barrios outline is overlaid.

    uv run python examples/cali_operational/make_figures.py --data-root data/operational
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import duckdb
import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import Normalize
from PIL import Image

from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.storage.layout import DataLake

AOI_PATH = Path("data/auxiliary/shapefiles/cali/cali.shp")
GRIDS = ("100m", "500m", "1km", "5km")
GRID_LABELS = {"100m": "100 m", "500m": "500 m", "1km": "1 km", "5km": "5 km"}
INK, MUTED, OUTLINE = "#1c2320", "#5b6761", "#2b2f2d"

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "axes.edgecolor": "#c9cec8",
        "axes.labelcolor": MUTED,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "axes.titlecolor": INK,
        "figure.facecolor": "white",
    }
)


@dataclass(frozen=True)
class Field:
    column: str
    label: str
    units: str
    cmap: str
    weekly_sum: bool = False
    #: Optical/revisit sensors: an empty week means clouds or no overpass, not latency.
    optical: bool = False


# One sequential single-hue ramp per family.
WEEK_FIELDS = (
    Field("imerg_precip", "Precipitación IMERG (suma)", "mm/semana", "Blues", weekly_sum=True),
    Field("agera5_temp_max", "Temperatura máxima AgERA5", "°C", "Oranges"),
    Field("agera5_temp_min", "Temperatura mínima AgERA5", "°C", "Oranges"),
    Field("agera5_rh_min", "Humedad relativa mínima AgERA5", "%", "GnBu"),
    Field("agera5_wind_speed", "Viento a 10 m AgERA5", "m/s", "Purples"),
    Field("agera5_solar_radiation", "Radiación solar AgERA5", "MJ/m²/día", "YlOrBr"),
    Field("goes_cold_cloud_frac_235", "Nube fría GOES (<235 K)", "fracción", "Greys"),
    Field("modis_lst_day_terra", "LST diurna MODIS Terra", "°C", "Reds", optical=True),
    Field("gee_s2_ndvi", "NDVI Sentinel-2", "índice", "Greens", optical=True),
    Field("smap_sm_surface", "Humedad del suelo SMAP", "m³/m³", "BuPu"),
    Field("era5land_skin_temp", "Temperatura de piel ERA5-Land", "°C", "Reds"),
    Field("viirs_ntl", "Luces nocturnas VIIRS", "nW/cm²/sr", "Purples"),
)
COMPARE_FIELDS = ("imerg_precip", "agera5_temp_max", "goes_cold_cloud_frac_235", "gee_s2_ndvi")
STATIC_FIELDS = (
    Field("static_elevation_m", "Elevación", "m", "Greys"),
    Field("urban_buildings_frac", "Huella de edificios", "fracción", "Oranges"),
    Field("urban_osm_road_density_km_km2", "Densidad de vías", "km/km²", "Purples"),
    Field("urban_landsat_lst_median", "LST mediana anual Landsat", "°C", "Reds"),
)
FIELDS = {field.column: field for field in (*WEEK_FIELDS, *STATIC_FIELDS)}


def read(lake: DataLake, grid: str, table: str, where: str = "") -> pd.DataFrame:
    folder = {"daily": "daily", "weekly": "epiweekly", "static": "static"}[table]
    pattern = lake.gold_layout(grid).parquet_root / folder / "**" / "*.parquet"
    query = (
        f"SELECT * FROM read_parquet('{pattern}', union_by_name = true, hive_partitioning = false) {where}"
    )
    with duckdb.connect() as connection:
        return connection.execute(query).df()


def to_raster(frame: pd.DataFrame, grid: GridSpec, column: str) -> np.ndarray:
    values = np.full(grid.width * grid.height, np.nan, dtype="float32")
    if column in frame:
        values[frame["cell_id"].to_numpy()] = frame[column].to_numpy(dtype="float32", na_value=np.nan)
    return values.reshape(grid.shape)


def extent(grid: GridSpec) -> tuple[float, float, float, float]:
    left, bottom, right, top = grid.bounds
    return (left, right, bottom, top)


class Figures:
    def __init__(self, data_root: Path, output_dir: Path) -> None:
        self.lake = DataLake(data_root)
        self.output_dir = output_dir
        self.grids = {name: GridSpec.load(self.lake.gold_layout(name).grid_path) for name in GRIDS}
        aoi = gpd.read_file(AOI_PATH).to_crs(self.grids["1km"].crs)
        self.barrios = aoi
        self.outline = gpd.GeoSeries([aoi.union_all()], crs=aoi.crs)
        self.weekly = {name: read(self.lake, name, "weekly") for name in GRIDS}
        self.static = {name: read(self.lake, name, "static") for name in GRIDS}
        self.view = self._view_bounds()

    def _view_bounds(self) -> tuple[float, float, float, float]:
        min_x, min_y, max_x, max_y = self.outline.total_bounds
        pad = 600.0
        return (min_x - pad, max_x + pad, min_y - pad, max_y + pad)

    def week(self, label: str | None = None) -> tuple[int, int]:
        weeks = self.weekly["1km"][["epi_year", "epi_week", "days_observed"]].drop_duplicates()
        complete = weeks[weeks["days_observed"] == 7].sort_values(["epi_year", "epi_week"])
        if label:
            year, week = label.split("-W")
            return int(year), int(week)
        # Latest complete week where the slowest daily source (AgERA5, ~8 d) is present.
        frame = self.weekly["1km"]
        good = frame.groupby(["epi_year", "epi_week"])["agera5_temp_max"].count()
        good = good[good > 0].index.tolist()
        candidates = [
            tuple(row) for row in complete[["epi_year", "epi_week"]].to_numpy() if tuple(row) in good
        ]
        return candidates[-1]

    def norm(self, column: str, frames: dict[str, pd.DataFrame]) -> Normalize:
        values = np.concatenate(
            [frame[column].dropna().to_numpy(dtype="float64") for frame in frames.values() if column in frame]
        )
        if values.size == 0:
            return Normalize(0, 1)
        low, high = np.nanpercentile(values, [2, 98])
        if high <= low:
            high = low + 1e-6
        return Normalize(low, high)

    def panel(
        self, ax, grid: str, frame: pd.DataFrame, field: Field, norm: Normalize, title: str | None = None
    ):
        spec = self.grids[grid]
        cmap = plt.get_cmap(field.cmap).copy()
        cmap.set_bad(alpha=0.0)
        image = ax.imshow(
            to_raster(frame, spec, field.column),
            extent=extent(spec),
            origin="upper",
            cmap=cmap,
            norm=norm,
            interpolation="nearest",
        )
        self.barrios.boundary.plot(ax=ax, color=OUTLINE, linewidth=0.15, alpha=0.35)
        self.outline.boundary.plot(ax=ax, color=OUTLINE, linewidth=0.8)
        ax.set_xlim(self.view[0], self.view[1])
        ax.set_ylim(self.view[2], self.view[3])
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_xlabel("")
        ax.set_ylabel("")
        ax.set_aspect("equal")
        for side in ax.spines.values():
            side.set_visible(False)
        if title:
            ax.set_title(title, fontsize=9, loc="left", pad=4)
        if frame.empty or field.column not in frame or frame[field.column].isna().all():
            reason = (
                "sin dato\n(nubes o sin pasada)"
                if field.optical
                else "sin dato todavía\n(semana aún no publicada completa)"
            )
            ax.text(
                0.5,
                0.5,
                reason,
                transform=ax.transAxes,
                ha="center",
                va="center",
                color=MUTED,
                fontsize=8,
            )
        return image

    def colorbar(self, fig, image, axes, field: Field) -> None:
        bar = fig.colorbar(image, ax=axes, shrink=0.85, pad=0.01, aspect=20)
        bar.set_label(field.units, color=MUTED)
        bar.outline.set_visible(False)

    def save(self, fig, relative: str) -> Path:
        path = self.output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(path)
        return path

    # --- figures ---------------------------------------------------------------------------
    def complete_weeks(self) -> list[tuple[int, int]]:
        frame = self.weekly["1km"]
        weeks = frame[frame["days_observed"] == 7][["epi_year", "epi_week"]].drop_duplicates()
        return [tuple(int(v) for v in row) for row in weeks.sort_values(["epi_year", "epi_week"]).to_numpy()]

    def week_dates(self, year: int, week: int) -> str:
        rows = self.weekly["1km"].query("epi_year == @year and epi_week == @week")
        start, end = pd.Timestamp(rows["week_start"].iloc[0]), pd.Timestamp(rows["week_end"].iloc[0])
        return f"{start:%d %b} – {end:%d %b %Y}"

    def resolutions_week(
        self,
        year: int,
        week: int,
        relative: str = "maps/resoluciones_semana.png",
        shared_scale: bool = False,
        dpi: int = 150,
    ) -> Path:
        """Key variables of one week, one column per grid. ``shared_scale`` fixes the colour scale
        over every week (for the weekly series and the GIF); otherwise it fits this week."""
        frames = {g: self.weekly[g].query("epi_year == @year and epi_week == @week") for g in GRIDS}
        fig, axes = plt.subplots(
            len(COMPARE_FIELDS), len(GRIDS), figsize=(13, 3.1 * len(COMPARE_FIELDS)), layout="constrained"
        )
        for row, column in enumerate(COMPARE_FIELDS):
            field = FIELDS[column]
            norm = self.norm(column, self.weekly if shared_scale else frames)
            image = None
            for col, grid in enumerate(GRIDS):
                title = f"{GRID_LABELS[grid]}" if row == 0 else None
                image = self.panel(axes[row, col], grid, frames[grid], field, norm, title)
            axes[row, 0].set_ylabel(field.label, fontsize=9, color=INK)
            self.colorbar(fig, image, axes[row, :].tolist(), field)
        fig.suptitle(
            f"Cali · semana epidemiológica {year}-W{week:02d} ({self.week_dates(year, week)}) "
            "en 100 m, 500 m, 1 km y 5 km",
            x=0.02,
            ha="left",
            fontsize=12,
            color=INK,
        )
        path = self.output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        print(path)
        return path

    def weekly_series(self) -> list[Path]:
        """One PNG per complete epidemiological week, colour scales fixed over all weeks."""
        return [
            self.resolutions_week(
                year, week, f"maps/semanas/resoluciones_{year}-W{week:02d}.png", shared_scale=True
            )
            for year, week in self.complete_weeks()
        ]

    def variables_grid(self, grid: str, year: int, week: int) -> None:
        frame = self.weekly[grid].query("epi_year == @year and epi_week == @week")
        frames = {name: self.weekly[name].query("epi_year == @year and epi_week == @week") for name in GRIDS}
        cols = 4
        rows = int(np.ceil(len(WEEK_FIELDS) / cols))
        fig, axes = plt.subplots(rows, cols, figsize=(14, 3.6 * rows), layout="constrained")
        for ax, field in zip(axes.flat, WEEK_FIELDS, strict=False):
            image = self.panel(ax, grid, frame, field, self.norm(field.column, frames), field.label)
            self.colorbar(fig, image, ax, field)
        for ax in list(axes.flat)[len(WEEK_FIELDS) :]:
            ax.axis("off")
        fig.suptitle(
            f"Cali · {GRID_LABELS[grid]} · semana {year}-W{week:02d} · una variable por familia",
            x=0.02,
            ha="left",
            fontsize=12,
            color=INK,
        )
        self.save(fig, f"maps/variables_{grid}.png")

    def statics(self) -> None:
        fig, axes = plt.subplots(
            len(STATIC_FIELDS), len(GRIDS), figsize=(13, 3.1 * len(STATIC_FIELDS)), layout="constrained"
        )
        for row, field in enumerate(STATIC_FIELDS):
            norm = self.norm(field.column, self.static)
            image = None
            for col, grid in enumerate(GRIDS):
                title = GRID_LABELS[grid] if row == 0 else None
                image = self.panel(axes[row, col], grid, self.static[grid], field, norm, title)
            axes[row, 0].set_ylabel(field.label, fontsize=9, color=INK)
            self.colorbar(fig, image, axes[row, :].tolist(), field)
        fig.suptitle(
            "Cali · covariables estáticas en las cuatro resoluciones",
            x=0.02,
            ha="left",
            fontsize=12,
            color=INK,
        )
        self.save(fig, "maps/estaticas_resoluciones.png")

    def weekly_gif(self) -> None:
        frames = []
        for year, week in self.complete_weeks():
            path = self.resolutions_week(
                year, week, f"animations/_frames/{year}-W{week:02d}.png", shared_scale=True, dpi=80
            )
            frames.append(Image.open(path).convert("RGB"))
            path.unlink()
        (self.output_dir / "animations/_frames").rmdir()
        size = frames[0].size
        frames = [frame.resize(size) for frame in frames]
        path = self.output_dir / "animations/semanal_resoluciones.gif"
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=1400, loop=0)
        print(path)

    def timeseries(self) -> None:
        daily = read(self.lake, "1km", "daily")
        daily["date"] = pd.to_datetime(daily["date"])
        means = daily.groupby("date").mean(numeric_only=True)
        panels = (
            (("imerg_precip",), "Precipitación IMERG", "mm/día", "bar"),
            (("agera5_temp_max", "agera5_temp_min"), "Temperatura máxima y mínima AgERA5", "°C", "line"),
            (("agera5_rh_max", "agera5_rh_min"), "Humedad relativa máx. y mín. AgERA5", "%", "line"),
            (("agera5_wind_speed",), "Viento a 10 m AgERA5", "m/s", "line"),
            (("agera5_solar_radiation",), "Radiación solar AgERA5", "MJ/m²/día", "line"),
            (("goes_cold_cloud_frac_235",), "Nube fría GOES (<235 K)", "fracción del día", "line"),
            (("modis_lst_day_aqua", "modis_lst_night_aqua"), "LST MODIS Aqua día y noche", "°C", "dots"),
            (("smap_sm_surface",), "Humedad del suelo SMAP", "m³/m³", "line"),
        )
        series_colors = ("#2a6fdb", "#d1652b")
        labels = {
            "agera5_temp_max": "máxima",
            "agera5_temp_min": "mínima",
            "agera5_rh_max": "máxima",
            "agera5_rh_min": "mínima",
            "modis_lst_day_aqua": "día (13:30)",
            "modis_lst_night_aqua": "noche (01:30)",
        }
        fig, axes = plt.subplots(4, 2, figsize=(13, 11), sharex=True)
        for ax, (columns, title, units, kind) in zip(axes.flat, panels, strict=True):
            for index, column in enumerate(columns):
                if column not in means:
                    continue
                series = means[column]
                color = series_colors[index]
                if kind == "bar":
                    ax.bar(series.index, series.to_numpy(), width=0.8, color=color)
                elif kind == "dots":
                    ax.plot(
                        series.index,
                        series.to_numpy(),
                        "o",
                        markersize=3.5,
                        color=color,
                        label=labels.get(column, column),
                    )
                else:
                    ax.plot(
                        series.index,
                        series.to_numpy(),
                        linewidth=2,
                        color=color,
                        label=labels.get(column, column),
                    )
            ax.set_title(title, loc="left", fontsize=10)
            ax.set_ylabel(units)
            ax.grid(axis="y", color="#e6e9e4", linewidth=0.8)
            ax.set_axisbelow(True)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            if len(columns) > 1:
                ax.legend(
                    frameon=False,
                    loc="lower right",
                    bbox_to_anchor=(1.0, 1.0),
                    fontsize=8,
                    ncols=2,
                    borderaxespad=0.2,
                )
        fig.suptitle(
            "Cali · promedio diario sobre el AOI (grilla de 1 km), 30 ago – 7 nov 2026",
            x=0.02,
            ha="left",
            fontsize=12,
            color=INK,
        )
        fig.autofmt_xdate()
        fig.tight_layout(rect=(0, 0, 1, 0.97))
        self.save(fig, "timeseries/series_diarias.png")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("data/operational"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/cali_operational"))
    parser.add_argument("--week", help="Epidemiological week as YYYY-Www (default: latest complete one)")
    args = parser.parse_args()
    figures = Figures(args.data_root, args.output_dir)
    year, week = figures.week(args.week)
    figures.resolutions_week(year, week)
    for grid in GRIDS:
        figures.variables_grid(grid, year, week)
    figures.statics()
    figures.weekly_series()
    figures.weekly_gif()
    figures.timeseries()


if __name__ == "__main__":
    main()
