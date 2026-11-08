"""Epidemiological-week maps of every Gold field over an OpenStreetMap basemap.

Reads the Gold Parquet tables of the 1 km and 5 km grids and writes, under ``--output-dir``:

* ``animations/weekly_1km_vs_5km.gif``     CHIRPS precipitation and AgERA5 mean temperature, both grids;
* ``animations/all_variables_{grid}.gif``  every weekly field as small multiples, one GIF per grid;
* ``animations/variables/{field}.gif``     with ``--per-field``: one GIF per weekly field, 1 km next to
  5 km (not versioned: the ``all_variables`` GIFs already show every field);
* ``maps/04_static_covariates_osm.png``    DEM covariates on both grids;
* PNG previews of the wettest week and of the week with the most NDVI coverage.

Every map draws the Cali shapefile (barrios thin, urban perimeter thick). Colour scales are fixed
per field across weeks and grids, so weeks and resolutions are directly comparable. The script
fails if a Gold table holds a field without a style below, so no column is silently skipped.

Usage: ``uv run python examples/cali_2026/make_weekly_gif.py --output-dir outputs/...``
(needs internet to fetch the OpenStreetMap tiles). Tiles come from the OpenStreetMap Germany
community server: tile.openstreetmap.org served its "Access blocked" placeholder to this client.
Requests identify the project and use a single connection, as the OSM tile usage policies ask.
"""

from __future__ import annotations

import argparse
import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import contextily as ctx
import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.collections import Collection, PatchCollection
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.figure import Figure
from matplotlib.text import Text
from PIL import Image, ImageColor
from shapely.geometry import box

from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.storage.gold import GoldTable, OutputFormat, open_gold_store
from climate_pipeline.storage.layout import DataLake

AOI_PATH = Path("data/auxiliary/shapefiles/cali/cali.shp")
GRIDS = ("1km", "5km")
WEB_MERCATOR = "EPSG:3857"
# OSM tile usage policy: identify the application, keep request volume low.
OSM_USER_AGENT = "climate-pipeline/0.1 (+https://github.com/Felipep17/climate-pipeline)"
# One palette shared by every frame, without dithering: the basemap keeps its colours from frame
# to frame and the GIFs are about half the size of per-frame palettes, with no visible loss. The
# ramp anchors are always in the palette, otherwise the rare dark ends of the colour bars collapse.
GIF_ADAPTIVE_COLORS = 160
MONTHS_ES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
plt.rcParams.update({"figure.dpi": 110, "font.size": 9, "axes.titlesize": 10})

# Single-hue sequential ramps (light = low, dark = high), one hue per physical family, and a
# blue <-> red diverging ramp with a neutral grey midpoint for signed aspect components.
BLUE = ("#cde2fb", "#6da7ec", "#256abf", "#0d366b")
ORANGE = ("#fde3d4", "#f4a27c", "#eb6834", "#8a2e0c")
AQUA = ("#d3f2e6", "#7fd6b4", "#1baf7a", "#0b5e40")
YELLOW = ("#fdf0cc", "#f5cd66", "#eda100", "#7a5300")
VIOLET = ("#e2defa", "#a79cf0", "#6250d6", "#2e2380")
GREEN = ("#d6f0d0", "#7cc46e", "#008300", "#004200")
BROWN = ("#efe6dc", "#c9a27e", "#8c5a2e", "#4a2a10")
DIVERGING = ("#256abf", "#f0efec", "#e34948")
RAMP_ANCHORS = (*BLUE, *ORANGE, *AQUA, *YELLOW, *VIOLET, *GREEN, *BROWN, *DIVERGING)


@dataclass(frozen=True)
class Field:
    column: str
    label: str
    units: str
    ramp: tuple[str, ...]
    value_format: str = "{:.1f}"
    zero_floor: bool = False
    symmetric: bool = False

    def cmap(self) -> LinearSegmentedColormap:
        cmap = LinearSegmentedColormap.from_list(self.column, self.ramp)
        cmap.set_bad((0, 0, 0, 0))
        return cmap

    def describe(self, value: float) -> str:
        return f"{self.value_format.format(value)} {self.units}".rstrip()


