import numpy as np
import pytest
import xarray as xr
from shapely.geometry import Polygon, box

from climate_pipeline.domain.aoi import AreaOfInterest
from climate_pipeline.domain.errors import EmptySelectionError
from climate_pipeline.processing.spatial import (
    aoi_mask,
    clip_to_aoi,
    grid_resolution,
    standardize_spatial_dims,
    subset_to_bounds,
)


def test_standardize_renames_wraps_and_orders(raster_factory) -> None:
    values = np.arange(12, dtype="float32").reshape(3, 4)
    raw = xr.DataArray(
        values,
        dims=("lat", "lon"),
        coords={"lat": [1.0, 2.0, 3.0], "lon": [282.0, 283.0, 284.0, 285.0]},  # ascending lat, 0..360 lon
    )
    result = standardize_spatial_dims(raw)
    assert result.dims == ("y", "x")
    np.testing.assert_allclose(result.x, [-78.0, -77.0, -76.0, -75.0])
    np.testing.assert_allclose(result.y, [3.0, 2.0, 1.0])
    assert result.rio.crs.to_epsg() == 4326
    # Values follow their coordinates when re-ordered.
    assert float(result.sel(y=3.0, x=-78.0)) == values[2, 0]


def test_grid_resolution_single_cell_uses_transform(raster_factory) -> None:
    array = raster_factory(np.ones((1, 1)), west=-76.6, north=3.5, resolution=0.1)
    dx, dy = grid_resolution(array)
    assert dx == pytest.approx(0.1)
    assert dy == pytest.approx(-0.1)


def test_subset_keeps_cells_touching_the_bounds(raster_factory) -> None:
    # 0.1° cells with edges at -77.0, -76.9, ...; bounds start exactly on an edge.
    array = raster_factory(np.ones((10, 10)), west=-77.0, north=4.0, resolution=0.1)
    subset = subset_to_bounds(array, (-76.75, 3.55, -76.45, 3.75))
    # x: cells [-76.8,-76.7] ... [-76.5,-76.4] → 4 cells ; y: [3.5,3.6] .. [3.7,3.8] → 3 cells
    assert subset.sizes == {"y": 3, "x": 4}
    edge = subset_to_bounds(array, (-76.7, 3.6, -76.6, 3.7))
    # Inclusive edges: cells sharing only an edge with the bounds are kept too.
    assert edge.sizes == {"y": 3, "x": 3}


def test_tiny_polygon_inside_one_pixel_keeps_that_pixel(raster_factory) -> None:
    array = raster_factory(np.arange(100.0).reshape(10, 10), west=-77.0, north=4.0, resolution=0.1)
    tiny = AreaOfInterest.from_geometry(box(-76.53, 3.52, -76.52, 3.53))  # 1 km² inside a 11 km cell
    clipped = clip_to_aoi(array, tiny)
    assert clipped.sizes == {"y": 1, "x": 1}
    assert float(clipped.values[0, 0]) == float(array.sel(x=-76.55, y=3.55, method="nearest"))
    assert clipped.rio.transform().a == pytest.approx(0.1)


def test_all_touched_keeps_cells_whose_centre_is_outside(raster_factory) -> None:
    array = raster_factory(np.ones((10, 10)), west=-77.0, north=4.0, resolution=0.1)
    # Triangle clipping only a corner fragment of the cell [-76.6,-76.5]x[3.5,3.6]:
    # its centre (-76.55, 3.55) is outside the triangle.
    triangle = AreaOfInterest.from_geometry(Polygon([(-76.62, 3.48), (-76.58, 3.48), (-76.62, 3.52)]))
    mask_touched = aoi_mask(array, triangle, all_touched=True)
    mask_centre = aoi_mask(array, triangle, all_touched=False)
    assert mask_touched.sum() > mask_centre.sum()
    clipped = clip_to_aoi(array, triangle)
    assert int(np.isfinite(clipped.values).sum()) == int(mask_touched.sum())


def test_polygon_mode_masks_cells_not_touching_and_trims(raster_factory, aoi) -> None:
    array = raster_factory(np.ones((40, 40)), west=-77.0, north=4.0, resolution=0.05)
    polygon = clip_to_aoi(array, aoi, mode="polygon")
    bbox = clip_to_aoi(array, aoi, mode="bbox")
    assert polygon.sizes == bbox.sizes  # trimmed to the same touched extent
    assert np.isnan(polygon.values).any()  # some bbox cells do not touch the polygon
    assert not np.isnan(bbox.values).any()
    assert polygon.attrs["clip_policy"] == "polygon_all_touched"
    # Every finite cell touches the AOI.
    assert np.isfinite(polygon.values).sum() == aoi_mask(polygon, aoi).sum()


def test_aoi_outside_extent_raises(raster_factory) -> None:
    array = raster_factory(np.ones((5, 5)), west=10.0, north=50.0, resolution=0.1)
    far = AreaOfInterest.from_geometry(box(-76.6, 3.3, -76.4, 3.5))
    with pytest.raises(EmptySelectionError):
        clip_to_aoi(array, far)


def test_clip_in_projected_crs(raster_factory, aoi) -> None:
    # 1 km grid in MAGNA-SIRGAS Origen-Nacional covering Cali.
    array = raster_factory(
        np.ones((40, 40)), west=4_590_000, north=1_960_000, resolution=1000, crs="EPSG:9377"
    )
    clipped = clip_to_aoi(array, aoi)
    assert clipped.rio.crs.to_epsg() == 9377
    finite = int(np.isfinite(clipped.values).sum())
    # The AOI is ~110 km²; all_touched on a 1 km grid keeps at least that many cells.
    assert finite >= 110
