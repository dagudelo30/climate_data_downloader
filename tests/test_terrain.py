import numpy as np
import pytest

from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.pipeline.static import (
    STATIC_COLUMNS,
    _static_frame,
    copernicus_tile_names,
    terrain_to_grid,
)
from climate_pipeline.processing.terrain import terrain_derivatives


def test_projected_plane_rising_north_faces_south(raster_factory) -> None:
    rows, cols, resolution = 30, 30, 100.0
    y = 1_950_000 - resolution * (np.arange(rows) + 0.5)
    elevation = np.repeat(((y - y.min()) * 0.05)[:, None], cols, axis=1)  # 5 % grade towards north
    dem = raster_factory(elevation, west=4_600_000, north=1_950_000, resolution=resolution, crs="EPSG:9377")
    terrain = terrain_derivatives(dem)
    np.testing.assert_allclose(terrain["slope_deg"], np.degrees(np.arctan(0.05)), rtol=1e-4)
    np.testing.assert_allclose(terrain["northness"], -1.0, atol=1e-6)  # downslope = south
    np.testing.assert_allclose(terrain["eastness"], 0.0, atol=1e-6)


def test_geographic_plane_rising_east_faces_west(raster_factory) -> None:
    rows, cols, resolution = 21, 21, 0.001
    dem = raster_factory(np.zeros((rows, cols)), west=-76.6, north=3.5, resolution=resolution)
    lat = dem["y"].values[:, None]
    x_m = (dem["x"].values[None, :] - dem["x"].values.min()) * 111_320.0 * np.cos(np.deg2rad(lat))
    dem = dem.copy(data=(0.1 * x_m).astype("float32"))  # 10 % grade towards east
    terrain = terrain_derivatives(dem)
    np.testing.assert_allclose(terrain["slope_deg"], np.degrees(np.arctan(0.1)), rtol=1e-3)
    np.testing.assert_allclose(terrain["eastness"], -1.0, atol=1e-4)  # downslope = west
    np.testing.assert_allclose(terrain["northness"], 0.0, atol=1e-3)


def test_flat_terrain_has_zero_aspect_components(raster_factory) -> None:
    terrain = terrain_derivatives(raster_factory(np.full((5, 5), 1000.0), west=0, north=1, resolution=0.01))
    assert float(terrain["slope_deg"].max()) == 0.0
    assert float(np.abs(terrain["northness"]).max()) == 0.0
    assert float(np.abs(terrain["eastness"]).max()) == 0.0


def test_static_frame_on_target_grid(raster_factory, aoi) -> None:
    rng = np.random.default_rng(1)
    dem = raster_factory(950 + rng.uniform(0, 50, size=(300, 300)), west=-76.7, north=3.6, resolution=0.001)
    grid = GridSpec.from_resolution("EPSG:9377", 5000, aoi)
    frame = _static_frame(grid, aoi, terrain_to_grid(dem, grid))
    assert set(STATIC_COLUMNS.values()) <= set(frame.columns)
    assert frame[list(STATIC_COLUMNS.values())].notna().all().all()
    assert frame["static_elevation_m"].between(950, 1000).all()
    assert frame["static_northness"].between(-1, 1).all()
    assert frame["cell_id"].is_unique


def test_copernicus_tile_names_cover_bounds() -> None:
    names = copernicus_tile_names((-76.6, 3.3, -76.4, 3.5))
    assert names == ["Copernicus_DSM_COG_30_N03_00_W077_00_DEM"]
    assert len(copernicus_tile_names((-77.2, -0.5, -76.4, 0.5))) == 4
    assert "Copernicus_DSM_COG_30_S01_00_W078_00_DEM" in copernicus_tile_names((-77.2, -0.5, -76.4, 0.5))


def test_terrain_needs_two_by_two(raster_factory) -> None:
    with pytest.raises(ValueError, match="2x2"):
        terrain_derivatives(raster_factory(np.ones((1, 5)), west=0, north=1, resolution=0.01))
