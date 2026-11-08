"""Shared fixtures: synthetic AOIs, rasters, a local CHIRPS mirror and a fake CDS client."""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pytest
import rasterio
import xarray as xr
from affine import Affine
from shapely.geometry import Polygon, box

from climate_pipeline.config import Settings, load_pipeline_config
from climate_pipeline.domain.aoi import AreaOfInterest
from climate_pipeline.storage.layout import DataLake

# A small polygon around Cali (not a rectangle, so polygon and bbox clipping differ).
CALI_LIKE = Polygon([(-76.58, 3.33), (-76.46, 3.36), (-76.47, 3.505), (-76.56, 3.49)])


@pytest.fixture
def aoi() -> AreaOfInterest:
    return AreaOfInterest.from_geometry(CALI_LIKE, name="test_aoi")


@pytest.fixture
def aoi_file(tmp_path: Path) -> Path:
    path = tmp_path / "aoi.geojson"
    gpd.GeoDataFrame({"name": ["test"]}, geometry=[CALI_LIKE], crs="EPSG:4326").to_file(path)
    return path


@pytest.fixture
def lake(tmp_path: Path) -> DataLake:
    return DataLake(tmp_path / "lake")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, climate_data_root=tmp_path / "lake")


def make_raster(
    values: np.ndarray,
    west: float,
    north: float,
    resolution: float,
    crs: str = "EPSG:4326",
    name: str = "value",
) -> xr.DataArray:
    """North-up DataArray with ``y``/``x`` cell-centre coordinates."""
    rows, cols = values.shape
    x = west + resolution * (np.arange(cols) + 0.5)
    y = north - resolution * (np.arange(rows) + 0.5)
    array = xr.DataArray(values.astype("float32"), dims=("y", "x"), coords={"y": y, "x": x}, name=name)
    transform = Affine(resolution, 0, west, 0, -resolution, north)
    return array.rio.write_crs(crs).rio.write_transform(transform)


@pytest.fixture
def raster_factory() -> Callable[..., xr.DataArray]:
    return make_raster


# --- local CHIRPS mirror ----------------------------------------------------------------
CHIRPS_WEST, CHIRPS_NORTH, CHIRPS_RES = -77.0, 4.0, 0.05


def write_chirps_tif(path: Path, day: date, rows: int = 20, cols: int = 20) -> np.ndarray:
    """Synthetic CHIRPS-like GeoTIFF: precipitation = day-of-month + column index / 10."""
    values = np.fromfunction(lambda r, c: day.day + c / 10.0, (rows, cols), dtype="float32")
    values[0, 0] = -9999.0  # CHIRPS ocean/nodata sentinel
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "width": cols,
        "height": rows,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": Affine(CHIRPS_RES, 0, CHIRPS_WEST, 0, -CHIRPS_RES, CHIRPS_NORTH),
    }
    with rasterio.open(path, "w", **profile) as destination:
        destination.write(values, 1)
    return values


