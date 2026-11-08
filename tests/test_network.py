"""Smoke tests against real public endpoints (no credentials). Run: ``uv run pytest -m network``."""

from datetime import date

import numpy as np
import pytest

from climate_pipeline.extractors.chirps import ChirpsOptions, read_window
from climate_pipeline.pipeline.static import fetch_copernicus_dem

pytestmark = pytest.mark.network

CALI_BOUNDS = (-76.59, 3.33, -76.46, 3.50)


def test_chirps_v3_final_window_read() -> None:
    options = ChirpsOptions()
    _, template = options.templates()[0]
    day = date(2026, 1, 15)
    url = template.format(
        base_url=options.base_url, disaggregation="rnl", year=day.year, month=day.month, day=day.day
    )
    array = read_window(url, CALI_BOUNDS, margin_cells=2, nodata=-9999.0)
    assert array.sizes["x"] <= 10 and array.sizes["y"] <= 10
    assert np.nanmin(array.values) >= 0


def test_copernicus_dem_cali_elevation() -> None:
    dem = fetch_copernicus_dem(CALI_BOUNDS, resolution_deg=0.005)
    # Cali sits around 1,000 m a.s.l. with the Farallones to the west.
    assert 900 < float(np.nanmedian(dem.values)) < 1300
    assert float(np.nanmax(dem.values)) > 1200
