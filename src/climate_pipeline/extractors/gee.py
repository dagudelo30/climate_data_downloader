"""Generic Google Earth Engine extractor for daily image collections.

A :class:`GeeProduct` declares one or more collections, band aliases (with scale factor
and valid range), an optional per-image QA mask and the indices to compute as Earth
Engine expressions. For each day every image is masked and turned into indices first;
the clear observations of all images/collections are then averaged, and the result is
downloaded as a single multi-band GeoTIFF covering the AOI bounding box.

Products:

* ``modis_terra_aqua_indices`` (default): MODIS Terra MOD09GA + Aqua MYD09GA surface
  reflectance, 500 m. Two overpasses per day roughly double the clear-sky observations in
  persistently cloudy regions such as the Colombian Andes. Columns ``gee_ndvi``/``gee_ndwi``.
* ``sentinel2_indices``: Sentinel-2 L2A (harmonized) masked with Cloud Score+, clear 20 m
  pixels averaged to 100 m, ~5-day revisit. Columns ``gee_s2_ndvi``/``gee_s2_ndwi``.

Indices: NDVI = (NIR − RED) / (NIR + RED); NDWI = (NIR − SWIR) / (NIR + SWIR) (Gao, 1996),
with MODIS b01/b02/b06 or Sentinel-2 B4/B8A/B11 (B8A matches the MODIS NIR band better
than B8 and shares the 20 m grid of B11).

MODIS cloud masks (``state_1km``), chosen with the ``cloud_mask`` option:

* ``standard`` (default): MOD35 cloud state clear, no cloud shadow, no internal cloud flag;
* ``strict``: additionally drops pixels flagged as adjacent to a cloud.

Measured over Cali (1 km cells, January-May 2024-2026), the pixels only flagged as
cloud-adjacent have a median NDVI of 0.57-0.62 (clear pixels: 0.67; cloudy: 0.07-0.12), so
they are kept by default: ``strict`` leaves 17-28 % of cell-weeks with a value against
46-69 % for ``standard``. Shadow/internal-cloud pixels are clearly contaminated (12-16 %
below NDVI 0.2) and are always removed. Reflectances outside the documented valid range
are removed before computing the indices.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import xarray as xr
from rasterio.io import MemoryFile

from climate_pipeline.domain.artifacts import MissingData, ProductStatus
from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.domain.variables import VariableSpec, normalized_index_spec
from climate_pipeline.extractors.base import (
    BaseExtractor,
    ExtractionFailure,
    ExtractionOutcome,
    PendingBronze,
    register_extractor,
)

logger = logging.getLogger(__name__)


class _ExtraSamplesWarningFilter(logging.Filter):
    """Earth Engine multi-band GeoTIFFs do not declare their ExtraSamples and libtiff warns on
    every read (two lines per day and product); the bands are read correctly, so drop the noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "ExtraSamples" not in record.getMessage()


logging.getLogger("rasterio._env").addFilter(_ExtraSamplesWarningFilter())

GEE_NODATA = -9999.0


@dataclass(frozen=True)
class GeeBand:
    band: str
    scale: float = 1.0
    valid_min: float | None = None
    valid_max: float | None = None


@dataclass(frozen=True)
class GeeIndex:
    name: str
    expression: str
    description: str


@dataclass(frozen=True)
class GeeProduct:
    name: str
    collections: tuple[str, ...]
    bands: Mapping[str, GeeBand]
    indices: tuple[GeeIndex, ...]
    scale_m: float
    # Per-image mask family; the variant comes from the ``cloud_mask`` option.
    mask: str | None = None
    # Extra per-image bands joined from another collection (e.g. Cloud Score+).
    link: tuple[str, tuple[str, ...]] | None = None
    # When set, clear pixels at this native resolution are averaged into each ``scale_m``
    # pixel, so a partly cloudy pixel still gets the mean of its clear part.
    native_scale_m: float | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


