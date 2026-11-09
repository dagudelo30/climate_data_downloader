"""Resampling of Silver arrays onto a Gold target grid.

``rasterio.warp.reproject`` runs directly on the numpy arrays (the same GDAL warp that
``rio.reproject`` wraps): building xarray coordinates for every call cost ~4x the warp
itself, and a Gold day resamples ~80 arrays per grid.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from affine import Affine
from rasterio.enums import Resampling
from rasterio.warp import reproject

from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.domain.variables import ResamplingMethod
from climate_pipeline.processing.spatial import grid_transform

RASTERIO_RESAMPLING: dict[ResamplingMethod, Resampling] = {
    ResamplingMethod.BILINEAR: Resampling.bilinear,
    # GDAL "average" weights each contributing source cell by its overlapping area,
    # the area-conservative behaviour wanted for fluxes such as precipitation.
    ResamplingMethod.AVERAGE: Resampling.average,
    ResamplingMethod.NEAREST: Resampling.nearest,
}


def _warp(
    data: np.ndarray, transform: Affine, crs: object, grid: GridSpec, method: ResamplingMethod
) -> np.ndarray:
    destination = np.full(grid.shape, np.nan, dtype="float32")
    reproject(
        source=data,
        destination=destination,
        src_transform=transform,
        src_crs=crs,
        src_nodata=np.nan,
        dst_transform=grid.affine,
        dst_crs=grid.crs,
        dst_nodata=np.nan,
        resampling=RASTERIO_RESAMPLING[method],
    )
    return destination


def resample_to_grid(
    values: xr.DataArray, grid: GridSpec, method: ResamplingMethod, edge_fallback: bool = True
) -> xr.DataArray:
    """Reproject ``values`` onto ``grid``; NaN cells are treated as nodata and ignored.

    Silver arrays are masked to the AOI, so near the AOI edge the bilinear kernel of a
    target cell may sample only masked source cells and return NaN even though the target
    cell overlaps valid data. With ``edge_fallback`` those cells take the area-weighted
    average of the valid source cells they overlap, which keeps the all_touched promise:
    every Gold cell touching the AOI gets a value whenever the source covers it.
    """
    crs = values.rio.crs
    if crs is None:
        raise ValueError(f"{values.name}: cannot resample an array without CRS")
    data = np.ascontiguousarray(values.squeeze(drop=True).values, dtype="float32")
    if data.ndim != 2:
        raise ValueError(f"{values.name}: expected a 2-D (y, x) array, got shape {data.shape}")
    transform = grid_transform(values)
    result = _warp(data, transform, crs, grid, method)
    if edge_fallback and method is ResamplingMethod.BILINEAR:
        missing = np.isnan(result)
        if missing.any():
            fallback = _warp(data, transform, crs, grid, ResamplingMethod.AVERAGE)
            result = np.where(missing, fallback, result)
    # Coordinates pinned to the exact grid definition (no float drift between sources).
    return grid.template(result, name=str(values.name or "value"))
