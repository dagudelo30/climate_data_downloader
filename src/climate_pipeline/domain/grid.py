"""Regular raster grid specification used as the Gold target grid."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr
from affine import Affine
from pyproj import CRS, Transformer

from climate_pipeline.domain.aoi import AreaOfInterest
from climate_pipeline.domain.errors import ConfigurationError

_SNAP_EPSILON = 1e-9


@dataclass(frozen=True)
class GridSpec:
    """North-up regular grid: CRS + affine transform + shape.

    Cells are addressed by ``cell_id = row * width + col``; this is the key that links
    the Gold tables back to raster space (see :mod:`climate_pipeline.storage.raster`).
    """

    crs: str
    transform: tuple[float, float, float, float, float, float]
    width: int
    height: int

    def __post_init__(self) -> None:
        a, b, _, d, e, _ = self.transform
        if b != 0 or d != 0:
            raise ConfigurationError("rotated grids are not supported")
        if a <= 0 or e >= 0:
            raise ConfigurationError("grid must be north-up (positive x step, negative y step)")
        if self.width < 1 or self.height < 1:
            raise ConfigurationError("grid must have at least one cell")

    # --- constructors ---------------------------------------------------------------
    @classmethod
    def from_raster(cls, path: str | Path) -> GridSpec:
        path = Path(path)
        if not path.exists():
            raise ConfigurationError(f"target grid raster not found: {path}")
        with rasterio.open(path) as src:
            if src.crs is None:
                raise ConfigurationError(f"target grid raster has no CRS: {path}")
            return cls(
                crs=src.crs.to_string(),
                transform=tuple(src.transform)[:6],
                width=src.width,
                height=src.height,
            )

    @classmethod
    def from_resolution(cls, crs: str, resolution: float, aoi: AreaOfInterest) -> GridSpec:
        """Grid of ``resolution`` (CRS units) snapped to multiples of the resolution."""
        if resolution <= 0:
            raise ConfigurationError("grid resolution must be positive")
        crs_string = CRS.from_user_input(crs).to_string()
        min_x, min_y, max_x, max_y = aoi.bounds_in(crs_string)
        left = math.floor(min_x / resolution + _SNAP_EPSILON) * resolution
        bottom = math.floor(min_y / resolution + _SNAP_EPSILON) * resolution
        right = math.ceil(max_x / resolution - _SNAP_EPSILON) * resolution
        top = math.ceil(max_y / resolution - _SNAP_EPSILON) * resolution
        width = max(round((right - left) / resolution), 1)
        height = max(round((top - bottom) / resolution), 1)
        return cls(
            crs=crs_string,
            transform=(resolution, 0.0, left, 0.0, -resolution, top),
            width=width,
            height=height,
        )

    @classmethod
    def from_bounds(cls, crs: str, resolution: float, bounds: tuple[float, float, float, float]) -> GridSpec:
        """Grid of ``resolution`` covering ``bounds`` (in ``crs`` units), snapped to multiples."""
        if resolution <= 0:
            raise ConfigurationError("grid resolution must be positive")
        min_x, min_y, max_x, max_y = bounds
        left = math.floor(min_x / resolution + _SNAP_EPSILON) * resolution
        bottom = math.floor(min_y / resolution + _SNAP_EPSILON) * resolution
        right = math.ceil(max_x / resolution - _SNAP_EPSILON) * resolution
        top = math.ceil(max_y / resolution - _SNAP_EPSILON) * resolution
        return cls(
            crs=CRS.from_user_input(crs).to_string(),
            transform=(resolution, 0.0, left, 0.0, -resolution, top),
            width=max(round((right - left) / resolution), 1),
            height=max(round((top - bottom) / resolution), 1),
        )

    @classmethod
    def from_dataarray(cls, data: xr.DataArray | xr.Dataset) -> GridSpec:
        transform = data.rio.transform()
        crs = data.rio.crs
        if crs is None:
            raise ConfigurationError("dataset has no CRS")
        return cls(
            crs=crs.to_string(),
            transform=tuple(transform)[:6],
            width=data.rio.width,
            height=data.rio.height,
        )

    @classmethod
    def parse(cls, value: str, aoi: AreaOfInterest) -> GridSpec:
        """Parse ``--target-grid``: a raster path or ``<CRS>@<resolution>``.

        Examples: ``templates/grid_5km.tif``, ``EPSG:4326@0.05``, ``EPSG:3116@1000``.
        """
        if "@" in value and not Path(value).exists():
            crs, _, resolution = value.rpartition("@")
            try:
                return cls.from_resolution(crs, float(resolution), aoi)
            except ValueError as exc:
                raise ConfigurationError(f"invalid grid spec {value!r}: {exc}") from exc
        return cls.from_raster(value)

    # --- geometry -------------------------------------------------------------------
    @property
    def affine(self) -> Affine:
        return Affine(*self.transform)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    @property
    def resolution(self) -> tuple[float, float]:
        return (self.transform[0], -self.transform[4])

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """``(left, bottom, right, top)`` in grid CRS units."""
        a, _, c, _, e, f = self.transform
        return (c, f + e * self.height, c + a * self.width, f)

    @property
    def is_geographic(self) -> bool:
        return CRS.from_user_input(self.crs).is_geographic

    def x_coords(self) -> np.ndarray:
        a, _, c, *_ = self.transform
        return c + a * (np.arange(self.width) + 0.5)

    def y_coords(self) -> np.ndarray:
        *_, e, f = self.transform
        return f + e * (np.arange(self.height) + 0.5)

    def lonlat(self) -> tuple[np.ndarray, np.ndarray]:
        """2-D longitude/latitude of cell centres."""
        x_grid, y_grid = np.meshgrid(self.x_coords(), self.y_coords())
        if self.is_geographic:
            return x_grid, y_grid
        transformer = Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)
        lon, lat = transformer.transform(x_grid, y_grid)
        return np.asarray(lon), np.asarray(lat)

    def template(self, values: np.ndarray | None = None, name: str = "value") -> xr.DataArray:
        """DataArray on this grid (NaN-filled unless ``values`` is given)."""
        data = np.full(self.shape, np.nan, dtype="float32") if values is None else values
        if data.shape != self.shape:
            raise ValueError(f"values shape {data.shape} does not match grid {self.shape}")
        array = xr.DataArray(
            data,
            dims=("y", "x"),
            coords={"y": self.y_coords(), "x": self.x_coords()},
            name=name,
        )
        array = array.rio.write_crs(self.crs).rio.write_transform(self.affine)
        return array.rio.write_nodata(np.nan, encoded=False)

    # --- identity -------------------------------------------------------------------
    def fingerprint(self) -> str:
        crs = CRS.from_user_input(self.crs)
        identity = {
            "crs": crs.to_epsg() or crs.to_wkt(),
            "transform": [round(value, 9) for value in self.transform],
            "shape": [self.height, self.width],
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {**asdict(self), "transform": list(self.transform), "fingerprint": self.fingerprint()}

    @classmethod
    def from_dict(cls, payload: dict) -> GridSpec:
        return cls(
            crs=payload["crs"],
            transform=tuple(payload["transform"]),
            width=int(payload["width"]),
            height=int(payload["height"]),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path) -> GridSpec:
        return cls.from_dict(json.loads(path.read_text()))


@dataclass(frozen=True)
class Nesting:
    """How a coarse grid sits on a fine one: every coarse cell is exactly a
    ``factor × factor`` block of fine cells starting at ``(row_offset, col_offset)``."""

    factor: int
    row_offset: int
    col_offset: int


def _integer(value: float, label: str) -> int:
    rounded = round(value)
    if abs(value - rounded) > 1e-6:
        raise ConfigurationError(f"grids are not nested: {label} = {value:.6f} is not an integer")
    return int(rounded)


def nest(fine: GridSpec, coarse: GridSpec) -> Nesting:
    """Check that ``coarse`` is an exact aggregation of ``fine`` and return the block layout."""
    if not CRS.from_user_input(fine.crs).equals(CRS.from_user_input(coarse.crs)):
        raise ConfigurationError(f"grids are not nested: CRS {fine.crs} != {coarse.crs}")
    fx, fy = fine.resolution
    cx, cy = coarse.resolution
    factor = _integer(cx / fx, "resolution ratio")
    if _integer(cy / fy, "resolution ratio") != factor:
        raise ConfigurationError("grids are not nested: x and y resolution ratios differ")
    f_left, _, _, f_top = fine.bounds
    c_left, _, _, c_top = coarse.bounds
    col_offset = _integer((c_left - f_left) / fx, "column offset")
    row_offset = _integer((f_top - c_top) / fy, "row offset")
    if (
        col_offset < 0
        or row_offset < 0
        or col_offset + coarse.width * factor > fine.width
        or row_offset + coarse.height * factor > fine.height
    ):
        raise ConfigurationError("grids are not nested: the coarse grid extends beyond the fine grid")
    return Nesting(factor, row_offset, col_offset)


def block_mean(values: np.ndarray, nesting: Nesting, shape: tuple[int, int]) -> np.ndarray:
    """Mean of the valid fine cells of every coarse cell (NaN when none is valid).

    Fine cells are equal-area in a projected CRS, so the plain mean is the area-weighted
    mean: precipitation, fractions and densities are conserved, and continuous fields
    (temperature, LST...) get the average over the coarse cell.
    """
    height, width = shape
    k = nesting.factor
    window = values[
        nesting.row_offset : nesting.row_offset + height * k,
        nesting.col_offset : nesting.col_offset + width * k,
    ].astype("float64")
    blocks = window.reshape(height, k, width, k)
    valid = np.isfinite(blocks)
    count = valid.sum(axis=(1, 3))
    total = np.where(valid, blocks, 0.0).sum(axis=(1, 3))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, total / np.maximum(count, 1), np.nan).astype("float32")


def parent_cell_ids(fine: GridSpec, coarse: GridSpec, cell_ids: np.ndarray) -> np.ndarray:
    """``cell_id`` of the coarse cell containing each fine cell (-1 outside the coarse grid).

    Joins Gold tables across scales without spatial operations (e.g. a 100 m row to its 1 km
    parent). Grids built with ``from_resolution`` are aligned to multiples of their resolution,
    so this works for nested and independent grids alike; only alignment is required.
    """
    if not CRS.from_user_input(fine.crs).equals(CRS.from_user_input(coarse.crs)):
        raise ConfigurationError(f"grids are not aligned: CRS {fine.crs} != {coarse.crs}")
    fx, fy = fine.resolution
    factor = _integer(coarse.resolution[0] / fx, "resolution ratio")
    col_offset = _integer((coarse.bounds[0] - fine.bounds[0]) / fx, "column offset")
    row_offset = _integer((fine.bounds[3] - coarse.bounds[3]) / fy, "row offset")
    ids = np.asarray(cell_ids, dtype="int64")
    rows = ids // fine.width - row_offset
    cols = ids % fine.width - col_offset
    parent_rows, parent_cols = np.floor_divide(rows, factor), np.floor_divide(cols, factor)
    inside = (
        (parent_rows >= 0) & (parent_cols >= 0) & (parent_rows < coarse.height) & (parent_cols < coarse.width)
    )
    return np.where(inside, parent_rows * coarse.width + parent_cols, -1)
