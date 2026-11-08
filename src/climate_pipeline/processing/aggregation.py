"""Daily → epidemiological-week aggregation of Gold tables.

Rules (per column, taken from the variable catalog):

* ``SUM`` (precipitation): weekly total, only if **all 7 days** are present; a total
  with missing days would be biased low and silently look like a dry week.
* ``MEAN`` (temperature, humidity, radiation, wind, NDVI/NDWI): mean of the valid days,
  only if at least ``min_valid_days`` are present.
* ``MIN``/``MAX``: extreme of the valid days under the same completeness rule.

Each aggregated column ``c`` is accompanied by ``c_ndays`` (number of valid daily values
that entered the aggregate), so completeness is auditable downstream.

The epidemiological week is derived exactly as in :mod:`climate_pipeline.domain.dates`
(Sunday-Saturday weeks, the week belongs to the year of its Wednesday).
"""

from __future__ import annotations

import logging

import duckdb
import pandas as pd

from climate_pipeline.domain.errors import QualityError
from climate_pipeline.domain.variables import VariableCatalog, WeeklyAggregation
from climate_pipeline.processing.quality import check_unique_keys

logger = logging.getLogger(__name__)

DAILY_KEYS = ("cell_id", "date")
WEEKLY_KEYS = ("cell_id", "epi_year", "epi_week")
_ID_COLUMNS = {"cell_id", "date", "latitude", "longitude"}

_SQL_AGGREGATES = {
    WeeklyAggregation.SUM: "sum",
    WeeklyAggregation.MEAN: "avg",
    WeeklyAggregation.MIN: "min",
    WeeklyAggregation.MAX: "max",
}


def ndays_column(column: str) -> str:
    return f"{column}_ndays"


def aggregate_to_epi_weeks(daily: pd.DataFrame, catalog: VariableCatalog) -> pd.DataFrame:
    """Aggregate a daily Gold frame to one row per ``(cell_id, epi_year, epi_week)``."""
    if daily.empty:
        return pd.DataFrame(columns=[*WEEKLY_KEYS, "week_start", "week_end"])
    check_unique_keys(daily, DAILY_KEYS, "gold_daily")
    for column in ("latitude", "longitude"):
        if column not in daily.columns:
            raise QualityError(f"gold_daily: missing column {column}")

    value_columns = [column for column in daily.columns if column not in _ID_COLUMNS]
    known = [column for column in value_columns if catalog.has_gold_column(column)]
    ignored = sorted(set(value_columns) - set(known))
    if ignored:
        logger.warning("columns without catalog spec are not aggregated: %s", ignored)

    selects = []
    for column in known:
        spec = catalog.by_gold_column(column)
        function = _SQL_AGGREGATES[spec.weekly_aggregation]
        quoted = f'"{column}"'
        selects.append(
            f"CASE WHEN count({quoted}) >= {spec.min_valid_days} "
            f"THEN CAST({function}({quoted}) AS FLOAT) END AS {quoted}"
        )
        selects.append(f'CAST(count({quoted}) AS TINYINT) AS "{ndays_column(column)}"')

    value_sql = (",\n            " + ",\n            ".join(selects)) if selects else ""
    query = f"""
        WITH days AS (
            SELECT *, CAST(CAST("date" AS DATE) - CAST(dayofweek(CAST("date" AS DATE)) AS INTEGER)
                AS DATE) AS week_start
            FROM daily
        )
        SELECT
            cell_id,
            CAST(year(week_start + 3) AS SMALLINT) AS epi_year,
            CAST((dayofyear(week_start + 3) - 1) // 7 + 1 AS TINYINT) AS epi_week,
            week_start,
            CAST(week_start + 6 AS DATE) AS week_end,
            any_value(latitude) AS latitude,
            any_value(longitude) AS longitude,
            CAST(count(DISTINCT "date") AS TINYINT) AS days_observed{value_sql}
        FROM days
        GROUP BY cell_id, week_start
        ORDER BY week_start, cell_id
    """
    connection = duckdb.connect()
    try:
        connection.register("daily", daily)
        weekly = connection.execute(query).df()
    finally:
        connection.close()
    weekly["week_start"] = pd.to_datetime(weekly["week_start"]).dt.date
    weekly["week_end"] = pd.to_datetime(weekly["week_end"]).dt.date
    check_unique_keys(weekly, WEEKLY_KEYS, "gold_epiweekly")
    return weekly
