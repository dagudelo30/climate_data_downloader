"""Urban-form covariates (static, one value per cell) for a Gold grid.

Two families, both computed on a target grid and stored in ``static_covariates``:

**Earth Engine** (``urban_*``), annual composites of ``year``:

=================================  ==================================================
``urban_s2_ndbi`` / ``_ndvi`` /    Sentinel-2 L2A + Cloud Score+ annual median:
``urban_s2_mndwi``                 NDBI (B11−B8)/(B11+B8), NDVI, MNDWI (B3−B11)/(B3+B11)
``urban_landsat_lst_median``       Landsat 8/9 annual median surface temperature (°C):
                                   urban heat islands
``urban_dw_built`` / ``_trees`` /  Dynamic World mean class probability of the year
``urban_dw_water``
``urban_worldcover_built_frac`` /  ESA WorldCover 2021 built-up / tree fraction of the cell
``urban_worldcover_tree_frac``
``urban_ghsl_built_frac``          GHSL built-up surface fraction (last observed epoch ≤ year)
``urban_ghsl_built_height_m``      GHSL average building height (2018)
``urban_buildings_frac`` /        Google Open Buildings 2.5D Temporal (≤ 2023): built-up
``urban_buildings_height_m``       fraction and mean building height
``urban_viirs_ntl_median``         VIIRS monthly night lights, median of the year
=================================  ==================================================

**OpenStreetMap** via the Overpass API (no extra dependency): road density (km/km²) of
all drivable roads and of major roads, and intersection density (junctions of ≥ 3
road segments per km²).

Earth Engine layers are downloaded in EPSG:4326 at a work scale finer than the grid (EE
cannot parse every national CRS, e.g. EPSG:9377) and area-averaged onto the grid locally,
exactly like Silver → Gold. OSM lengths are measured in a metric CRS.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np
import shapely
from pyproj import CRS, Transformer

from climate_pipeline.domain.errors import DataNotAvailableError
from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.domain.variables import ResamplingMethod
from climate_pipeline.extractors.gee import GEE_NODATA, geotiff_to_arrays
from climate_pipeline.processing.resample import resample_to_grid

logger = logging.getLogger(__name__)

METERS_PER_DEGREE = 111_320.0
CLOUD_SCORE_PLUS_THRESHOLD = 0.6


def grid_bounds_wgs84(grid: GridSpec, margin_cells: float = 1.0) -> tuple[float, float, float, float]:
    """WGS84 bounds of the grid extent plus ``margin_cells`` cells."""
    left, bottom, right, top = grid.bounds
    dx, dy = grid.resolution
    left, right, bottom, top = (
        left - margin_cells * dx,
        right + margin_cells * dx,
        bottom - margin_cells * dy,
        top + margin_cells * dy,
    )
    if grid.is_geographic:
        return left, bottom, right, top
    transformer = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    xs, ys = transformer.transform([left, left, right, right], [bottom, top, bottom, top])
    return min(xs), min(ys), max(xs), max(ys)


def grid_resolution_m(grid: GridSpec) -> float:
    return grid.resolution[0] * (METERS_PER_DEGREE if grid.is_geographic else 1.0)


# --- Earth Engine layers ------------------------------------------------------------------
@dataclass(frozen=True)
class UrbanLayer:
    columns: tuple[str, ...]
    build: Callable[[Any, Any, int, float], Any]  # (ee, region, year, work_scale_m) -> ee.Image
    description: str


def _sentinel2_indices(ee: Any, region: Any, year: int, _: float) -> Any:
    collection = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(region)
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .linkCollection(ee.ImageCollection("GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"), ["cs_cdf"])
        .map(lambda image: image.updateMask(image.select("cs_cdf").gte(CLOUD_SCORE_PLUS_THRESHOLD)))
    )
    median = collection.select(["B3", "B4", "B8", "B11"]).median().multiply(1e-4)
    green, red, nir, swir = (median.select(name) for name in ("B3", "B4", "B8", "B11"))
    return ee.Image.cat(
        [
            swir.subtract(nir).divide(swir.add(nir)).rename("urban_s2_ndbi"),
            nir.subtract(red).divide(nir.add(red)).rename("urban_s2_ndvi"),
            green.subtract(swir).divide(green.add(swir)).rename("urban_s2_mndwi"),
        ]
    )


def _landsat_lst(ee: Any, region: Any, year: int, _: float) -> Any:
    def clear(image: Any) -> Any:
        qa = image.select("QA_PIXEL")
        mask = qa.bitwiseAnd(0b11111).eq(0)  # fill, dilated cloud, cirrus, cloud, shadow
        return image.updateMask(mask)

    collection = (
        ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
        .merge(ee.ImageCollection("LANDSAT/LC09/C02/T1_L2"))
        .filterBounds(region)
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .map(clear)
    )
    lst = collection.select("ST_B10").median().multiply(0.00341802).add(149.0 - 273.15)
    return lst.rename("urban_landsat_lst_median")


def _dynamic_world(ee: Any, region: Any, year: int, _: float) -> Any:
    mean = (
        ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
        .filterBounds(region)
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .select(["built", "trees", "water"])
        .mean()
    )
    return mean.rename(["urban_dw_built", "urban_dw_trees", "urban_dw_water"])


def _fraction(ee: Any, image: Any, native_scale_m: float, work_scale_m: float) -> Any:
    """Exact cover fraction at ``work_scale_m`` from a fine binary image (reduceResolution)."""
    ratio = math.ceil(work_scale_m / native_scale_m) + 1
    return (
        image.toFloat()
        .reduceResolution(ee.Reducer.mean(), maxPixels=max(ratio * ratio, 4))
        .reproject(ee.Projection("EPSG:4326").atScale(work_scale_m))
    )


def _worldcover(ee: Any, region: Any, year: int, work_scale_m: float) -> Any:
    classes = ee.ImageCollection("ESA/WorldCover/v200").first().select("Map")
    return ee.Image.cat(
        [
            _fraction(ee, classes.eq(50), 10.0, work_scale_m).rename("urban_worldcover_built_frac"),
            _fraction(ee, classes.eq(10), 10.0, work_scale_m).rename("urban_worldcover_tree_frac"),
        ]
    )


def ghsl_epoch(year: int) -> int:
    """Last GHSL P2023A epoch observed by satellites (2025/2030 are projections)."""
    return min(2020, 5 * (year // 5))


def _ghsl(ee: Any, region: Any, year: int, _: float) -> Any:
    built = ee.Image(f"JRC/GHSL/P2023A/GHS_BUILT_S/{ghsl_epoch(year)}").select("built_surface")
    height = ee.Image("JRC/GHSL/P2023A/GHS_BUILT_H/2018").select("built_height")
    # built_surface: m² of built-up surface in each 100 m × 100 m cell.
    return ee.Image.cat(
        [built.divide(10_000).rename("urban_ghsl_built_frac"), height.rename("urban_ghsl_built_height_m")]
    )


OPEN_BUILDINGS_LAST_YEAR = 2023


def _open_buildings(ee: Any, region: Any, year: int, work_scale_m: float) -> Any:
    """Google Open Buildings 2.5D Temporal (yearly rasters, ~4 m effective resolution):
    built-up fraction (presence ≥ 0.5) and mean building height over built pixels."""
    epoch = min(year, OPEN_BUILDINGS_LAST_YEAR)
    mosaic = (
        ee.ImageCollection("GOOGLE/Research/open-buildings-temporal/v1")
        .filterBounds(region)
        .filterDate(f"{epoch}-01-01", f"{epoch + 1}-01-01")
        .mosaic()
        .reproject(ee.Projection("EPSG:4326").atScale(4.0))
    )
    presence = mosaic.select("building_presence").gte(0.5)
    height = mosaic.select("building_height").updateMask(presence)
    return ee.Image.cat(
        [
            _fraction(ee, presence, 4.0, work_scale_m).rename("urban_buildings_frac"),
            _fraction(ee, height, 4.0, work_scale_m).rename("urban_buildings_height_m"),
        ]
    )


def _night_lights(ee: Any, region: Any, year: int, _: float) -> Any:
    return (
        ee.ImageCollection("NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG")
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .select("avg_rad")
        .median()
        .rename("urban_viirs_ntl_median")
    )


URBAN_GEE_LAYERS: tuple[UrbanLayer, ...] = (
    UrbanLayer(
        ("urban_s2_ndbi", "urban_s2_ndvi", "urban_s2_mndwi"),
        _sentinel2_indices,
        "Sentinel-2 annual median indices",
    ),
    UrbanLayer(("urban_landsat_lst_median",), _landsat_lst, "Landsat 8/9 annual median LST"),
    UrbanLayer(
        ("urban_dw_built", "urban_dw_trees", "urban_dw_water"),
        _dynamic_world,
        "Dynamic World mean probabilities",
    ),
    UrbanLayer(
        ("urban_worldcover_built_frac", "urban_worldcover_tree_frac"),
        _worldcover,
        "ESA WorldCover 2021 fractions",
    ),
    UrbanLayer(
        ("urban_ghsl_built_frac", "urban_ghsl_built_height_m"), _ghsl, "GHSL built surface and height"
    ),
    UrbanLayer(
        ("urban_buildings_frac", "urban_buildings_height_m"), _open_buildings, "Open Buildings 2.5D Temporal"
    ),
    UrbanLayer(("urban_viirs_ntl_median",), _night_lights, "VIIRS annual median night lights"),
)


def work_scale_for(grid: GridSpec) -> float:
    """Download scale: ~4 samples per grid cell side, between 10 and 100 m."""
    return float(min(max(grid_resolution_m(grid) / 4.0, 10.0), 100.0))


def fetch_gee_urban_layers(
    ee: Any, grid: GridSpec, year: int, http: httpx.Client | None = None
) -> dict[str, np.ndarray]:
    """Every Earth Engine urban layer area-averaged onto ``grid`` (full-grid 2-D arrays)."""
    client = http or httpx.Client(timeout=300.0, follow_redirects=True)
    min_x, min_y, max_x, max_y = grid_bounds_wgs84(grid)
    region = ee.Geometry.Rectangle([min_x, min_y, max_x, max_y], "EPSG:4326", False)
    scale = work_scale_for(grid)
    layers: dict[str, np.ndarray] = {}
    for layer in URBAN_GEE_LAYERS:
        image = layer.build(ee, region, year, scale).toFloat()
        url = image.unmask(GEE_NODATA).getDownloadURL(
            {
                "region": region,
                "scale": scale,
                "crs": "EPSG:4326",
                "format": "GEO_TIFF",
                "bands": list(layer.columns),
            }
        )
        logger.info("urban: %s (%s, %.0f m)", layer.description, year, scale)
        response = client.get(url)
        response.raise_for_status()
        arrays = geotiff_to_arrays(response.content, list(layer.columns), nodata=GEE_NODATA)
        for column, array in arrays.items():
            layers[column] = resample_to_grid(array, grid, ResamplingMethod.AVERAGE).values.astype("float32")
    return layers


# --- OpenStreetMap ------------------------------------------------------------------------
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
MAJOR_ROADS = {"motorway", "trunk", "primary", "secondary"}
DRIVABLE_ROADS = MAJOR_ROADS | {"tertiary", "unclassified", "residential", "living_street"}
OSM_COLUMNS = (
    "urban_osm_road_density_km_km2",
    "urban_osm_major_road_density_km_km2",
    "urban_osm_intersection_density_km2",
)


def _road_class(tags: dict[str, str]) -> str | None:
    highway = tags.get("highway", "")
    base = highway.removesuffix("_link")
    return base if base in DRIVABLE_ROADS else None


def fetch_osm_roads(
    bounds: tuple[float, float, float, float], http: httpx.Client | None = None
) -> list[dict[str, Any]]:
    """Drivable OSM ways (with node ids and geometry) inside WGS84 ``bounds``."""
    min_x, min_y, max_x, max_y = bounds
    pattern = "|".join(sorted(DRIVABLE_ROADS))
    query = (
        f'[out:json][timeout:300];way["highway"~"^({pattern})(_link)?$"]'
        f"({min_y},{min_x},{max_y},{max_x});out body geom;"
    )
    client = http or httpx.Client(timeout=360.0, headers={"User-Agent": "climate-pipeline/0.1"})
    last_error: Exception | None = None
    for attempt, endpoint in enumerate(OVERPASS_ENDPOINTS * 2):
        try:
            response = client.post(endpoint, data={"data": query})
            if response.status_code in (429, 502, 503, 504):
                raise httpx.HTTPStatusError("overpass busy", request=response.request, response=response)
            response.raise_for_status()
            return [element for element in response.json()["elements"] if element.get("type") == "way"]
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            logger.warning("overpass %s failed (%s), retrying", endpoint, exc)
            time.sleep(5 * (attempt + 1))
    raise DataNotAvailableError(f"Overpass API unavailable: {last_error!r}")


def _metric_crs(grid: GridSpec, bounds: tuple[float, float, float, float]) -> str:
    if not grid.is_geographic:
        return grid.crs
    lon = (bounds[0] + bounds[2]) / 2
    lat = (bounds[1] + bounds[3]) / 2
    zone = int((lon + 180) // 6) + 1
    return CRS.from_dict({"proj": "utm", "zone": zone, "south": lat < 0}).to_string()


def osm_layers(ways: list[dict[str, Any]], grid: GridSpec) -> dict[str, np.ndarray]:
    """Road and intersection densities per grid cell from Overpass ways.

    Lines are split into pieces shorter than a fifth of a cell and every piece is assigned
    to the cell of its midpoint, so lengths are exact up to a few metres per cell.
    """
    to_grid = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
    # Lengths are measured in metres: the grid CRS when projected, else a local UTM zone.
    to_metric = (
        Transformer.from_crs(grid.crs, _metric_crs(grid, grid_bounds_wgs84(grid)), always_xy=True)
        if grid.is_geographic
        else None
    )
    height, width = grid.shape
    a, _, c, _, e, f = grid.transform

    totals = {"all": np.zeros(height * width), "major": np.zeros(height * width)}
    degree: dict[int, int] = {}
    node_xy: dict[int, tuple[float, float]] = {}
    max_piece = abs(a) / 5.0
    for way in ways:
        road = _road_class(way.get("tags", {}))
        geometry = way.get("geometry") or []
        nodes = way.get("nodes") or []
        if road is None or len(geometry) < 2:
            continue
        lon = np.array([point["lon"] for point in geometry])
        lat = np.array([point["lat"] for point in geometry])
        gx, gy = to_grid.transform(lon, lat)
        coords = shapely.get_coordinates(
            shapely.segmentize(shapely.linestrings(np.column_stack([gx, gy])), max_piece)
        )
        if to_metric is None:
            mx, my = coords[:, 0], coords[:, 1]
        else:
            mx, my = to_metric.transform(coords[:, 0], coords[:, 1])
        lengths = np.hypot(np.diff(mx), np.diff(my))
        mid_x = (coords[1:, 0] + coords[:-1, 0]) / 2
        mid_y = (coords[1:, 1] + coords[:-1, 1]) / 2
        cols = np.floor((mid_x - c) / a).astype(int)
        rows = np.floor((mid_y - f) / e).astype(int)
        inside = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)
        index = rows[inside] * width + cols[inside]
        np.add.at(totals["all"], index, lengths[inside] / 1000.0)
        if road in MAJOR_ROADS:
            np.add.at(totals["major"], index, lengths[inside] / 1000.0)
        if len(nodes) == len(geometry):
            for position, node in enumerate(nodes):
                end = position in (0, len(nodes) - 1)
                degree[node] = degree.get(node, 0) + (1 if end else 2)
                node_xy[node] = (gx[position], gy[position])

    intersections = np.zeros(height * width)
    junctions = [node_xy[node] for node, value in degree.items() if value >= 3]
    if junctions:
        jx, jy = np.array(junctions).T
        cols = np.floor((jx - c) / a).astype(int)
        rows = np.floor((jy - f) / e).astype(int)
        inside = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)
        np.add.at(intersections, rows[inside] * width + cols[inside], 1.0)

    if grid.is_geographic:  # cell area shrinks with latitude
        lat_centres = grid.y_coords()
        row_area = (
            (abs(a) * METERS_PER_DEGREE)
            * (abs(e) * METERS_PER_DEGREE)
            * np.cos(np.deg2rad(lat_centres))
            / 1e6
        )
        area = np.repeat(row_area, width)
    else:
        area = np.full(height * width, grid_resolution_m(grid) ** 2 / 1e6)
    return {
        OSM_COLUMNS[0]: (totals["all"] / area).reshape(grid.shape).astype("float32"),
        OSM_COLUMNS[1]: (totals["major"] / area).reshape(grid.shape).astype("float32"),
        OSM_COLUMNS[2]: (intersections / area).reshape(grid.shape).astype("float32"),
    }


def fetch_osm_layers(grid: GridSpec, http: httpx.Client | None = None) -> dict[str, np.ndarray]:
    ways = fetch_osm_roads(grid_bounds_wgs84(grid), http)
    logger.info("osm: %d drivable ways", len(ways))
    return osm_layers(ways, grid)