_MODIS_BANDS = {
    "BLUE": GeeBand("sur_refl_b03", 1e-4, -100, 16000),
    "GREEN": GeeBand("sur_refl_b04", 1e-4, -100, 16000),
    "RED": GeeBand("sur_refl_b01", 1e-4, -100, 16000),
    "NIR": GeeBand("sur_refl_b02", 1e-4, -100, 16000),
    "SWIR": GeeBand("sur_refl_b06", 1e-4, -100, 16000),
}
# S2_SR_HARMONIZED removes the 2022 processing-baseline offset: reflectance = DN * 1e-4.
_SENTINEL2_BANDS = {
    "BLUE": GeeBand("B2", 1e-4, 1, 10000),
    "GREEN": GeeBand("B3", 1e-4, 1, 10000),
    "RED": GeeBand("B4", 1e-4, 1, 10000),
    "REDEDGE": GeeBand("B5", 1e-4, 1, 10000),
    "NIR": GeeBand("B8A", 1e-4, 1, 10000),
    "SWIR": GeeBand("B11", 1e-4, 1, 10000),
}


def _normalized_indices(label: str, prefix: str = "", red_edge: bool = False) -> tuple[GeeIndex, ...]:
    """Vegetation, moisture and water indices shared by MODIS and Sentinel-2.

    EVI (Huete et al. 2002) saturates less than NDVI over dense canopy; MNDWI (Xu 2006)
    separates open water and wet soil (Aedes breeding sites) from built-up surfaces; NDRE and
    BSI need Sentinel-2's red-edge band and finer pixels.
    """
    indices = [
        GeeIndex(f"{prefix}ndvi", "(NIR - RED) / (NIR + RED)", f"{label} NDVI (cloud-masked)"),
        GeeIndex(f"{prefix}ndwi", "(NIR - SWIR) / (NIR + SWIR)", f"{label} NDWI, Gao 1996 (cloud-masked)"),
        GeeIndex(
            f"{prefix}evi",
            "2.5 * (NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1)",
            f"{label} EVI (cloud-masked)",
        ),
        GeeIndex(
            f"{prefix}mndwi", "(GREEN - SWIR) / (GREEN + SWIR)", f"{label} MNDWI, Xu 2006 (cloud-masked)"
        ),
    ]
    if red_edge:
        indices += [
            GeeIndex(
                f"{prefix}ndre", "(NIR - REDEDGE) / (NIR + REDEDGE)", f"{label} NDRE red-edge (cloud-masked)"
            ),
            GeeIndex(
                f"{prefix}bsi",
                "((SWIR + RED) - (NIR + BLUE)) / ((SWIR + RED) + (NIR + BLUE))",
                f"{label} bare soil index (cloud-masked)",
            ),
        ]
    return tuple(indices)


GEE_PRODUCTS: dict[str, GeeProduct] = {
    "modis_terra_aqua_indices": GeeProduct(
        name="modis_terra_aqua_indices",
        collections=("MODIS/061/MOD09GA", "MODIS/061/MYD09GA"),
        bands=_MODIS_BANDS,
        indices=_normalized_indices("MODIS Terra+Aqua (MOD09GA/MYD09GA)"),
        scale_m=500.0,
        mask="modis_state_1km",
    ),
    "modis_mod09ga_indices": GeeProduct(
        name="modis_mod09ga_indices",
        collections=("MODIS/061/MOD09GA",),
        bands=_MODIS_BANDS,
        indices=_normalized_indices("MODIS Terra (MOD09GA)"),
        scale_m=500.0,
        mask="modis_state_1km",
    ),
    "sentinel2_indices": GeeProduct(
        name="sentinel2_indices",
        collections=("COPERNICUS/S2_SR_HARMONIZED",),
        bands=_SENTINEL2_BANDS,
        indices=_normalized_indices("Sentinel-2 L2A + Cloud Score+ (100 m)", prefix="s2_", red_edge=True),
        scale_m=100.0,
        mask="cloud_score_plus",
        link=("GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED", ("cs_cdf",)),
        native_scale_m=20.0,
    ),
}


def gee_variable_specs(product_names: Sequence[str]) -> list[VariableSpec]:
    specs: list[VariableSpec] = []
    for product_name in product_names:
        product = resolve_product(product_name)
        specs.extend(normalized_index_spec("gee", index.name, index.description) for index in product.indices)
    columns = [spec.gold_column for spec in specs]
    if duplicated := sorted({column for column in columns if columns.count(column) > 1}):
        raise ConfigurationError(f"GEE products {list(product_names)} produce the same columns {duplicated}")
    return specs


def resolve_product(name: str) -> GeeProduct:
    try:
        return GEE_PRODUCTS[name]
    except KeyError as exc:
        raise ConfigurationError(f"unknown GEE product {name!r}; known: {sorted(GEE_PRODUCTS)}") from exc


