"""``make_raster``: rebuild a GeoTIFF/COG from a Gold table.

Gold rows carry ``cell_id = row * width + col`` on the grid stored in ``grid.json``,
so a raster is rebuilt by scattering values into an array of the grid shape: no
interpolation, no coordinate matching, exact round trip.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import rasterio

from climate_pipeline.domain.dates import EpiWeek
from climate_pipeline.domain.errors import ConfigurationError, DataNotAvailableError
from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.domain.variables import ResamplingMethod, VariableCatalog
from climate_pipeline.processing.resample import resample_to_grid
from climate_pipeline.storage.gold import GoldStore, GoldTable

_IDENTIFIER_COLUMNS = {
    "cell_id",
    "date",
    "latitude",
    "longitude",
    "epi_year",
    "epi_week",
    "week_start",
    "week_end",
}


def _validate_column(column: str) -> None:
    if not column.replace("_", "").isalnum() or column in _IDENTIFIER_COLUMNS:
        raise ConfigurationError(f"invalid value column {column!r}")


def load_layer(
    store: GoldStore,
    grid: GridSpec,
    column: str,
    *,
    day: date | None = None,
    week: EpiWeek | None = None,
) -> np.ndarray:
    """2-D float32 array of ``column`` on ``grid`` (NaN where there is no row)."""
    _validate_column(column)
    if day is not None and week is not None:
        raise ConfigurationError("pass either day or week, not both")
    if day is not None:
        sql = f'SELECT cell_id, "{column}" AS value FROM {GoldTable.DAILY} WHERE "date" = ?'
        parameters: list[object] = [day]
    elif week is not None:
        sql = (
            f'SELECT cell_id, "{column}" AS value FROM {GoldTable.EPIWEEKLY} '
            "WHERE epi_year = ? AND epi_week = ?"
        )
        parameters = [week.year, week.week]
    else:
        sql = f'SELECT cell_id, "{column}" AS value FROM {GoldTable.STATIC}'
        parameters = []
    try:
        rows = store.query(sql, parameters)
    except Exception as exc:  # duckdb raises Catalog/Binder errors for unknown tables/columns
        raise DataNotAvailableError(f"cannot read {column!r} from Gold: {exc}") from exc
    if rows.empty:
        raise DataNotAvailableError(f"no Gold rows for {column!r} (day={day}, week={week})")

    cell_ids = rows["cell_id"].to_numpy(dtype="int64")
    if cell_ids.min() < 0 or cell_ids.max() >= grid.width * grid.height:
        raise ConfigurationError("cell_id outside the stored grid: Gold and grid.json disagree")
    layer = np.full(grid.width * grid.height, np.nan, dtype="float32")
    layer[cell_ids] = rows["value"].to_numpy(dtype="float32", na_value=np.nan)
    return layer.reshape(grid.shape)


def make_raster(
    store: GoldStore,
    column: str,
    output_path: Path,
    *,
    day: date | None = None,
    week: EpiWeek | None = None,
    template: GridSpec | None = None,
    catalog: VariableCatalog | None = None,
    as_cog: bool = True,
) -> Path:
    """Write ``column`` for a day, an epi week or (neither) the static table as a raster.

    ``template`` defaults to the grid the Gold store was built on. When a different
    template is given, the layer is resampled with the variable's resampling method.
    """
    stored_grid = GridSpec.load(store.layout.grid_path)
    values = load_layer(store, stored_grid, column, day=day, week=week)

    target = template or stored_grid
    if target.fingerprint() != stored_grid.fingerprint():
        method = ResamplingMethod.BILINEAR
        base_column = column.removesuffix("_ndays")
        if column.endswith("_ndays"):
            method = ResamplingMethod.NEAREST
        elif catalog is not None and catalog.has_gold_column(base_column):
            method = catalog.by_gold_column(base_column).resampling
        array = resample_to_grid(stored_grid.template(values, name=column), target, method)
        values = array.values.astype("float32")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "COG" if as_cog else "GTiff",
        "width": target.width,
        "height": target.height,
        "count": 1,
        "dtype": "float32",
        "crs": target.crs,
        "transform": target.affine,
        "nodata": np.nan,
        "compress": "deflate",
    }
    with rasterio.open(output_path, "w", **profile) as destination:
        destination.write(values, 1)
        destination.set_band_description(1, column)
        destination.update_tags(
            column=column,
            date=day.isoformat() if day else "",
            epi_week=week.label if week else "",
        )
    return output_path
