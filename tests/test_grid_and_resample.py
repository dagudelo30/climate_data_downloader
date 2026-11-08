from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine

from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.domain.variables import ResamplingMethod
from climate_pipeline.pipeline.orchestrator import NamedGrid
from climate_pipeline.processing.resample import resample_to_grid


def test_projected_grid_is_snapped_to_resolution(aoi) -> None:
    grid = GridSpec.from_resolution("EPSG:9377", 1000, aoi)
    a, _, c, _, e, f = grid.transform
    assert (a, e) == (1000, -1000)
    assert c % 1000 == 0 and f % 1000 == 0
    min_x, min_y, max_x, max_y = aoi.bounds_in("EPSG:9377")
    left, top = c, f
    right, bottom = c + grid.width * a, f + grid.height * e
    assert left <= min_x and right >= max_x and bottom <= min_y and top >= max_y
    assert grid.resolution == (1000, 1000)


def test_five_km_grid_nests_one_km_grid(aoi) -> None:
    one = GridSpec.from_resolution("EPSG:9377", 1000, aoi)
    five = GridSpec.from_resolution("EPSG:9377", 5000, aoi)
    # Both snapped to multiples of their resolution → 5 km edges are also 1 km edges.
    assert five.transform[2] % 1000 == 0 and five.transform[5] % 1000 == 0
    assert five.width * 5 >= one.width and five.height * 5 >= one.height


def test_parse_named_and_anonymous_grids(aoi, tmp_path: Path) -> None:
    assert NamedGrid.parse("1km=EPSG:9377@1000") == NamedGrid("1km", "EPSG:9377@1000")
    assert NamedGrid.parse("EPSG:4326@0.05").name == "epsg_4326_0.05"
    assert NamedGrid.parse(None) == NamedGrid("native", None)
    with pytest.raises(ValueError, match="invalid grid name"):
        NamedGrid.parse("bad name=EPSG:4326@0.1")
    grid = GridSpec.parse("EPSG:4326@0.05", aoi)
    assert grid.resolution == pytest.approx((0.05, 0.05))
    with pytest.raises(ConfigurationError):
        GridSpec.parse("EPSG:4326@abc", aoi)
    with pytest.raises(ConfigurationError, match="not found"):
        GridSpec.parse(str(tmp_path / "missing.tif"), aoi)


def test_grid_from_template_raster_and_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "template.tif"
    transform = Affine(5000, 0, 4_590_000, 0, -5000, 1_950_000)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=4,
        height=5,
        count=1,
        dtype="float32",
        crs="EPSG:9377",
        transform=transform,
    ) as destination:
        destination.write(np.zeros((1, 5, 4), dtype="float32"))
    grid = GridSpec.from_raster(path)
    assert grid.shape == (5, 4)
    assert grid.crs == "EPSG:9377"
    grid.save(tmp_path / "grid.json")
    loaded = GridSpec.load(tmp_path / "grid.json")
    assert loaded == grid and loaded.fingerprint() == grid.fingerprint()
    np.testing.assert_allclose(grid.x_coords(), [4_592_500, 4_597_500, 4_602_500, 4_607_500])


def test_lonlat_of_projected_grid_are_geographic(aoi) -> None:
    grid = GridSpec.from_resolution("EPSG:9377", 1000, aoi)
    lon, lat = grid.lonlat()
    assert lon.shape == grid.shape
    assert -76.7 < lon.min() < lon.max() < -76.3
    assert 3.2 < lat.min() < lat.max() < 3.6


def test_rotated_or_south_up_grids_are_rejected() -> None:
    with pytest.raises(ConfigurationError):
        GridSpec("EPSG:4326", (0.1, 0.0, 0.0, 0.0, 0.1, 0.0), 2, 2)
    with pytest.raises(ConfigurationError):
        GridSpec("EPSG:4326", (0.1, 0.01, 0.0, 0.0, -0.1, 0.0), 2, 2)


def test_average_resampling_conserves_area_mean(raster_factory) -> None:
    rng = np.random.default_rng(0)
    values = rng.uniform(0, 50, size=(8, 8))
    fine = raster_factory(values, west=0.0, north=0.8, resolution=0.1)
    coarse_grid = GridSpec("EPSG:4326", (0.2, 0.0, 0.0, 0.0, -0.2, 0.8), 4, 4)
    coarse = resample_to_grid(fine, coarse_grid, ResamplingMethod.AVERAGE)
    expected = values.reshape(4, 2, 4, 2).mean(axis=(1, 3))
    np.testing.assert_allclose(coarse.values, expected, rtol=1e-5)
    assert float(coarse.mean()) == pytest.approx(values.mean(), rel=1e-5)


def test_bilinear_keeps_constant_fields_constant_and_ignores_nan(raster_factory) -> None:
    values = np.full((6, 6), 24.0)
    values[0, 0] = np.nan
    source = raster_factory(values, west=0.0, north=0.6, resolution=0.1)
    target = GridSpec("EPSG:4326", (0.025, 0.0, 0.1, 0.0, -0.025, 0.5), 16, 16)
    result = resample_to_grid(source, target, ResamplingMethod.BILINEAR)
    finite = result.values[np.isfinite(result.values)]
    assert finite.size > 0
    np.testing.assert_allclose(finite, 24.0, rtol=1e-6)
    np.testing.assert_allclose(result.x, target.x_coords())


def test_resampling_to_projected_grid(raster_factory, aoi) -> None:
    source = raster_factory(np.full((10, 10), 3.0), west=-77.0, north=4.0, resolution=0.1)
    grid = GridSpec.from_resolution("EPSG:9377", 5000, aoi)
    result = resample_to_grid(source, grid, ResamplingMethod.AVERAGE)
    assert result.shape == grid.shape
    np.testing.assert_allclose(result.values, 3.0, rtol=1e-6)


def test_bilinear_edge_fallback_fills_cells_overlapping_valid_data(raster_factory) -> None:
    # Valid source data only in the left column (AOI-masked Silver); the target cell
    # centre falls on the masked side, but the cell overlaps the valid column.
    values = np.array([[10.0, np.nan], [12.0, np.nan]])
    source = raster_factory(values, west=0.0, north=0.2, resolution=0.1)
    target = GridSpec("EPSG:4326", (0.15, 0.0, 0.05, 0.0, -0.2, 0.2), 1, 1)  # centre x=0.125
    plain = resample_to_grid(source, target, ResamplingMethod.BILINEAR, edge_fallback=False)
    filled = resample_to_grid(source, target, ResamplingMethod.BILINEAR)
    assert np.isnan(plain.values).all()
    assert float(filled.values[0, 0]) == pytest.approx(11.0)