def _modis_state_1km_mask(image: Any, drop_cloud_adjacent: bool) -> Any:
    state = image.select("state_1km")
    clear = state.bitwiseAnd(0b11).eq(0)  # bits 0-1: cloud state == clear
    no_shadow = state.bitwiseAnd(1 << 2).eq(0)  # bit 2: cloud shadow
    no_internal_cloud = state.bitwiseAnd(1 << 10).eq(0)  # bit 10: internal cloud flag
    mask = clear.And(no_shadow).And(no_internal_cloud)
    if drop_cloud_adjacent:
        mask = mask.And(state.bitwiseAnd(1 << 13).eq(0))  # bit 13: adjacent to cloud
    return mask


# Cloud Score+ ``cs_cdf``: 1 = clear, 0 = occluded; 0.6 is within the 0.5-0.65 range Google
# recommends for a balance between removing clouds/shadows and keeping clear pixels.
CLOUD_SCORE_PLUS_THRESHOLD = 0.6

# MASKS[family][variant] -> per-image boolean ee.Image (1 = keep).
MASKS: dict[str, dict[str, Callable[[Any], Any]]] = {
    "modis_state_1km": {
        "standard": lambda image: _modis_state_1km_mask(image, drop_cloud_adjacent=False),
        "strict": lambda image: _modis_state_1km_mask(image, drop_cloud_adjacent=True),
    },
    "cloud_score_plus": {
        "standard": lambda image: image.select("cs_cdf").gte(CLOUD_SCORE_PLUS_THRESHOLD),
        "strict": lambda image: image.select("cs_cdf").gte(0.8),  # also drops haze / thin cirrus
    },
}
CLOUD_MASK_VARIANTS = ("standard", "strict")


def resolve_mask(product: GeeProduct, variant: str) -> Callable[[Any], Any] | None:
    if variant not in CLOUD_MASK_VARIANTS:
        raise ConfigurationError(f"unknown cloud_mask {variant!r}; choose one of {CLOUD_MASK_VARIANTS}")
    return MASKS[product.mask][variant] if product.mask else None


_EE_LOCK = threading.Lock()
_EE_INITIALISED: dict[tuple[str | None, str | None], Any] = {}


def initialize_earth_engine(project: str | None, credentials_file: str | None) -> Any:
    """Initialise Earth Engine once per process and credentials (sources run in parallel threads)."""
    key = (project, credentials_file)
    with _EE_LOCK:
        if key not in _EE_INITIALISED:
            _EE_INITIALISED[key] = _initialize_earth_engine(project, credentials_file)
        return _EE_INITIALISED[key]


def _initialize_earth_engine(project: str | None, credentials_file: str | None) -> Any:
    """Initialise Earth Engine.

    ``credentials_file`` may be a service-account key (``type: service_account``) or any
    other Google credentials file, such as the external-account file produced by
    Workload Identity Federation in GitHub Actions. Without it, the credentials stored
    by ``earthengine authenticate`` are used.
    """
    import ee

    if credentials_file:
        import json
        from pathlib import Path

        key_path = Path(credentials_file)
        payload = json.loads(key_path.read_text())
        if payload.get("type") == "service_account":
            credentials = ee.ServiceAccountCredentials(payload["client_email"], str(key_path))
        else:
            import google.auth

            credentials, _ = google.auth.load_credentials_from_file(
                str(key_path),
                scopes=[
                    "https://www.googleapis.com/auth/earthengine",
                    "https://www.googleapis.com/auth/cloud-platform",
                ],
            )
        ee.Initialize(credentials=credentials, project=project)
    else:
        ee.Initialize(project=project)
    return ee