WEEKLY_FIELDS = (
    Field("chirps_precip", "CHIRPS · precipitación (suma)", "mm/sem", BLUE, "{:.0f}", zero_floor=True),
    Field("agera5_precip", "AgERA5 · precipitación (suma)", "mm/sem", BLUE, "{:.0f}", zero_floor=True),
    Field("agera5_temp_min", "AgERA5 · temperatura mínima", "°C", ORANGE),
    Field("agera5_temp_mean", "AgERA5 · temperatura media", "°C", ORANGE),
    Field("agera5_temp_max", "AgERA5 · temperatura máxima", "°C", ORANGE),
    Field("agera5_dewpoint_mean", "AgERA5 · punto de rocío", "°C", AQUA),
    Field("agera5_vapour_pressure", "AgERA5 · presión de vapor", "hPa", AQUA),
    Field("agera5_rh_06h", "AgERA5 · humedad relativa 06 h", "%", AQUA, "{:.0f}"),
    Field("agera5_rh_15h", "AgERA5 · humedad relativa 15 h", "%", AQUA, "{:.0f}"),
    Field("agera5_solar_radiation", "AgERA5 · radiación solar", "MJ/m²/día", YELLOW),
    Field("agera5_wind_speed", "AgERA5 · viento a 10 m", "m/s", VIOLET, "{:.2f}"),
    Field("gee_ndvi", "MODIS · NDVI", "", GREEN, "{:.2f}"),
    Field("gee_ndwi", "MODIS · NDWI (Gao)", "", AQUA, "{:.2f}"),
    Field("gee_s2_ndvi", "Sentinel-2 · NDVI", "", GREEN, "{:.2f}"),
    Field("gee_s2_ndwi", "Sentinel-2 · NDWI (Gao)", "", AQUA, "{:.2f}"),
)
STATIC_FIELDS = (
    Field("static_elevation_m", "Elevación media", "m", BROWN, "{:.0f}"),
    Field("static_slope_deg", "Pendiente media", "°", VIOLET),
    Field("static_northness", "Orientación norte (cos aspecto)", "", DIVERGING, "{:.2f}", symmetric=True),
    Field("static_eastness", "Orientación este (sen aspecto)", "", DIVERGING, "{:.2f}", symmetric=True),
)
FIELDS = {field.column: field for field in (*WEEKLY_FIELDS, *STATIC_FIELDS)}
FIELD_PREFIXES = ("chirps_", "mswep_", "agera5_", "gee_", "static_")


@dataclass
class Slot:
    """One animated panel: its cell polygons, its label and its weeks x cells values."""

    collection: Collection
    annotation: Text
    field: Field
    values: np.ndarray

    def show(self, week: int) -> None:
        values = self.values[week]
        self.collection.set_array(np.ma.masked_invalid(values))
        self.annotation.set_text(
            f"media celdas: {self.field.describe(np.nanmean(values))}"
            if np.isfinite(values).any()
            else "sin dato válido esta semana"
        )


def cell_polygons(grid: GridSpec, cell_ids: np.ndarray) -> gpd.GeoDataFrame:
    a, _, c, _, e, f = grid.transform
    rows, cols = np.divmod(cell_ids, grid.width)
    geometries = [
        box(c + col * a, f + (row + 1) * e, c + (col + 1) * a, f + row * e)
        for row, col in zip(rows, cols, strict=True)
    ]
    return gpd.GeoDataFrame({"cell_id": cell_ids}, geometry=geometries, crs=grid.crs)


def week_label(week: pd.Series) -> str:
    start, end = week["week_start"], week["week_end"]
    return (
        f"Semana epidemiológica {week['epi_year']}-W{week['epi_week']:02d}  ·  "
        f"{start.day:02d} {MONTHS_ES[start.month - 1]} – {end.day:02d} {MONTHS_ES[end.month - 1]} {end.year}"
    )


