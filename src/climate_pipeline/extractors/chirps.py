"""CHIRPS daily precipitation (CHC/UCSB).

Only the AOI window is read: the global GeoTIFFs are opened through GDAL's
``/vsicurl/`` (and ``/vsigzip/`` for v2.0 ``.tif.gz``) and a windowed read downloads
just the needed strips/blocks, ~1 s per day instead of a ~30 MB file.

Product maturity:

* **final**: v3.0 ``final/{rnl|sat}`` (v2.0 ``global_daily``), published for month M
  around the third week of M+1;
* **prelim**: v3.0 ``prelim/sat`` (v2.0 ``prelim/global_daily``), a few days after the
  observation date.

The final product is always tried first; prelim dates are revisited by the latency
planner until the final version replaces them.

Politeness and fallback: the CHC server answers HTTP 403 to clients that open too many
connections, so requests are spaced by ``min_request_interval_s`` across threads and
transient errors are retried with exponential back-off. ``mirror: gee`` reads CHIRPS
v2.0 final (``UCSB-CHG/CHIRPS/DAILY``) from Google Earth Engine on its native 0.05°
grid instead; v3.0 is not available there, so ``mirror: gee`` requires ``version: v2``
(mixing versions in one series is refused).
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import numpy as np
import rasterio
import xarray as xr
from pydantic import BaseModel, model_validator
from rasterio.errors import RasterioIOError
from rasterio.windows import Window
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from climate_pipeline.domain.artifacts import MissingData, ProductStatus
from climate_pipeline.domain.errors import EmptySelectionError
from climate_pipeline.extractors.base import (
    BaseExtractor,
    ExtractionFailure,
    ExtractionOutcome,
    PendingBronze,
    RequestThrottle,
    register_extractor,
)
from climate_pipeline.extractors.gee import geotiff_to_arrays, initialize_earth_engine

_TEMPLATES = {
    "v3": {
        "final": (
            "{base_url}/CHIRPS/v3.0/daily/final/{disaggregation}/{year}/"
            "chirps-v3.0.{disaggregation}.{year}.{month:02d}.{day:02d}.tif"
        ),
        "prelim": (
            "{base_url}/CHIRPS/v3.0/daily/prelim/sat/{year}/"
            "chirps-v3.0.prelim.{year}.{month:02d}.{day:02d}.tif"
        ),
    },
    "v2": {
        "final": (
            "{base_url}/CHIRPS-2.0/global_daily/tifs/p05/{year}/"
            "chirps-v2.0.{year}.{month:02d}.{day:02d}.tif.gz"
        ),
        "prelim": (
            "{base_url}/CHIRPS-2.0/prelim/global_daily/tifs/p05/{year}/"
            "chirps-v2.0.{year}.{month:02d}.{day:02d}.tif.gz"
        ),
    },
}

_GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.gz",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "GDAL_HTTP_TIMEOUT": "60",
    # Never cache partial responses: a rate-limited (403) answer would poison later reads.
    "CPL_VSIL_CURL_NON_CACHED": "/vsicurl/https://data.chc.ucsb.edu",
    # Header + IFD in as few round trips as possible.
    "GDAL_INGESTED_BYTES_AT_OPEN": "65536",
    "GDAL_HTTP_USERAGENT": "climate-pipeline/0.1 (+https://github.com/Felipep17/climate-pipeline)",
}

GEE_CHIRPS_COLLECTION = "UCSB-CHG/CHIRPS/DAILY"
CHIRPS_RESOLUTION = 0.05
CHIRPS_ORIGIN = (-180.0, 50.0)  # GEE native transform of UCSB-CHG/CHIRPS/DAILY

_THROTTLES: dict[float, RequestThrottle] = {}


def _throttle(min_interval_s: float) -> RequestThrottle:
    """One shared throttle per interval: every extractor instance respects the same pace."""
    return _THROTTLES.setdefault(min_interval_s, RequestThrottle(min_interval_s))


_TRANSIENT_GDAL_HINTS = (
    "403",
    "429",
    "500",
    "502",
    "503",
    "504",
    "timed out",
    "tiffreaddirectory",
    "not recognized as being in a supported file format",
    "curl error",
)


def _is_transient_gdal_error(exc: BaseException) -> bool:
    return isinstance(exc, RasterioIOError) and any(
        hint in str(exc).lower() for hint in _TRANSIENT_GDAL_HINTS
    )


class ChirpsOptions(BaseModel):
    version: Literal["v2", "v3"] = "v3"
    disaggregation: Literal["rnl", "sat"] = "rnl"
    base_url: str = "https://data.chc.ucsb.edu/products"
    final_template: str | None = None
    prelim_template: str | None = None
    use_prelim: bool = True
    nodata: float = -9999.0
    margin_cells: int = 2
    mirror: Literal["chc", "gee"] = "chc"
    min_request_interval_s: float = 1.0

    @model_validator(mode="after")
    def _gee_mirror_is_v2(self) -> ChirpsOptions:
        if self.mirror == "gee" and self.version != "v2":
            raise ValueError("mirror 'gee' only serves CHIRPS v2.0: set version: v2")
        return self

    def templates(self) -> list[tuple[ProductStatus, str]]:
        defaults = _TEMPLATES[self.version]
        templates = [(ProductStatus.FINAL, self.final_template or defaults["final"])]
        if self.use_prelim:
            templates.append((ProductStatus.PRELIM, self.prelim_template or defaults["prelim"]))
        return templates


def gdal_path(location: str) -> str:
    """GDAL virtual path for a URL or local file (``.gz`` handled transparently)."""
    path = f"/vsicurl/{location}" if location.startswith(("http://", "https://")) else location
    return f"/vsigzip/{path}" if location.endswith(".gz") else path


@retry(
    retry=retry_if_exception(_is_transient_gdal_error),
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    reraise=True,
)
def read_window(
    location: str, bounds: tuple[float, float, float, float], margin_cells: int, nodata: float
) -> xr.DataArray:
    """Read the cells covering ``bounds`` (+margin) from a north-up single-band raster."""
    with rasterio.Env(**_GDAL_ENV), rasterio.open(gdal_path(location)) as source:
        transform = source.transform
        min_x, min_y, max_x, max_y = bounds
        col_start = math.floor((min_x - transform.c) / transform.a) - margin_cells
        col_stop = math.ceil((max_x - transform.c) / transform.a) + margin_cells
        row_start = math.floor((max_y - transform.f) / transform.e) - margin_cells
        row_stop = math.ceil((min_y - transform.f) / transform.e) + margin_cells
        col_start, row_start = max(col_start, 0), max(row_start, 0)
        col_stop, row_stop = min(col_stop, source.width), min(row_stop, source.height)
        if col_stop <= col_start or row_stop <= row_start:
            raise EmptySelectionError(f"AOI outside raster extent of {location}")
        window = Window(col_start, row_start, col_stop - col_start, row_stop - row_start)
        values = source.read(1, window=window).astype("float32")
        window_transform = source.window_transform(window)
        file_nodata = source.nodata if source.nodata is not None else nodata
        crs = source.crs or "EPSG:4326"

    values[values == file_nodata] = np.nan
    values[values < -1.0] = np.nan  # any other negative sentinel
    x = window_transform.c + window_transform.a * (np.arange(values.shape[1]) + 0.5)
    y = window_transform.f + window_transform.e * (np.arange(values.shape[0]) + 0.5)
    array = xr.DataArray(values, dims=("y", "x"), coords={"y": y, "x": x}, name="precip")
    array.attrs["units"] = "mm d-1"
    return array.rio.write_crs(crs).rio.write_transform(window_transform)


@register_extractor
class ChirpsExtractor(BaseExtractor):
    name = "chirps"
    single_variable = True

    @property
    def options(self) -> ChirpsOptions:
        return ChirpsOptions.model_validate(self.config.options)

    def _exists(self, location: str) -> bool:
        if location.startswith(("http://", "https://")):
            return self.http_exists(location)
        return Path(location).exists()

    def fetch(self, requests: Mapping[str, Sequence[date]]) -> Iterator[ExtractionOutcome]:
        options = self.options
        spec = next(iter(self.variables.values()))
        days = sorted({day for dates in requests.values() for day in dates})
        throttle = _throttle(options.min_request_interval_s)

        def from_chc(day: date) -> list[ExtractionOutcome]:
            for status, template in options.templates():
                location = template.format(
                    base_url=options.base_url,
                    disaggregation=options.disaggregation,
                    year=day.year,
                    month=day.month,
                    day=day.day,
                )
                remote = location.startswith(("http://", "https://"))
                if remote:
                    throttle.wait()
                if not self._exists(location):
                    continue
                if remote:
                    throttle.wait()
                array = read_window(location, self.aoi.bounds, options.margin_cells, options.nodata)
                return [PendingBronze(array, spec, day, status, location)]
            return [MissingData(self.name, spec.name, day, "not published (final nor prelim)")]

        def on_error(day: date, exc: Exception) -> list[ExtractionOutcome]:
            return [ExtractionFailure(self.name, spec.name, day, repr(exc))]

        worker = from_chc
        if options.mirror == "gee":
            ee = self._earth_engine()

            def worker(day: date) -> list[ExtractionOutcome]:
                return self._from_gee(ee, day, spec, options)

        yield from self.run_parallel(days, worker, on_error)

    def _earth_engine(self) -> Any:
        key = self.settings.gee_service_account_key
        return initialize_earth_engine(self.settings.gee_project, str(key) if key else None)

    def _from_gee(self, ee: Any, day: date, spec: Any, options: ChirpsOptions) -> list[ExtractionOutcome]:
        """Download the AOI window of CHIRPS v2.0 from Earth Engine on the native grid."""
        collection = ee.ImageCollection(GEE_CHIRPS_COLLECTION).filterDate(
            day.isoformat(), (day + timedelta(days=1)).isoformat()
        )
        if collection.size().getInfo() == 0:
            return [MissingData(self.name, spec.name, day, f"no {GEE_CHIRPS_COLLECTION} image")]
        x0, y0 = CHIRPS_ORIGIN
        min_x, min_y, max_x, max_y = self.aoi.bounds
        col_start = math.floor((min_x - x0) / CHIRPS_RESOLUTION) - options.margin_cells
        col_stop = math.ceil((max_x - x0) / CHIRPS_RESOLUTION) + options.margin_cells
        row_start = math.floor((y0 - max_y) / CHIRPS_RESOLUTION) - options.margin_cells
        row_stop = math.ceil((y0 - min_y) / CHIRPS_RESOLUTION) + options.margin_cells
        transform = [
            CHIRPS_RESOLUTION,
            0,
            x0 + col_start * CHIRPS_RESOLUTION,
            0,
            -CHIRPS_RESOLUTION,
            y0 - row_start * CHIRPS_RESOLUTION,
        ]
        image = collection.first().select("precipitation").toFloat().unmask(options.nodata)
        url = image.getDownloadURL(
            {
                "crs": "EPSG:4326",
                "crs_transform": transform,
                "dimensions": f"{col_stop - col_start}x{row_stop - row_start}",
                "format": "GEO_TIFF",
            }
        )
        array = geotiff_to_arrays(self.http_get(url), ["precip"], nodata=options.nodata)["precip"]
        array.attrs["units"] = "mm d-1"
        origin = f"gee:{GEE_CHIRPS_COLLECTION}:{day.isoformat()}"
        return [PendingBronze(array, spec, day, ProductStatus.FINAL, origin)]
