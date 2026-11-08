import numpy as np
import pytest
import xarray as xr

from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.domain.variables import AGERA5_VARIABLES, CHIRPS_VARIABLES, normalized_index_spec
from climate_pipeline.processing.harmonize import (
    SilverInput,
    build_silver_dataset,
    convert_units,
    harmonize_variable,
    normalize_units,
    select_data_variable,
)

SPECS = {spec.name: spec for spec in AGERA5_VARIABLES}


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("J m**-2 day**-1", "J m-2 d-1"),
        ("kg m-2 d-1", "mm d-1"),
        ("Celsius", "degC"),
        ("K", "K"),
        ("m/s", "m s-1"),
        ("", "1"),
    ],
)
def test_normalize_units(raw: str, canonical: str) -> None:
    assert normalize_units(raw) == canonical


def test_unit_conversions() -> None:
    values = xr.DataArray(np.array([273.15, 300.0]))
    np.testing.assert_allclose(convert_units(values, "K", "degC"), [0.0, 26.85])
    np.testing.assert_allclose(convert_units(xr.DataArray([18.5e6]), "J m-2 d-1", "MJ m-2 d-1"), [18.5])
    with pytest.raises(ConfigurationError):
        convert_units(values, "K", "MJ m-2 d-1")


def test_select_data_variable_ignores_crs_variable() -> None:
    dataset = xr.Dataset({"Temperature_Air_2m_Mean_24h": ("x", [1.0]), "crs": ((), 0)})
    assert select_data_variable(dataset).name == "Temperature_Air_2m_Mean_24h"
    with pytest.raises(ConfigurationError):
        select_data_variable(xr.Dataset({"a": ("x", [1.0]), "b": ("x", [2.0])}))


def test_harmonize_temperature_converts_kelvin_and_renames(raster_factory) -> None:
    raw = raster_factory(
        np.full((3, 3), 298.15), west=-77, north=4, resolution=0.1, name="Temperature_Air_2m_Mean_24h"
    )
    raw.attrs["units"] = "K"
    raw = raw.expand_dims(time=[np.datetime64("2026-01-01")])
    result, report = harmonize_variable(raw, SPECS["temp_mean_2m"])
    assert result.name == "temp_mean_2m"
    assert result.dims == ("y", "x")
    assert result.attrs["units"] == "degC"
    np.testing.assert_allclose(result.values, 25.0, atol=1e-4)
    assert result.dtype == np.float32
    assert report.valid_values == 9


def test_harmonize_missing_units_attribute_uses_native_units(raster_factory) -> None:
    raw = raster_factory(np.full((2, 2), 18.0e6), west=-77, north=4, resolution=0.1)
    result, _ = harmonize_variable(raw, SPECS["solar_radiation_flux"])
    np.testing.assert_allclose(result.values, 18.0)


def test_harmonize_applies_physical_bounds_to_ndvi(raster_factory) -> None:
    raw = raster_factory(np.array([[0.4, 1.8], [-1.6, np.nan]]), west=-77, north=4, resolution=0.01)
    result, report = harmonize_variable(raw, normalized_index_spec("gee", "ndvi", "NDVI"))
    assert np.isnan(result.values[0, 1]) and np.isnan(result.values[1, 0])
    assert report.above_maximum == 1 and report.below_minimum == 1


def test_build_silver_dataset_merges_variables_and_clips(raster_factory, aoi) -> None:
    tmean = raster_factory(np.full((10, 10), 297.15), west=-77, north=4, resolution=0.1)
    tmean.attrs["units"] = "K"
    tmax = raster_factory(np.full((10, 10), 303.15), west=-77, north=4, resolution=0.1)
    tmax.attrs["units"] = "K"
    from datetime import date

    dataset, reports = build_silver_dataset(
        "agera5",
        date(2026, 1, 1),
        [
            SilverInput(tmean, SPECS["temp_mean_2m"], "final"),
            SilverInput(tmax, SPECS["temp_max_2m"], "final"),
        ],
        aoi,
    )
    assert set(dataset.data_vars) == {"temp_mean_2m", "temp_max_2m"}
    assert dataset.attrs["spatial_rule"] == "all_touched=True"
    assert dataset.attrs["product_status"] == "final"
    # AOI spans 0.12° x 0.175°: with all_touched it must cover at least 2x2 cells of 0.1°.
    assert dataset.sizes["x"] >= 2 and dataset.sizes["y"] >= 2
    assert float(dataset["temp_max_2m"].max()) == pytest.approx(30.0, abs=1e-4)
    assert len(reports) == 2


def test_chirps_precipitation_spec() -> None:
    spec = CHIRPS_VARIABLES[0]
    assert spec.gold_column == "chirps_precip"
    assert spec.units == "mm d-1"
