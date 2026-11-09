"""Static covariates (orography) on a Gold grid.

Elevation comes from the Copernicus DEM GLO-90 (public Cloud-Optimized GeoTIFFs on AWS
Open Data, no credentials). Only the AOI window of each 1°×1° tile is read, at a
resolution adapted to the target grid (overviews are used for coarse grids).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from datetime import date
from typing import Any

import httpx
import numpy as np
import pandas as pd
import rasterio
import xarray as xr
from rasterio.enums import Resampling
from rasterio.merge import merge

from climate_pipeline.config import Settings
from climate_pipeline.domain.aoi import AreaOfInterest, Bounds
from climate_pipeline.domain.errors import ConfigurationError, DataNotAvailableError
from climate_pipeline.domain.grid import GridSpec, block_mean, nest
from climate_pipeline.domain.variables import ResamplingMethod
from climate_pipeline.processing.resample import RASTERIO_RESAMPLING
from climate_pipeline.processing.spatial import aoi_mask
from climate_pipeline.processing.terrain import terrain_derivatives
from climate_pipeline.storage.gold import GoldTable, OutputFormat, open_gold_store
from climate_pipeline.storage.layout import DataLake

logger = logging.getLogger(__name__)

COPERNICUS_DEM_URL = "https://copernicus-dem-90m.s3.amazonaws.com/{name}/{name}.tif"
DEM_NATIVE_RESOLUTION_DEG = 1.0 / 1200.0  # 3 arc-seconds
_METERS_PER_DEGREE = 111_320.0

STATIC_COLUMNS = {
    "elevation_m": "static_elevation_m",
    "slope_deg": "static_slope_deg",
    "northness": "static_northness",
    "eastness": "static_eastness",
    "tpi_500m": "static_tpi_500m",
    "tpi_2km": "static_tpi_2km",
    "tri_m": "static_tri_m",
}


def copernicus_tile_name(lat_floor: int, lon_floor: int) -> str:
    lat = f"{'N' if lat_floor >= 0 else 'S'}{abs(lat_floor):02d}_00"
    lon = f"{'E' if lon_floor >= 0 else 'W'}{abs(lon_floor):03d}_00"
    return f"Copernicus_DSM_COG_30_{lat}_{lon}_DEM"


def copernicus_tile_names(bounds: Bounds) -> list[str]:
    min_x, min_y, max_x, max_y = bounds
    return [
        copernicus_tile_name(lat, lon)
        for lat in range(math.floor(min_y), math.floor(max_y) + 1)
        for lon in range(math.floor(min_x), math.floor(max_x) + 1)
    ]


def fetch_copernicus_dem(
    bounds: Bounds, resolution_deg: float, http: httpx.Client | None = None
) -> xr.DataArray:
    client = http or httpx.Client(timeout=60.0, follow_redirects=True)
    urls = []
    for name in copernicus_tile_names(bounds):
        url = COPERNICUS_DEM_URL.format(name=name)
        if client.head(url).status_code == 200:
            urls.append(url)
        else:
            logger.info("DEM tile %s not available (ocean or outside coverage)", name)
    if not urls:
        raise DataNotAvailableError(f"no Copernicus DEM tiles for bounds {bounds}")

    resolution = max(resolution_deg, DEM_NATIVE_RESOLUTION_DEG)
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"):
        datasets = [rasterio.open(f"/vsicurl/{url}") for url in urls]
        try:
            mosaic, transform = merge(
                datasets, bounds=bounds, res=resolution, resampling=Resampling.average, nodata=np.nan
            )
        finally:
            for dataset in datasets:
                dataset.close()
    values = mosaic[0].astype("float32")
    x = transform.c + transform.a * (np.arange(values.shape[1]) + 0.5)
    y = transform.f + transform.e * (np.arange(values.shape[0]) + 0.5)
    dem = xr.DataArray(values, dims=("y", "x"), coords={"y": y, "x": x}, name="elevation")
    return dem.rio.write_crs("EPSG:4326").rio.write_transform(transform)


def terrain_to_grid(dem: xr.DataArray, grid: GridSpec) -> dict[str, np.ndarray]:
    """Elevation, slope, aspect components, TPI and roughness of ``dem`` area-averaged onto ``grid``."""
    terrain = terrain_derivatives(dem)
    layers = {}
    for name, column in STATIC_COLUMNS.items():
        layer = terrain[name].rio.write_nodata(np.nan, encoded=False)
        resampled = layer.rio.reproject(
            dst_crs=grid.crs,
            shape=grid.shape,
            transform=grid.affine,
            resampling=RASTERIO_RESAMPLING[ResamplingMethod.AVERAGE],
            nodata=np.nan,
        )
        layers[column] = resampled.values.astype("float32")
    return layers


def terrain_layers(grid: GridSpec) -> dict[str, np.ndarray]:
    """Elevation, slope and aspect components area-averaged onto the full ``grid``."""
    from climate_pipeline.pipeline.urban import grid_bounds_wgs84

    grid_resolution_deg = (
        grid.resolution[0] if grid.is_geographic else grid.resolution[0] / _METERS_PER_DEGREE
    )
    dem = fetch_copernicus_dem(grid_bounds_wgs84(grid, margin_cells=2), grid_resolution_deg / 10.0)
    return terrain_to_grid(dem, grid)


STATIC_LAYERS = ("terrain", "urban", "osm")


def compute_static_layers(
    grid: GridSpec,
    layers: Sequence[str],
    year: int,
    settings: Settings | None = None,
    ee: Any | None = None,
    osm_ways: list[dict[str, Any]] | None = None,
) -> dict[str, np.ndarray]:
    unknown = sorted(set(layers) - set(STATIC_LAYERS))
    if unknown:
        raise ConfigurationError(f"unknown static layers {unknown}; choose among {STATIC_LAYERS}")
    result: dict[str, np.ndarray] = {}
    if "terrain" in layers:
        result.update(terrain_layers(grid))
    if "urban" in layers:
        from climate_pipeline.extractors.gee import initialize_earth_engine
        from climate_pipeline.pipeline.urban import fetch_gee_urban_layers

        if ee is None:
            settings = settings or Settings()
            key = settings.gee_service_account_key
            ee = initialize_earth_engine(settings.gee_project, str(key) if key else None)
        result.update(fetch_gee_urban_layers(ee, grid, year))
    if "osm" in layers:
        from climate_pipeline.pipeline.urban import fetch_osm_layers, osm_layers

        result.update(osm_layers(osm_ways, grid) if osm_ways is not None else fetch_osm_layers(grid))
    return result


def _static_frame(grid: GridSpec, aoi: AreaOfInterest, layers: dict[str, np.ndarray]) -> pd.DataFrame:
    mask = aoi_mask(grid.template(), aoi, all_touched=True).reshape(-1)
    cell_ids = np.flatnonzero(mask)
    lon, lat = grid.lonlat()
    frame = pd.DataFrame(
        {
            "cell_id": cell_ids.astype("int64"),
            "latitude": lat.reshape(-1)[cell_ids],
            "longitude": lon.reshape(-1)[cell_ids],
        }
    )
    for column in sorted(layers):
        frame[column] = layers[column].reshape(-1)[cell_ids].astype("float32")
    return frame


def _merge_existing(store: Any, frame: pd.DataFrame) -> pd.DataFrame:
    """Keep the static columns that this run did not recompute (e.g. terrain when adding OSM)."""
    try:
        existing = store.query(f"SELECT * FROM {GoldTable.STATIC}")
    except Exception:
        return frame
    keep = [column for column in existing.columns if column not in frame.columns]
    if not keep or existing.empty:
        return frame
    return frame.merge(existing[["cell_id", *keep]], on="cell_id", how="left")


def build_static_covariates(
    lake: DataLake,
    grid_name: str | Sequence[str],
    aoi: AreaOfInterest,
    output_format: OutputFormat,
    layers: Sequence[str] = ("terrain",),
    year: int | None = None,
    settings: Settings | None = None,
    ee: Any | None = None,
    nested: bool = False,
) -> dict[str, pd.DataFrame]:
    """Static covariates on one or several Gold grids.

    * ``nested=False`` (default): every grid is computed directly from the native layers
      (DEM at 90 m, Earth Engine composites, OSM vectors) at its own resolution.
    * ``nested=True``: computed once on the finest grid and block-averaged onto the others
      (grids built with ``run --nested``), so a 1 km value is exactly the mean of its 100 m cells.
    """
    names = [grid_name] if isinstance(grid_name, str) else list(grid_name)
    grids = {}
    for name in names:
        layout = lake.gold_layout(name)
        if not layout.grid_path.exists() and OutputFormat(output_format) is OutputFormat.POSTGRES:
            open_gold_store(layout, output_format, settings).close()  # grid.json from the database
        if not layout.grid_path.exists():
            raise DataNotAvailableError(f"gold/{name} has no grid.json yet: build Gold first")
        grids[name] = GridSpec.load(layout.grid_path)
    ordered = sorted(
        names,
        key=lambda name: grids[name].resolution[0] * (_METERS_PER_DEGREE if grids[name].is_geographic else 1),
    )
    year = year or date.today().year - 1

    per_grid: dict[str, dict[str, np.ndarray]] = {}
    if nested:
        template = grids[ordered[0]]
        nestings = {name: nest(template, grids[name]) for name in ordered[1:]}
        fine_layers = compute_static_layers(template, layers, year, settings=settings, ee=ee)
        per_grid[ordered[0]] = fine_layers
        for name in ordered[1:]:
            per_grid[name] = {
                column: block_mean(values, nestings[name], grids[name].shape)
                for column, values in fine_layers.items()
            }
    else:
        osm_ways = None
        if "osm" in layers:
            from climate_pipeline.pipeline.urban import fetch_osm_roads, grid_bounds_wgs84

            # One Overpass query covering every grid, reused for each resolution.
            bounds = [grid_bounds_wgs84(grid) for grid in grids.values()]
            union = (
                min(b[0] for b in bounds),
                min(b[1] for b in bounds),
                max(b[2] for b in bounds),
                max(b[3] for b in bounds),
            )
            osm_ways = fetch_osm_roads(union)
        if "urban" in layers and ee is None:
            from climate_pipeline.extractors.gee import initialize_earth_engine

            settings = settings or Settings()
            key = settings.gee_service_account_key
            ee = initialize_earth_engine(settings.gee_project, str(key) if key else None)
        for name in ordered:
            logger.info("static: %s computed from the native layers", name)
            per_grid[name] = compute_static_layers(
                grids[name], layers, year, settings=settings, ee=ee, osm_ways=osm_ways
            )

    frames = {}
    for name in ordered:
        frame = _static_frame(grids[name], aoi, per_grid[name])
        store = open_gold_store(lake.gold_layout(name), output_format, settings)
        try:
            frame = _merge_existing(store, frame)
            store.write_static(frame)
        finally:
            store.close()
        frames[name] = frame
    return frames
