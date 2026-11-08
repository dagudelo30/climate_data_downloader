"""Resampling of Silver arrays onto a Gold target grid."""

from __future__ import annotations

import numpy as np
import xarray as xr
from rasterio.enums import Resampling

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


def _reproject(source: xr.DataArray, grid: GridSpec, method: ResamplingMethod) -> xr.DataArray:
    result = source.rio.reproject(
        dst_crs=grid.crs,
        shape=grid.shape,
        transform=grid.affine,
        resampling=RASTERIO_RESAMPLING[method],
        nodata=np.nan,
    )
    # Pin coordinates to the exact grid definition to avoid float drift between sources.
    return result.assign_coords(x=grid.x_coords(), y=grid.y_coords())


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
    if values.rio.crs is None:
        raise ValueError(f"{values.name}: cannot resample an array without CRS")
    source = values.astype("float32")
    source = source.rio.write_transform(grid_transform(source))
    source = source.rio.write_nodata(np.nan, encoded=False)
    result = _reproject(source, grid, method)
    if edge_fallback and method is ResamplingMethod.BILINEAR:
        missing = np.isnan(result.values)
        if missing.any():
            fallback = _reproject(source, grid, ResamplingMethod.AVERAGE)
            result = result.copy(data=np.where(missing, fallback.values, result.values))
    return result.rename(values.name)
