import numpy as np
import pandas as pd
import pytest
import xarray as xr

from climate_pipeline.domain.errors import QualityError
from climate_pipeline.domain.variables import (
    AGERA5_VARIABLES,
    AIR_TEMPERATURE_BOUNDS,
    NORMALIZED_INDEX_BOUNDS,
    PRECIPITATION_BOUNDS,
    RELATIVE_HUMIDITY_BOUNDS,
    VariableCatalog,
    default_specs,
    normalized_index_spec,
)
from climate_pipeline.processing.quality import (
    ConsistencyRule,
    apply_bounds,
    check_bounds,
    check_unique_keys,
    enforce_consistency,
)


def _array(values: list[float]) -> xr.DataArray:
    return xr.DataArray(np.array(values, dtype="float64"), dims=("x",))


def test_ndvi_and_ndwi_outside_minus_one_one_are_rejected() -> None:
    cleaned, report = apply_bounds(
        _array([-1.0, -0.2, 0.0, 0.85, 1.0, 1.7, -1.3, np.inf, -np.inf, np.nan]),
        NORMALIZED_INDEX_BOUNDS,
        "gee/ndvi",
    )
    np.testing.assert_array_equal(
        cleaned.values,
        [-1.0, -0.2, 0.0, 0.85, 1.0, np.nan, np.nan, np.nan, np.nan, np.nan],
    )
    assert report.total_values == 9  # the input NaN is not a rejection
    assert report.above_maximum == 1
    assert report.below_minimum == 1
    assert report.non_finite == 2
    assert report.valid_values == 5
    valid = cleaned.values[~np.isnan(cleaned.values)]
    assert valid.min() >= -1 and valid.max() <= 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-95.0, np.nan), (-89.0, -89.0), (24.3, 24.3), (56.0, 56.0), (61.0, np.nan), (300.0, np.nan)],
)
def test_air_temperature_physical_limits(value: float, expected: float) -> None:
    cleaned, _ = apply_bounds(_array([value]), AIR_TEMPERATURE_BOUNDS, "t")
    np.testing.assert_array_equal(cleaned.values, [expected])


def test_kelvin_values_left_unconverted_are_caught() -> None:
    # A temperature of 297 °C means someone forgot K → °C: must not reach Gold.
    cleaned, report = apply_bounds(_array([297.15, 301.0]), AIR_TEMPERATURE_BOUNDS, "t")
    assert np.isnan(cleaned.values).all()
    assert report.above_maximum == 2


def test_relative_humidity_tolerance_band_clips_then_rejects() -> None:
    cleaned, report = apply_bounds(_array([-3.0, -1.0, 50.0, 101.5, 103.0]), RELATIVE_HUMIDITY_BOUNDS, "rh")
    np.testing.assert_array_equal(cleaned.values, [np.nan, 0.0, 50.0, 100.0, np.nan])
    assert report.clipped == 2
    assert report.below_minimum == 1 and report.above_maximum == 1


def test_negative_precipitation_is_rejected() -> None:
    cleaned, _ = apply_bounds(_array([-0.1, 0.0, 12.5, 2500.0]), PRECIPITATION_BOUNDS, "p")
    np.testing.assert_array_equal(cleaned.values, [np.nan, 0.0, 12.5, np.nan])


def test_temperature_consistency_nulls_inconsistent_rows() -> None:
    frame = pd.DataFrame(
        {
            "agera5_temp_min": [18.0, 25.0, 19.0, np.nan],
            "agera5_temp_mean": [24.0, 24.0, 23.8, 24.0],
            "agera5_temp_max": [30.0, 29.0, 23.5, 30.0],
            "agera5_dewpoint_mean": [19.0, 19.0, 19.0, 26.0],
        }
    )
    cleaned, report = enforce_consistency(frame)
    # Row 0 is consistent.
    assert cleaned.loc[0].notna().all()
    # Row 1: Tmin 25 > Tmean 24 (+0.5 tolerance) and > Tmax? no → only Tmin/Tmean nulled.
    assert np.isnan(cleaned.loc[1, "agera5_temp_min"]) and np.isnan(cleaned.loc[1, "agera5_temp_mean"])
    assert cleaned.loc[1, "agera5_temp_max"] == 29.0
    # Row 2: Tmean 23.8 vs Tmax 23.5 is within the 0.5 °C tolerance → kept.
    assert cleaned.loc[2].notna().all()
    # Row 3: dewpoint 26 > Tmean 24 → both nulled; missing Tmin does not trigger rules.
    assert np.isnan(cleaned.loc[3, "agera5_dewpoint_mean"])
    assert report.violations["Tmin <= Tmean"] == 1
    assert report.violations["Tdew <= Tmean"] == 1
    assert report.total == 2


def test_consistency_rule_on_absent_columns_is_skipped() -> None:
    frame = pd.DataFrame({"chirps_precip": [1.0]})
    cleaned, report = enforce_consistency(frame, [ConsistencyRule("a", "b")])
    pd.testing.assert_frame_equal(cleaned, frame)
    assert report.total == 0


def test_check_unique_keys() -> None:
    frame = pd.DataFrame({"cell_id": [1, 1], "date": ["2026-01-01", "2026-01-01"]})
    with pytest.raises(QualityError, match="duplicated"):
        check_unique_keys(frame, ["cell_id", "date"], "gold_daily")
    with pytest.raises(QualityError, match="missing key"):
        check_unique_keys(frame, ["cell_id", "epi_week"], "gold_epiweekly")


def test_check_bounds_detects_out_of_range_values() -> None:
    catalog = VariableCatalog([*default_specs(), normalized_index_spec("gee", "ndvi", "")])
    good = pd.DataFrame({"gee_ndvi": [0.1, 0.9, np.nan], "agera5_temp_mean": [18.0, 25.0, np.nan]})
    check_bounds(good, catalog, "gold_daily")
    with pytest.raises(QualityError, match="gee_ndvi"):
        check_bounds(pd.DataFrame({"gee_ndvi": [1.2]}), catalog, "gold_daily")
    with pytest.raises(QualityError, match="infinite"):
        check_bounds(pd.DataFrame({"agera5_temp_max": [np.inf]}), catalog, "gold_daily")
    # Weekly precipitation sums may exceed the daily maximum (up to 7×).
    check_bounds(pd.DataFrame({"chirps_precip": [2500.0]}), catalog, "gold_epiweekly")


def test_catalog_bounds_are_physically_sensible() -> None:
    catalog = VariableCatalog([*default_specs(), normalized_index_spec("gee", "ndvi", "")])
    for spec in catalog.all():
        if spec.units == "degC":
            assert spec.bounds.minimum >= -100 and spec.bounds.maximum <= 60
        if spec.name == "precipitation_flux":
            assert spec.bounds.minimum == 0
        if spec.units == "%":
            assert (spec.bounds.minimum, spec.bounds.maximum) == (0, 100)
        if spec.source == "gee":
            assert (spec.bounds.minimum, spec.bounds.maximum) == (-1, 1)
    assert {spec.units for spec in AGERA5_VARIABLES if "temp" in spec.name} == {"degC"}
