"""Spatial normalisation and AOI clipping.

Fundamental rule of the pipeline: **every pixel that touches the AOI is kept**
(``all_touched=True``). A pixel survives if any fragment of its cell intersects the
polygon, even if its centre lies outside. This matters for coarse products (AgERA5 at
0.1°, MSWEP at 0.1°) where a centre-based rule would drop entire border cells or, for
small AOIs, return nothing at all.

The implementation does not depend on ``rio.clip``/``rio.clip_box`` heuristics: cell
edges are computed explicitly from the grid resolution and the polygon is rasterised
with :func:`rasterio.features.geometry_mask`.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr
from affine import Affine
from rasterio.features import geometry_mask

from climate_pipeline.domain.aoi import AreaOfInterest, Bounds
from climate_pipeline.domain.errors import EmptySelectionError

RasterLike = xr.DataArray | xr.Dataset
ClipMode = Literal["polygon", "bbox"]

_X_ALIASES = ("x", "lon", "longitude")
_Y_ALIASES = ("y", "lat", "latitude")
_EDGE_EPSILON = 1e-9


def standardize_spatial_dims(data: RasterLike, default_crs: str = "EPSG:4326") -> RasterLike:
    """Return data with ``y``/``x`` dims, ascending x, descending y and a CRS.

    * renames ``lon``/``longitude``/``lat``/``latitude`` to ``x``/``y``;
    * wraps 0..360 longitudes to -180..180;
    * drops singleton ``band`` dimensions produced by GeoTIFF readers.
    """
    rename: dict[str, str] = {}
    for name in data.dims:
        lowered = str(name).lower()
        if lowered in _X_ALIASES and name != "x":
            rename[name] = "x"
        elif lowered in _Y_ALIASES and name != "y":
            rename[name] = "y"
    if rename:
        data = data.rename(rename)
    if "x" not in data.dims or "y" not in data.dims:
        raise ValueError(f"cannot find spatial dimensions in {tuple(data.dims)}")
    if "band" in data.dims and data.sizes["band"] == 1:
        data = data.squeeze("band", drop=True)

    crs = data.rio.crs or default_crs
    if float(data["x"].max()) > 180.0 and _is_geographic(crs):
        data = data.assign_coords(x=((data["x"] + 180.0) % 360.0) - 180.0)
    data = data.sortby("x").sortby("y", ascending=False)
    data = data.rio.set_spatial_dims(x_dim="x", y_dim="y")
    data = data.rio.write_crs(crs)
    return data.rio.write_transform(grid_transform(data))


def _is_geographic(crs: object) -> bool:
    from pyproj import CRS

    return CRS.from_user_input(crs).is_geographic


def grid_resolution(data: RasterLike) -> tuple[float, float]:
    """``(dx, dy)`` with ``dx > 0`` and ``dy < 0`` for a north-up grid.

    Uses coordinate spacing when there are at least two cells along an axis and falls
    back to the stored transform for single-cell axes.
    """
    stored: Affine | None = None
    resolution = []
    for axis, sign in (("x", 1.0), ("y", -1.0)):
        values = np.asarray(data[axis].values, dtype="float64")
        if values.size >= 2:
            step = float(np.median(np.abs(np.diff(values))))
        else:
            if stored is None:
                stored = data.rio.transform()
            step = abs(stored.a if axis == "x" else stored.e)
        if step <= 0:
            raise ValueError(f"cannot infer resolution along {axis}")
        resolution.append(sign * step)
    return resolution[0], resolution[1]


def grid_transform(data: RasterLike) -> Affine:
    """Affine transform derived from cell-centre coordinates (north-up)."""
    dx, dy = grid_resolution(data)
    x0 = float(data["x"].values[0]) - dx / 2.0
    y0 = float(data["y"].values[0]) - dy / 2.0
    return Affine(dx, 0.0, x0, 0.0, dy, y0)


def subset_to_bounds(data: RasterLike, bounds: Bounds, buffer_cells: int = 0) -> RasterLike:
    """Keep every cell whose footprint touches ``bounds`` (inclusive on edges).

    ``bounds`` must be expressed in the CRS of ``data``. ``buffer_cells`` widens the
    selection by whole cells on each side.
    """
    dx, dy = grid_resolution(data)
    half_x, half_y = abs(dx) / 2.0, abs(dy) / 2.0
    eps_x, eps_y = abs(dx) * _EDGE_EPSILON, abs(dy) * _EDGE_EPSILON
    min_x, min_y, max_x, max_y = bounds
    min_x -= buffer_cells * abs(dx)
    max_x += buffer_cells * abs(dx)
    min_y -= buffer_cells * abs(dy)
    max_y += buffer_cells * abs(dy)

    x = np.asarray(data["x"].values)
    y = np.asarray(data["y"].values)
    keep_x = (x + half_x >= min_x - eps_x) & (x - half_x <= max_x + eps_x)
    keep_y = (y + half_y >= min_y - eps_y) & (y - half_y <= max_y + eps_y)
    if not keep_x.any() or not keep_y.any():
        raise EmptySelectionError(f"bounds {bounds} do not intersect the dataset extent")

    subset = data.isel(x=np.flatnonzero(keep_x), y=np.flatnonzero(keep_y))
    return _write_transform(subset, dx, dy)


def _write_transform(data: RasterLike, dx: float, dy: float) -> RasterLike:
    x0 = float(data["x"].values[0]) - dx / 2.0
    y0 = float(data["y"].values[0]) - dy / 2.0
    return data.rio.write_transform(Affine(dx, 0.0, x0, 0.0, dy, y0))


def aoi_mask(data: RasterLike, aoi: AreaOfInterest, all_touched: bool = True) -> np.ndarray:
    """Boolean ``(y, x)`` array: ``True`` where the cell touches the AOI."""
    geometry = aoi.geometry_in(data.rio.crs)
    return geometry_mask(
        [geometry],
        out_shape=(data.sizes["y"], data.sizes["x"]),
        transform=grid_transform(data),
        all_touched=all_touched,
        invert=True,
    )


def clip_to_aoi(data: RasterLike, aoi: AreaOfInterest, mode: ClipMode = "polygon") -> RasterLike:
    """Clip to the AOI keeping every touched cell.

    ``mode="polygon"`` masks cells that do not touch the polygon (set to NaN) and trims
    empty rows/columns. ``mode="bbox"`` keeps every cell touching the AOI bounding box.
    """
    dx, dy = grid_resolution(data)
    subset = subset_to_bounds(data, aoi.bounds_in(data.rio.crs))
    if mode == "bbox":
        return subset.assign_attrs(clip_policy="bbox_all_touched")

    mask = aoi_mask(subset, aoi, all_touched=True)
    if not mask.any():
        raise EmptySelectionError("AOI polygon does not touch any cell of the dataset")
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    subset = subset.isel(y=rows, x=cols)
    mask = mask[np.ix_(rows, cols)]

    crs = subset.rio.crs
    clipped = subset.where(xr.DataArray(mask, dims=("y", "x")))
    clipped = clipped.rio.write_crs(crs)
    clipped = _write_transform(clipped, dx, dy)
    return clipped.assign_attrs(clip_policy="polygon_all_touched")
