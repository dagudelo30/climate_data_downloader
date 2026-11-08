"""Area of interest loaded from any vector file readable by GeoPandas."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import shapely
from pyproj import CRS, Transformer
from shapely import make_valid
from shapely.geometry.base import BaseGeometry

from climate_pipeline.domain.errors import ConfigurationError

WGS84 = "EPSG:4326"

Bounds = tuple[float, float, float, float]


@dataclass(frozen=True, eq=False)
class AreaOfInterest:
    """Dissolved AOI geometry stored in EPSG:4326."""

    geometry: BaseGeometry
    name: str

    @classmethod
    def from_file(cls, path: str | Path, name: str | None = None) -> AreaOfInterest:
        path = Path(path)
        if not path.exists():
            raise ConfigurationError(f"AOI file not found: {path}")
        frame = gpd.read_file(path)
        if frame.empty:
            raise ConfigurationError(f"AOI file has no features: {path}")
        if frame.crs is None:
            raise ConfigurationError(f"AOI file has no CRS (missing .prj?): {path}")
        geometry = make_valid(frame.to_crs(WGS84).geometry.union_all())
        return cls.from_geometry(geometry, name=name or path.stem)

    @classmethod
    def from_geometry(cls, geometry: BaseGeometry, name: str = "aoi") -> AreaOfInterest:
        if geometry.is_empty:
            raise ConfigurationError("AOI geometry is empty")
        if geometry.geom_type not in {"Polygon", "MultiPolygon", "GeometryCollection"}:
            raise ConfigurationError(f"AOI must be polygonal, got {geometry.geom_type}")
        return cls(geometry=geometry, name=name)

    @property
    def bounds(self) -> Bounds:
        return tuple(self.geometry.bounds)  # type: ignore[return-value]

    def geometry_in(self, crs: str | CRS) -> BaseGeometry:
        target = CRS.from_user_input(crs)
        if target.equals(CRS.from_user_input(WGS84)):
            return self.geometry
        transformer = Transformer.from_crs(WGS84, target, always_xy=True)
        return shapely.transform(self.geometry, lambda x, y: transformer.transform(x, y), interleaved=False)

    def bounds_in(self, crs: str | CRS) -> Bounds:
        return tuple(self.geometry_in(crs).bounds)  # type: ignore[return-value]

    def expanded_bounds(self, margin: float) -> Bounds:
        """WGS84 bounds expanded by ``margin`` degrees, clamped to valid coordinates."""
        min_x, min_y, max_x, max_y = self.bounds
        return (
            max(min_x - margin, -180.0),
            max(min_y - margin, -90.0),
            min(max_x + margin, 180.0),
            min(max_y + margin, 90.0),
        )

    def cds_area(self, margin: float) -> list[float]:
        """``[north, west, south, east]`` rounded outwards to 0.01°, as the CDS API expects."""
        min_x, min_y, max_x, max_y = self.expanded_bounds(margin)
        return [
            math.ceil(max_y * 100) / 100,
            math.floor(min_x * 100) / 100,
            math.floor(min_y * 100) / 100,
            math.ceil(max_x * 100) / 100,
        ]

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.geometry.wkb).hexdigest()[:16]