def field_norm(field: Field, values: np.ndarray) -> Normalize:
    """Colour limits shared by every week and grid (1st-99th percentile, robust to outliers)."""
    values = values[np.isfinite(values)]
    if field.symmetric:
        limit = float(np.percentile(np.abs(values), 99)) or 1.0
        return Normalize(-limit, limit)
    low = 0.0 if field.zero_floor else float(np.percentile(values, 1))
    high = float(np.percentile(values, 99))
    return Normalize(low, max(high, low + 1e-6))


def value_columns(frame: pd.DataFrame) -> set[str]:
    return {c for c in frame.columns if c.startswith(FIELD_PREFIXES) and not c.endswith("_ndays")}


def faded_basemap(bounds: tuple[float, float, float, float], zoom: int) -> tuple[np.ndarray, tuple]:
    """OpenStreetMap basemap for ``bounds`` (Web Mercator), slightly faded so data stand out."""
    image, extent = ctx.bounds2img(
        *bounds,
        zoom=zoom,
        source=ctx.providers.OpenStreetMap.DE,
        n_connections=1,
        headers={"user-agent": OSM_USER_AGENT},
    )
    rgb = image[..., :3].astype("float32") / 255.0
    return 0.6 * rgb + 0.4, extent


class WeeklyMaps:
    def __init__(self, data_root: Path, output_dir: Path, zoom: int, fps: float) -> None:
        self.lake = DataLake(data_root)
        self.out = output_dir
        self.fps = fps
        for sub in ("animations", "maps"):
            (self.out / sub).mkdir(parents=True, exist_ok=True)
        self.barrios = gpd.read_file(AOI_PATH).to_crs(WEB_MERCATOR)
        self.perimeter = self.barrios.dissolve().boundary
        self.weekly = {name: self._read(name, GoldTable.EPIWEEKLY) for name in GRIDS}
        self.static = {name: self._read(name, GoldTable.STATIC) for name in GRIDS}
        self._check_every_field_has_a_style()

        self.cells = {}
        for name in GRIDS:
            grid = GridSpec.load(self.lake.gold_layout(name).grid_path)
            cell_ids = np.sort(self.static[name]["cell_id"].unique())
            self.cells[name] = cell_polygons(grid, cell_ids).to_crs(WEB_MERCATOR)

        # Timeline: weeks where the anchor fields exist on both grids (drops the truncated
        # 2025-W53 / 2026-W22). Sparse fields (NDVI/NDWI) show "sin dato" on their empty weeks.
        anchors = ["chirps_precip", "agera5_temp_mean"]
        counts = pd.concat(self.weekly.values()).groupby(["epi_year", "epi_week"])[anchors].count()
        self.weeks = (
            self.weekly["1km"][["epi_year", "epi_week", "week_start", "week_end"]]
            .drop_duplicates()
            .set_index(["epi_year", "epi_week"])
            .loc[counts[(counts > 0).all(axis=1)].index]
            .sort_index()
            .reset_index()
        )
        week_index = pd.MultiIndex.from_frame(self.weeks[["epi_year", "epi_week"]])
        self.matrices = {
            (name, field.column): self.weekly[name]
            .set_index(["epi_year", "epi_week", "cell_id"])[field.column]
            .unstack("cell_id")
            .reindex(index=week_index, columns=self.cells[name]["cell_id"])
            .to_numpy(dtype="float64")
            for name in GRIDS
            for field in WEEKLY_FIELDS
        }
        self.norms = {
            field.column: field_norm(
                field,
                pd.concat(frame[field.column] for frame in tables.values()).to_numpy("float64"),
            )
            for fields, tables in ((WEEKLY_FIELDS, self.weekly), (STATIC_FIELDS, self.static))
            for field in fields
        }

        west, south, east, north = self.cells["5km"].total_bounds
        pad = 0.04 * max(east - west, north - south)
        self.view = (west - pad, south - pad, east + pad, north + pad)
        self.basemap, self.basemap_extent = faded_basemap(self.view, zoom)

    def _read(self, grid_name: str, table: GoldTable) -> pd.DataFrame:
        store = open_gold_store(self.lake.gold_layout(grid_name), OutputFormat.PARQUET)
        frame = store.query(f"SELECT * FROM {table}")
        for column in ("week_start", "week_end"):
            if column in frame.columns:
                frame[column] = pd.to_datetime(frame[column]).dt.date
        return frame

    def _check_every_field_has_a_style(self) -> None:
        found = set().union(*(value_columns(f) for f in (*self.weekly.values(), *self.static.values())))
        if missing := sorted(found - FIELDS.keys()):
            raise ValueError(f"Gold fields without a map style (add them to WEEKLY/STATIC_FIELDS): {missing}")
        if absent := sorted(FIELDS.keys() - found):
            raise ValueError(f"styled fields absent from the Gold tables: {absent}")

    # --- drawing --------------------------------------------------------------------
    def panel(self, ax: Axes, grid_name: str, field: Field, title: str, fontsize: float = 9) -> tuple:
        ax.imshow(self.basemap, extent=self.basemap_extent, interpolation="bilinear", zorder=0)
        cells = self.cells[grid_name]
        existing = len(ax.collections)
        cells.plot(
            ax=ax,
            column=np.zeros(len(cells)),
            cmap=field.cmap(),
            norm=self.norms[field.column],
            edgecolor="white",
            linewidth=0.4,
            alpha=0.68,
            zorder=1,
        )
        self.barrios.plot(ax=ax, facecolor="none", edgecolor="#3b3b3b", linewidth=0.2, zorder=2)
        self.perimeter.plot(ax=ax, color="#111111", linewidth=1.1, zorder=3)
        ax.set_xlim(self.view[0], self.view[2])
        ax.set_ylim(self.view[1], self.view[3])
        ax.set_axis_off()
        ax.set_title(title, loc="left", fontsize=fontsize + 1)
        annotation = ax.text(
            0.02,
            0.02,
            "",
            transform=ax.transAxes,
            fontsize=fontsize,
            color="#111111",
            zorder=4,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.85, "pad": 2.5},
        )
        ax.text(
            0.98,
            0.01,
            "© OpenStreetMap contributors",
            transform=ax.transAxes,
            ha="right",
            fontsize=fontsize - 3,
            color="#444444",
            zorder=4,
        )
        # The polygon fill, not the edge LineCollection geopandas may add alongside it.
        fill = next(c for c in ax.collections[existing:] if isinstance(c, PatchCollection))
        return fill, annotation

    def colorbar(self, fig: Figure, axes: Axes | Sequence[Axes], field: Field, **kwargs) -> None:
        mappable = plt.cm.ScalarMappable(norm=self.norms[field.column], cmap=field.cmap())
        colorbar = fig.colorbar(mappable, ax=axes, extend="neither" if field.symmetric else "max", **kwargs)
        colorbar.set_label(field.units or "adimensional")
        colorbar.outline.set_visible(False)

    def footer(self, fig: Figure, y: float = 0.012) -> None:
        fig.text(
            0.5,
            y,
            "Gold Parquet (EPSG:9377) · celdas que tocan el perímetro urbano de Cali (all_touched) · "
            "escala de color fija por variable en todas las semanas y grillas",
            ha="center",
            fontsize=7.5,
            color="#555555",
        )

    def best_weeks(self) -> dict[str, int]:
        """Weeks used for static previews: wettest (CHIRPS) and clearest (NDVI coverage) on 1 km."""
        chirps = np.nanmean(self.matrices[("1km", "chirps_precip")], axis=1)
        ndvi = np.isfinite(self.matrices[("1km", "gee_ndvi")]).sum(axis=1)
        return {"wettest": int(np.nanargmax(chirps)), "clearest": int(np.argmax(ndvi))}

    def animate(
        self,
        fig: Figure,
        slots: list[Slot],
        path: Path,
        dpi: int,
        heading: str = "",
        previews: Sequence[int] = (),
        title_size: float = 12,
    ) -> list[Path]:
        title = fig.suptitle("", fontsize=title_size, fontweight="bold")

        def draw(week: int) -> None:
            title.set_text(f"{heading}\n{week_label(self.weeks.iloc[week])}".strip())
            for slot in slots:
                slot.show(week)

        frames = []
        for week in range(len(self.weeks)):
            draw(week)
            frames.append(render(fig, dpi))
        save_gif(frames, path, duration_ms=round(1000 / self.fps))
        written = [path]
        for week in previews:
            draw(week)
            row = self.weeks.iloc[week]
            preview = path.with_name(f"{path.stem}_{row['epi_year']}-W{row['epi_week']:02d}.png")
            quantize([render(fig, dpi, tight=True)])[0].save(preview, optimize=True)
            written.append(preview)
        plt.close(fig)
        return written

    def comparison(self, fields: Sequence[Field], path: Path, dpi: int, previews: Sequence[int] = ()) -> list:
        """Rows = fields, columns = 1 km | 5 km; one colour bar per field."""
        height = 5.4 * len(fields) + 0.6
        fig, axes = plt.subplots(len(fields), 2, figsize=(10.8, height), squeeze=False)
        single = len(fields) == 1
        slots = []
        for i, field in enumerate(fields):
            for j, name in enumerate(GRIDS):
                title = f"Grilla {name} · {len(self.cells[name])} celdas"
                collection, annotation = self.panel(
                    axes[i, j], name, field, title if single else f"{field.label}\n{title}"
                )
                slots.append(Slot(collection, annotation, field, self.matrices[(name, field.column)]))
        fig.subplots_adjust(
            left=0.01, right=0.93, top=1 - 1.0 / height, bottom=0.05, hspace=0.12, wspace=0.03
        )
        for i, field in enumerate(fields):
            self.colorbar(fig, axes[i, :], field, orientation="vertical", fraction=0.03, pad=0.02, shrink=0.8)
        self.footer(fig)
        return self.animate(fig, slots, path, dpi, fields[0].label if single else "", previews)

    def overview(self, grid_name: str, path: Path, dpi: int, previews: Sequence[int] = ()) -> list:
        """All weekly fields of one grid as small multiples, each with its own colour bar."""
        ncols = 6
        nrows = -(-(len(WEEKLY_FIELDS) + 1) // ncols)  # +1 keeps a free panel for the note
        fig, axes = plt.subplots(nrows, ncols, figsize=(19, 5.0 * nrows), squeeze=False)
        fig.subplots_adjust(left=0.01, right=0.99, top=0.91, bottom=0.04, hspace=0.32, wspace=0.06)
        slots = []
        for ax, field in zip(axes.flat, WEEKLY_FIELDS, strict=False):
            collection, annotation = self.panel(ax, grid_name, field, field.label, fontsize=8)
            slots.append(Slot(collection, annotation, field, self.matrices[(grid_name, field.column)]))
            self.colorbar(fig, ax, field, orientation="horizontal", fraction=0.05, pad=0.02, shrink=0.9)
        for ax in axes.flat[len(WEEKLY_FIELDS) :]:
            ax.set_axis_off()
        axes.flat[-1].text(
            0.0,
            0.6,
            f"Grilla {grid_name} · {len(self.cells[grid_name])} celdas\n\n"
            "Sumas semanales: solo con 7/7 días.\nMedias: ≥ 4 días (NDVI/NDWI ≥ 1).\n"
            "Celda vacía = sin dato válido\n(nubes o sin pasada de\nSentinel-2 en NDVI/NDWI).",
            transform=axes.flat[-1].transAxes,
            fontsize=9,
            va="top",
            color="#333333",
        )
        self.footer(fig, y=0.008)
        heading = f"Todas las variables semanales · grilla {grid_name}"
        return self.animate(fig, slots, path, dpi, heading, previews, title_size=14)

    def static_map(self, path: Path, dpi: int) -> Path:
        """DEM covariates: rows = 1 km | 5 km, columns = fields."""
        fig, axes = plt.subplots(len(GRIDS), len(STATIC_FIELDS), figsize=(17, 10.6), squeeze=False)
        fig.subplots_adjust(left=0.01, right=0.99, top=0.9, bottom=0.1, hspace=0.15, wspace=0.05)
        for i, name in enumerate(GRIDS):
            frame = self.static[name].set_index("cell_id").reindex(self.cells[name]["cell_id"])
            for j, field in enumerate(STATIC_FIELDS):
                title = f"{field.label}\nGrilla {name} · {len(self.cells[name])} celdas"
                values = frame[field.column].to_numpy("float64")
                Slot(*self.panel(axes[i, j], name, field, title, fontsize=8), field, values[None, :]).show(0)
        for j, field in enumerate(STATIC_FIELDS):
            self.colorbar(
                fig, axes[:, j], field, orientation="horizontal", fraction=0.03, pad=0.02, shrink=0.9
            )
        fig.suptitle(
            "Covariables estáticas de relieve (Copernicus DEM GLO-90) sobre OpenStreetMap",
            fontsize=13,
            fontweight="bold",
        )
        fig.text(
            0.5,
            0.01,
            "Pendiente y orientación calculadas a 90 m y promediadas por celda; norte/este = cos/sen del "
            "aspecto de la ladera (0 = plano)",
            ha="center",
            fontsize=8,
            color="#555555",
        )
        fig.savefig(path, bbox_inches="tight", dpi=dpi)
        plt.close(fig)
        return path


def render(fig: Figure, dpi: int, tight: bool = False) -> Image.Image:
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=dpi, bbox_inches="tight" if tight else None)
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")


