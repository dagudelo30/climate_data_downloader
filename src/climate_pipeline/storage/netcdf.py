"""Atomic NetCDF I/O.

Writes go to a temporary file in the destination directory and are moved into place
with an atomic rename, so an interrupted run never leaves a truncated file that a later
run would mistake for a valid one.

The netCDF4/HDF5 C libraries are not thread-safe, and extractors download in parallel
threads, so every NetCDF read/write goes through a process-wide lock.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import xarray as xr

NETCDF_LOCK = threading.RLock()


def write_netcdf_atomic(dataset: xr.Dataset | xr.DataArray, path: Path) -> Path:
    if isinstance(dataset, xr.DataArray):
        dataset = dataset.to_dataset(name=dataset.name or "value")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoding = {
        name: {"zlib": True, "complevel": 4}
        for name, variable in dataset.data_vars.items()
        if variable.dtype.kind in "fiu"
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with NETCDF_LOCK:
            dataset.to_netcdf(temporary, engine="netcdf4", encoding=encoding)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def read_netcdf(path: Path) -> xr.Dataset:
    """Load a dataset fully into memory and release the file handle."""
    with NETCDF_LOCK, xr.open_dataset(path, engine="netcdf4", decode_coords="all") as dataset:
        loaded = dataset.load()
    # The CF grid-mapping variable is metadata, never a geophysical variable.
    grid_mapping = [name for name in ("spatial_ref",) if name in loaded.data_vars]
    return loaded.set_coords(grid_mapping) if grid_mapping else loaded