@register_extractor
class GeeExtractor(BaseExtractor):
    name = "gee"

    def __init__(self, *args: Any, ee_module: Any | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ee = ee_module

    @property
    def products(self) -> list[GeeProduct]:
        return [resolve_product(name) for name in self.config.options.get("products", [])]

    @property
    def cloud_mask(self) -> str:
        return str(self.config.options.get("cloud_mask", "standard"))

    def _earth_engine(self) -> Any:
        if self._ee is None:
            key = self.settings.gee_service_account_key
            self._ee = initialize_earth_engine(self.settings.gee_project, str(key) if key else None)
        return self._ee

    def fetch(self, requests: Mapping[str, Sequence[date]]) -> Iterator[ExtractionOutcome]:
        for product in self.products:  # fail fast on a bad cloud_mask, before any request
            resolve_mask(product, self.cloud_mask)
        ee = self._earth_engine()
        for product in self.products:
            names = [index.name for index in product.indices if index.name in requests]
            days = sorted({day for name in names for day in requests[name]})
            if not names:
                continue

            def worker(
                day: date, product: GeeProduct = product, names: list[str] = names
            ) -> list[ExtractionOutcome]:
                return self._fetch_day(ee, product, names, day)

            def on_error(day: date, exc: Exception, names: list[str] = names) -> list[ExtractionOutcome]:
                return [ExtractionFailure(self.name, name, day, repr(exc)) for name in names]

            yield from self.run_parallel(days, worker, on_error)

    def _fetch_day(
        self, ee: Any, product: GeeProduct, names: list[str], day: date
    ) -> list[ExtractionOutcome]:
        min_x, min_y, max_x, max_y = self.aoi.expanded_bounds(0.02)
        region = ee.Geometry.Rectangle([min_x, min_y, max_x, max_y], "EPSG:4326", False)
        mask = resolve_mask(product, self.cloud_mask)
        collection = ee.ImageCollection(product.collections[0])
        for extra in product.collections[1:]:
            collection = collection.merge(ee.ImageCollection(extra))
        collection = collection.filterDate(
            day.isoformat(), (day + timedelta(days=1)).isoformat()
        ).filterBounds(region)
        if collection.size().getInfo() == 0:
            return [MissingData(self.name, name, day, f"no image in {product.collections}") for name in names]
        if product.link:
            linked_collection, linked_bands = product.link
            collection = collection.linkCollection(ee.ImageCollection(linked_collection), list(linked_bands))

        indices = {index.name: index for index in product.indices}

        def to_indices(image: Any) -> Any:
            if mask is not None:
                image = image.updateMask(mask(image))
            aliases = {}
            for alias, band in product.bands.items():
                selected = image.select(band.band)
                if band.valid_min is not None:
                    selected = selected.updateMask(selected.gte(band.valid_min))
                if band.valid_max is not None:
                    selected = selected.updateMask(selected.lte(band.valid_max))
                aliases[alias] = selected.multiply(band.scale)
            return ee.Image.cat(
                [image.expression(indices[name].expression, aliases).rename(name) for name in names]
            ).toFloat()

        # Mean of the clear observations of the day (masked pixels do not contribute).
        stacked = collection.map(to_indices).mean()
        if product.native_scale_m:
            # Average the clear native pixels inside each output pixel instead of sampling one.
            native = ee.Projection("EPSG:4326").atScale(product.native_scale_m)
            ratio = round(product.scale_m / product.native_scale_m) + 1
            stacked = (
                stacked.setDefaultProjection(native)
                .reduceResolution(ee.Reducer.mean(), maxPixels=ratio * ratio)
                .reproject(ee.Projection("EPSG:4326").atScale(product.scale_m))
            )
        url = stacked.unmask(GEE_NODATA).getDownloadURL(
            {
                "region": region,
                "scale": product.scale_m,
                "crs": "EPSG:4326",
                "format": "GEO_TIFF",
                "bands": names,
            }
        )
        payload = self.http_get(url)
        arrays = geotiff_to_arrays(payload, names, nodata=GEE_NODATA)
        mask_label = f":cloud_mask={self.cloud_mask}" if mask is not None else ""
        origin = f"gee:{'+'.join(product.collections)}{mask_label}:{day.isoformat()}"
        return [
            PendingBronze(arrays[name], self.variables[name], day, ProductStatus.FINAL, origin)
            for name in names
        ]


def geotiff_to_arrays(payload: bytes, names: Sequence[str], nodata: float) -> dict[str, xr.DataArray]:
    """Split an in-memory multi-band GeoTIFF into named DataArrays."""
    with MemoryFile(payload) as memory, memory.open() as source:
        if source.count != len(names):
            raise ValueError(f"expected {len(names)} bands, got {source.count}")
        data = source.read().astype("float32")
        transform = source.transform
        crs = source.crs
    data[data == nodata] = np.nan
    x = transform.c + transform.a * (np.arange(data.shape[2]) + 0.5)
    y = transform.f + transform.e * (np.arange(data.shape[1]) + 0.5)
    arrays = {}
    for index, name in enumerate(names):
        array = xr.DataArray(data[index], dims=("y", "x"), coords={"y": y, "x": x}, name=name)
        array.attrs["units"] = "1"
        arrays[name] = array.rio.write_crs(crs).rio.write_transform(transform)
    return arrays