def quantize(frames: Sequence[Image.Image]) -> list[Image.Image]:
    """Map every frame onto one palette: adaptive colours of all frames plus the ramp anchors."""
    width, height = frames[0].size
    mosaic = Image.new("RGB", (width, height * len(frames)))
    for i, frame in enumerate(frames):
        mosaic.paste(frame, (0, height * i))
    adaptive = mosaic.quantize(colors=GIF_ADAPTIVE_COLORS, method=Image.Quantize.MEDIANCUT)
    anchors = [channel for color in RAMP_ANCHORS for channel in ImageColor.getrgb(color)]
    palette = Image.new("P", (1, 1))
    palette.putpalette(adaptive.getpalette()[: 3 * GIF_ADAPTIVE_COLORS] + anchors)
    return [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]


def save_gif(frames: Sequence[Image.Image], path: Path, duration_ms: int) -> None:
    first, *rest = quantize(frames)
    first.save(path, save_all=True, append_images=rest, duration=duration_ms, loop=0, optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--fps", type=float, default=1.5)
    parser.add_argument("--zoom", type=int, default=12)
    parser.add_argument(
        "--per-field", action="store_true", help="also write one GIF per weekly field (animations/variables/)"
    )
    args = parser.parse_args()

    maps = WeeklyMaps(args.data_root, args.output_dir, args.zoom, args.fps)
    animations = args.output_dir / "animations"
    best = maps.best_weeks()
    written = [
        *maps.comparison(
            [FIELDS["chirps_precip"], FIELDS["agera5_temp_mean"]],
            animations / "weekly_1km_vs_5km.gif",
            dpi=100,
            previews=[best["wettest"]],
        ),
        maps.static_map(args.output_dir / "maps" / "04_static_covariates_osm.png", dpi=100),
    ]
    for name in GRIDS:
        written += maps.overview(
            name, animations / f"all_variables_{name}.gif", dpi=70, previews=[best["clearest"]]
        )
    if args.per_field:
        (animations / "variables").mkdir(exist_ok=True)
        for field in WEEKLY_FIELDS:
            written += maps.comparison([field], animations / "variables" / f"{field.column}.gif", dpi=80)
    for path in written:
        print(f"{path}  ({path.stat().st_size / 1e6:.1f} MB)")
    print(f"{len(maps.weeks)} semanas, {len(WEEKLY_FIELDS)} campos semanales, {len(STATIC_FIELDS)} estáticos")


if __name__ == "__main__":
    main()
