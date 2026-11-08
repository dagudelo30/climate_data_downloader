"""Static orographic covariates derived from a DEM.

Outputs, all aggregated to the target grid with area-weighted averaging:

* ``elevation_m``: mean elevation of the cell;
* ``slope_deg``: mean slope computed at the DEM's native resolution (aggregating the
  slope keeps sub-grid relief information that is lost if the slope is computed on the
  aggregated DEM);
* ``northness``/``eastness``: mean cosine/sine of the aspect. Aspect is circular, so it
  cannot be averaged directly; its components can. Flat cells contribute 0.
* ``tpi_500m``/``tpi_2km``: topographic position index, elevation minus the mean elevation
  within ~500 m / ~2 km (positive on ridges, negative in valleys): cold-air pooling, wind
  exposure and orographic rainfall (used by CHELSA and ML wind/temperature downscaling);
* ``tri_m``: terrain ruggedness index (Riley 1999), mean absolute elevation difference to
  the 8 neighbours: surface roughness for wind.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from pyproj import CRS

_METERS_PER_DEGREE_LAT = 110_574.0
_METERS_PER_DEGREE_LON_EQUATOR = 111_320.0
# Grades below 1e-6 m/m are numerical noise (float coordinates), not terrain: treated as flat
# so that they do not produce an arbitrary aspect.
_FLAT_GRADIENT = 1e-6


def box_mean(values: np.ndarray, half: int) -> np.ndarray:
    """Mean over a ``(2·half+1)²`` window (edges padded by replication), via summed areas."""
    if half < 1:
        return values.copy()
    padded = np.pad(values, half, mode="edge")
    summed = np.pad(padded.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))
    size = 2 * half + 1
    window = summed[size:, size:] - summed[:-size, size:] - summed[size:, :-size] + summed[:-size, :-size]
    return window / (size * size)


def ruggedness(values: np.ndarray) -> np.ndarray:
    padded = np.pad(values, 1, mode="edge")
    rows, cols = values.shape
    total = np.zeros_like(values)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                total += np.abs(padded[1 + dy : 1 + dy + rows, 1 + dx : 1 + dx + cols] - values)
    return total / 8.0


def terrain_derivatives(dem: xr.DataArray) -> xr.Dataset:
    """Elevation, slope and aspect components for a north-up DEM with ``y``/``x`` dims."""
    elevation = dem.astype("float64")
    z = elevation.values
    x = elevation["x"].values.astype("float64")
    y = elevation["y"].values.astype("float64")
    if z.shape[0] < 2 or z.shape[1] < 2:
        raise ValueError("terrain derivatives need at least 2x2 cells")

    y_m = y * _METERS_PER_DEGREE_LAT
    if CRS.from_user_input(dem.rio.crs).is_geographic:
        cos_lat = np.cos(np.deg2rad(y))[:, None]
        dz_dy = np.gradient(z, y_m, axis=0)
        dz_dx = np.gradient(z, x, axis=1) / (_METERS_PER_DEGREE_LON_EQUATOR * cos_lat)
    else:
        dz_dy = np.gradient(z, y, axis=0)
        dz_dx = np.gradient(z, x, axis=1)

    pixel_m = float(np.median(np.abs(np.diff(y_m if CRS.from_user_input(dem.rio.crs).is_geographic else y))))
    filled = np.where(np.isfinite(z), z, np.nanmean(z))
    tpi = {radius: filled - box_mean(filled, max(round(radius / pixel_m), 1)) for radius in (500, 2000)}
    tri = ruggedness(filled)

    gradient = np.hypot(dz_dx, dz_dy)
    sloped = gradient > _FLAT_GRADIENT
    slope = np.where(sloped, np.degrees(np.arctan(gradient)), 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        # Aspect = compass direction of steepest descent, i.e. of (-dz/dx, -dz/dy).
        northness = np.where(sloped, -dz_dy / gradient, 0.0)
        eastness = np.where(sloped, -dz_dx / gradient, 0.0)

    def _like(values: np.ndarray, name: str, units: str) -> xr.DataArray:
        array = elevation.copy(data=values.astype("float32")).rename(name)
        array.attrs = {"units": units}
        return array

    return xr.Dataset(
        {
            "elevation_m": _like(z, "elevation_m", "m"),
            "slope_deg": _like(slope, "slope_deg", "degree"),
            "northness": _like(northness, "northness", "1"),
            "eastness": _like(eastness, "eastness", "1"),
            "tpi_500m": _like(tpi[500], "tpi_500m", "m"),
            "tpi_2km": _like(tpi[2000], "tpi_2km", "m"),
            "tri_m": _like(tri, "tri_m", "m"),
        }
    )