@pytest.fixture
def chirps_mirror(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "chirps_mirror"
    final = str(root / "final" / "{year}" / "chirps.{year}.{month:02d}.{day:02d}.tif")
    prelim = str(root / "prelim" / "{year}" / "chirps.{year}.{month:02d}.{day:02d}.tif")

    def publish(day: date, status: str = "final") -> Path:
        template = final if status == "final" else prelim
        path = Path(template.format(year=day.year, month=day.month, day=day.day))
        write_chirps_tif(path, day)
        return path

    return {"final_template": final, "prelim_template": prelim, "publish": publish}


# --- fake Copernicus CDS ----------------------------------------------------------------
AGERA5_FAKE_VALUES: dict[tuple[str, str | None], tuple[str, float, str]] = {
    ("2m_temperature", "24_hour_mean"): ("Temperature_Air_2m_Mean_24h", 297.15, "K"),
    ("2m_temperature", "24_hour_maximum"): ("Temperature_Air_2m_Max_24h", 302.15, "K"),
    ("2m_temperature", "24_hour_minimum"): ("Temperature_Air_2m_Min_24h", 291.15, "K"),
    ("2m_dewpoint_temperature", "24_hour_mean"): ("Dew_Point_Temperature_2m_Mean", 292.15, "K"),
    ("solar_radiation_flux", None): ("Solar_Radiation_Flux", 18.5e6, "J m-2 d-1"),
    ("vapour_pressure", "24_hour_mean"): ("Vapour_Pressure_Mean", 22.0, "hPa"),
    ("2m_relative_humidity", "06_00"): ("Relative_Humidity_2m_06h", 95.0, "%"),
    ("2m_relative_humidity", "15_00"): ("Relative_Humidity_2m_15h", 62.0, "%"),
    ("10m_wind_speed", "24_hour_mean"): ("Wind_Speed_10m_Mean", 2.1, "m s-1"),
    ("precipitation_flux", None): ("Precipitation_Flux", 4.0, "mm d-1"),
    ("cloud_cover", "24_hour_mean"): ("Cloud_Cover_Mean", 0.6, "1"),
    ("2m_temperature", "day_time_maximum"): ("Temperature_Air_2m_Max_Day_Time", 302.15, "K"),
    ("2m_temperature", "day_time_mean"): ("Temperature_Air_2m_Mean_Day_Time", 299.15, "K"),
    ("2m_temperature", "night_time_mean"): ("Temperature_Air_2m_Mean_Night_Time", 294.15, "K"),
    ("2m_temperature", "night_time_minimum"): ("Temperature_Air_2m_Min_Night_Time", 291.15, "K"),
    ("2m_relative_humidity", "09_00"): ("Relative_Humidity_2m_09h", 85.0, "%"),
    ("2m_relative_humidity", "12_00"): ("Relative_Humidity_2m_12h", 70.0, "%"),
    ("2m_relative_humidity", "18_00"): ("Relative_Humidity_2m_18h", 75.0, "%"),
    ("2m_relative_humidity_derived", "24_hour_maximum"): ("Relative_Humidity_2m_Max", 97.0, "%"),
    ("2m_relative_humidity_derived", "24_hour_minimum"): ("Relative_Humidity_2m_Min", 55.0, "%"),
    ("vapour_pressure_deficit_at_maximum_temperature", None): ("Vapour_Pressure_Deficit_Tmax", 12.0, "hPa"),
    ("reference_evapotranspiration", None): ("ET0", 3.8, "mm d-1"),
    ("precipitation_duration_fraction", None): ("Precipitation_Duration_Fraction", 0.3, "1"),
}


class FakeCdsClient:
    """Mimics ``cdsapi.Client.retrieve`` for AgERA5: a zip with one NetCDF per day."""

    def __init__(
        self, unavailable_after: date | None = None, offsets: dict[str, float] | None = None
    ) -> None:
        self.unavailable_after = unavailable_after
        self.offsets = offsets or {}
        self.requests: list[dict[str, Any]] = []

    def retrieve(self, dataset: str, request: dict[str, Any], target: str) -> None:
        self.requests.append(request)
        year, month = int(request["year"][0]), int(request["month"][0])
        days = [date(year, month, int(day)) for day in request["day"]]
        if self.unavailable_after and all(day > self.unavailable_after for day in days):
            raise RuntimeError("The job failed: There is no data matching your request")
        key = (request["variable"], (request.get("statistic") or request.get("time") or [None])[0])
        name, base, units = AGERA5_FAKE_VALUES[key]
        base += self.offsets.get(name, 0.0)
        north, west, south, east = request["area"]
        lat = np.round(np.arange(north, south - 1e-9, -0.1), 2)
        lon = np.round(np.arange(west, east + 1e-9, 0.1), 2)
        workdir = Path(target).parent
        with zipfile.ZipFile(target, "w") as bundle:
            for day in days:
                if self.unavailable_after and day > self.unavailable_after:
                    continue
                gradient = np.add.outer(np.zeros(lat.size), (lon - lon.min()) * 0.1)
                values = (base + gradient)[None, :, :].astype("float32")
                dataset_nc = xr.Dataset(
                    {
                        name: (("time", "lat", "lon"), values, {"units": units}),
                        "crs": (("time",), np.array([0])),
                    },
                    coords={"time": [datetime(day.year, day.month, day.day)], "lat": lat, "lon": lon},
                )
                file_name = f"{name}_C3S-glob-agric_AgERA5_{day:%Y%m%d}_final-v2.0.0.nc"
                local = workdir / file_name
                dataset_nc.to_netcdf(local)
                bundle.write(local, file_name)
                local.unlink()


@pytest.fixture
def fake_cds() -> FakeCdsClient:
    return FakeCdsClient()


@pytest.fixture
def pipeline_config(chirps_mirror: dict[str, Any]):
    config = load_pipeline_config(None)
    # These fixtures test the pipeline mechanics with a local CHIRPS mirror: keep every column
    # (the redundancy rule would drop chirps_precip in favour of the faster IMERG).
    config.deduplicate = False
    chirps = config.sources["chirps"]
    chirps.options = {
        **chirps.options,
        "final_template": chirps_mirror["final_template"],
        "prelim_template": chirps_mirror["prelim_template"],
    }
    chirps.max_workers = 2
    config.sources["agera5"].max_workers = 2
    return config


@pytest.fixture
def square() -> Polygon:
    return box(-76.6, 3.3, -76.4, 3.5)
