"""Bronze → Silver harmonisation: variable selection, units, QC, naming and clipping."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
import xarray as xr

from climate_pipeline.domain.aoi import AreaOfInterest
from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.domain.variables import VariableSpec
from climate_pipeline.processing.quality import BoundsReport, apply_bounds
from climate_pipeline.processing.spatial import ClipMode, clip_to_aoi, standardize_spatial_dims

NON_DATA_VARIABLES = {"crs", "spatial_ref", "time_bnds", "lat_bnds", "lon_bnds"}


def normalize_units(units: str) -> str:
    """Canonical spelling of a unit string (``J m**-2 day**-1`` → ``J m-2 d-1``)."""
    text = units.strip().replace("**", "").replace("^", "")
    text = re.sub(r"\bday\b|\bdays\b", "d", text)
    text = re.sub(r"\s+", " ", text)
    aliases = {
        "kelvin": "K",
        "degrees_kelvin": "K",
        "c": "degC",
        "°c": "degC",
        "celsius": "degC",
        "degrees_celsius": "degC",
        "deg_c": "degC",
        "kg m-2 d-1": "mm d-1",
        "mm/d": "mm d-1",
        "mm/day": "mm d-1",
        "mm": "mm d-1",
        "percent": "%",
        "fraction": "1",
        "(0-1)": "1",
        "0-1": "1",
        "1.0": "1",
        "dimensionless": "1",
        "-": "1",
        "": "1",
        "m/s": "m s-1",
        "mbar": "hPa",
        "j m-2": "J m-2 d-1",
    }
    return aliases.get(text.lower(), text)


UNIT_CONVERSIONS: dict[tuple[str, str], Callable[[xr.DataArray], xr.DataArray]] = {
    ("K", "degC"): lambda values: values - 273.15,
    ("degC", "K"): lambda values: values + 273.15,
    ("J m-2 d-1", "MJ m-2 d-1"): lambda values: values / 1e6,
    ("kJ m-2 d-1", "MJ m-2 d-1"): lambda values: values / 1e3,
    ("Pa", "hPa"): lambda values: values / 100.0,
    ("kPa", "hPa"): lambda values: values * 10.0,
    ("%", "1"): lambda values: values / 100.0,
    ("1", "%"): lambda values: values * 100.0,
}


def convert_units(values: xr.DataArray, from_units: str, to_units: str) -> xr.DataArray:
    source, target = normalize_units(from_units), normalize_units(to_units)
    if source == target:
        return values
    try:
        converter = UNIT_CONVERSIONS[(source, target)]
    except KeyError as exc:
        raise ConfigurationError(f"no conversion from {from_units!r} to {to_units!r}") from exc
    return converter(values)


def select_data_variable(data: xr.Dataset | xr.DataArray, preferred: str | None = None) -> xr.DataArray:
    """Pick the single geophysical variable of a file (AgERA5/CHIRPS files hold one)."""
    if isinstance(data, xr.DataArray):
        return data
    candidates = [name for name in data.data_vars if str(name).lower() not in NON_DATA_VARIABLES]
    if preferred is not None and preferred in candidates:
        return data[preferred]
    if len(candidates) == 1:
        return data[candidates[0]]
    raise ConfigurationError(f"cannot choose a data variable among {candidates}")


def _drop_time(values: xr.DataArray) -> xr.DataArray:
    for dim in ("time", "t"):
        if dim in values.dims:
            if values.sizes[dim] != 1:
                raise ConfigurationError(f"expected a single time step, got {values.sizes[dim]}")
            values = values.isel({dim: 0}, drop=True)
    return values.drop_vars([c for c in ("time", "t") if c in values.coords])


def harmonize_variable(
    data: xr.Dataset | xr.DataArray,
    spec: VariableSpec,
    nodata: float | None = None,
) -> tuple[xr.DataArray, BoundsReport]:
    """Convert one Bronze variable to its canonical Silver form (before clipping)."""
    values = select_data_variable(data, preferred=spec.name)
    values = _drop_time(standardize_spatial_dims(values))
    crs = values.rio.crs
    transform = values.rio.transform()

    values = values.astype("float64")
    nodata = values.rio.nodata if nodata is None else nodata
    if nodata is not None and not np.isnan(nodata):
        values = values.where(values != nodata)

    source_units = values.attrs.get("units") or spec.native_units
    values = convert_units(values, source_units, spec.units)
    values, report = apply_bounds(values, spec.bounds, variable=f"{spec.source}/{spec.name}")

    values = values.astype("float32").rename(spec.name)
    values.attrs = {
        "units": spec.units,
        "long_name": spec.description,
        "source": spec.source,
        "gold_column": spec.gold_column,
    }
    values = values.rio.write_crs(crs).rio.write_transform(transform)
    return values.rio.write_nodata(np.nan, encoded=False), report


@dataclass(frozen=True)
class SilverInput:
    """A Bronze array paired with its variable spec."""

    data: xr.Dataset | xr.DataArray
    spec: VariableSpec
    status: str


def build_silver_dataset(
    source: str,
    day: date,
    inputs: Sequence[SilverInput],
    aoi: AreaOfInterest,
    clip_mode: ClipMode = "polygon",
) -> tuple[xr.Dataset, list[BoundsReport]]:
    """Harmonise and clip every variable of one source for one day into a dataset."""
    if not inputs:
        raise ValueError("build_silver_dataset needs at least one input")
    arrays: list[xr.DataArray] = []
    reports: list[BoundsReport] = []
    for item in inputs:
        array, report = harmonize_variable(item.data, item.spec)
        arrays.append(array)
        reports.append(report)

    reference = arrays[0]
    dx = abs(float(reference.rio.transform().a))
    aligned = [reference]
    for array in arrays[1:]:
        if (
            array.shape == reference.shape
            and np.allclose(array.x, reference.x)
            and np.allclose(array.y, reference.y)
        ):
            aligned.append(array.assign_coords(x=reference.x, y=reference.y))
        else:
            aligned.append(array.reindex_like(reference, method="nearest", tolerance=dx * 1e-3))
    dataset = xr.merge(aligned, compat="override", join="override", combine_attrs="drop")
    dataset = dataset.rio.write_crs(reference.rio.crs).rio.write_transform(reference.rio.transform())

    dataset = clip_to_aoi(dataset, aoi, mode=clip_mode)
    statuses = sorted({item.status for item in inputs})
    dataset = dataset.assign_coords(time=pd.Timestamp(day).to_datetime64())
    dataset.attrs.update(
        {
            "source": source,
            "date": day.isoformat(),
            "product_status": ",".join(statuses),
            "aoi": aoi.name,
            "aoi_fingerprint": aoi.fingerprint,
            "spatial_rule": "all_touched=True",
            "Conventions": "CF-1.8",
        }
    )
    return dataset, reports
