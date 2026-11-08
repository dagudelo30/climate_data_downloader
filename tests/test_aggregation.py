from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from climate_pipeline.domain.dates import EpiWeek, date_range
from climate_pipeline.domain.errors import QualityError
from climate_pipeline.domain.variables import VariableCatalog, default_specs, normalized_index_spec
from climate_pipeline.processing.aggregation import aggregate_to_epi_weeks

CATALOG = VariableCatalog([*default_specs(), normalized_index_spec("gee", "ndvi", "")])


def _daily(days: list[date], cells: tuple[int, ...] = (1, 2)) -> pd.DataFrame:
    rows = []
    for cell in cells:
        for index, day in enumerate(days):
            rows.append(
                {
                    "cell_id": cell,
                    "date": day,
                    "latitude": 3.4 + cell / 100,
                    "longitude": -76.5,
                    "chirps_precip": float(index + cell),  # 0+c, 1+c, ...
                    "agera5_temp_mean": 20.0 + index % 7,  # 20..26 over a full week
                    "agera5_temp_max": 28.0 + index % 7,
                    "gee_ndvi": 0.5,
                }
            )
    return pd.DataFrame(rows)


def _reference(daily: pd.DataFrame) -> pd.DataFrame:
    """Independent pandas implementation used to cross-check the SQL aggregation."""
    frame = daily.copy()
    frame["week"] = [EpiWeek.from_date(day) for day in frame["date"]]
    grouped = frame.groupby(["cell_id", "week"])
    result = grouped.agg(
        precip_sum=("chirps_precip", "sum"),
        precip_n=("chirps_precip", "count"),
        tmean=("agera5_temp_mean", "mean"),
        tmean_n=("agera5_temp_mean", "count"),
    ).reset_index()
    result.loc[result["precip_n"] < 7, "precip_sum"] = np.nan
    result.loc[result["tmean_n"] < 4, "tmean"] = np.nan
    return result


def test_full_weeks_match_independent_reference() -> None:
    days = date_range(date(2026, 1, 4), date(2026, 1, 31))  # epi weeks 2026-W01..W04, complete
    daily = _daily(days)
    weekly = aggregate_to_epi_weeks(daily, CATALOG)
    assert len(weekly) == 2 * 4
    assert weekly["epi_week"].tolist()[:2] == [1, 1]
    reference = _reference(daily)
    merged = weekly.merge(
        reference.assign(
            epi_year=[w.year for w in reference["week"]], epi_week=[w.week for w in reference["week"]]
        ),
        on=["cell_id", "epi_year", "epi_week"],
    )
    np.testing.assert_allclose(merged["chirps_precip"], merged["precip_sum"], rtol=1e-6)
    np.testing.assert_allclose(merged["agera5_temp_mean"], merged["tmean"], rtol=1e-6)
    assert (weekly["days_observed"] == 7).all()
    assert (weekly["chirps_precip_ndays"] == 7).all()


def test_weekly_precipitation_is_sum_and_temperature_is_mean() -> None:
    days = date_range(date(2026, 3, 15), date(2026, 3, 21))  # one full week (2026-W11)
    weekly = aggregate_to_epi_weeks(_daily(days, cells=(1,)), CATALOG)
    row = weekly.iloc[0]
    assert (row["epi_year"], row["epi_week"]) == (2026, 11)
    assert row["week_start"] == date(2026, 3, 15) and row["week_end"] == date(2026, 3, 21)
    assert row["chirps_precip"] == pytest.approx(sum(range(1, 8)))  # 1+2+...+7 = 28
    assert row["agera5_temp_mean"] == pytest.approx(23.0)  # mean of 20..26
    assert row["agera5_temp_max"] == pytest.approx(31.0)
    assert row["gee_ndvi"] == pytest.approx(0.5)


def test_incomplete_weeks_follow_completeness_rules() -> None:
    # 2026-01-01..03 are the last 3 days of epi week 2025-W53 (Dec 28 - Jan 3).
    days = date_range(date(2026, 1, 1), date(2026, 1, 3))
    weekly = aggregate_to_epi_weeks(_daily(days, cells=(1,)), CATALOG)
    row = weekly.iloc[0]
    assert (row["epi_year"], row["epi_week"]) == (2025, 53)
    assert row["days_observed"] == 3
    assert np.isnan(row["chirps_precip"])  # a 3-day total would look like a dry week
    assert row["chirps_precip_ndays"] == 3
    assert np.isnan(row["agera5_temp_mean"])  # 3 < 4 valid days
    assert row["gee_ndvi"] == pytest.approx(0.5)  # NDVI needs only 2 clear days


def test_missing_values_reduce_valid_day_counts() -> None:
    days = date_range(date(2026, 3, 15), date(2026, 3, 21))
    daily = _daily(days, cells=(1,))
    daily.loc[daily.index[:4], "agera5_temp_mean"] = np.nan  # only 3 valid days left
    daily.loc[daily.index[0], "chirps_precip"] = np.nan  # 6 of 7 days
    row = aggregate_to_epi_weeks(daily, CATALOG).iloc[0]
    assert np.isnan(row["agera5_temp_mean"]) and row["agera5_temp_mean_ndays"] == 3
    assert np.isnan(row["chirps_precip"]) and row["chirps_precip_ndays"] == 6
    assert row["days_observed"] == 7


def test_duplicated_daily_rows_are_rejected() -> None:
    days = [date(2026, 3, 15)]
    daily = pd.concat([_daily(days, cells=(1,))] * 2)
    with pytest.raises(QualityError, match="duplicated"):
        aggregate_to_epi_weeks(daily, CATALOG)


def test_unknown_columns_are_ignored() -> None:
    daily = _daily([date(2026, 3, 15)], cells=(1,)).assign(mystery=1.0)
    weekly = aggregate_to_epi_weeks(daily, CATALOG)
    assert "mystery" not in weekly.columns


def test_week_spanning_two_months_and_years_is_grouped_once() -> None:
    days = [date(2025, 12, 28) + timedelta(days=offset) for offset in range(7)]
    weekly = aggregate_to_epi_weeks(_daily(days, cells=(1,)), CATALOG)
    assert len(weekly) == 1
    assert (weekly.iloc[0]["epi_year"], weekly.iloc[0]["epi_week"]) == (2025, 53)
    assert weekly.iloc[0]["chirps_precip"] == pytest.approx(sum(range(1, 8)))
