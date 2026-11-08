from datetime import date, timedelta

import pytest

from climate_pipeline.config import LatencyConfig, load_pipeline_config
from climate_pipeline.domain.artifacts import ProductStatus
from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.pipeline.latency import plan_source

TODAY = date(2026, 10, 27)
CHIRPS = LatencyConfig(prelim_lag_days=7, final_lag_days=52, lookback_days=90)
AGERA5 = LatencyConfig(final_lag_days=8, lookback_days=45)


def test_fixed_window_requests_every_unseen_day() -> None:
    plan = plan_source(
        "chirps",
        CHIRPS,
        ["precipitation_flux"],
        {},
        today=TODAY,
        start=date(2026, 1, 1),
        end=date(2026, 5, 31),
    )
    assert plan.total_requests == 151
    assert plan.reasons["new"] == 151


def test_dates_beyond_latency_are_not_requested() -> None:
    plan = plan_source(
        "agera5",
        AGERA5,
        ["temp_mean_2m", "temp_max_2m"],
        {},
        today=TODAY,
        start=date(2026, 10, 1),
        end=date(2026, 10, 27),
    )
    assert plan.available_until == date(2026, 10, 19)
    assert plan.window_end == date(2026, 10, 19)
    assert max(plan.days) == date(2026, 10, 19)
    assert plan.reasons["not_yet_expected"] == 8 * 2  # Oct 20..27 for two variables


def test_statuses_drive_the_decision() -> None:
    final_until = TODAY - timedelta(days=52)  # 2026-09-05
    statuses = {
        ("precipitation_flux", date(2026, 9, 1)): ProductStatus.FINAL,  # up to date
        ("precipitation_flux", date(2026, 9, 2)): ProductStatus.PRELIM,  # old prelim → upgrade
        ("precipitation_flux", date(2026, 10, 10)): ProductStatus.PRELIM,  # recent prelim → wait
        ("precipitation_flux", date(2026, 10, 11)): ProductStatus.MISSING,  # retry
    }
    plan = plan_source(
        "chirps",
        CHIRPS,
        ["precipitation_flux"],
        statuses,
        today=TODAY,
        start=date(2026, 9, 1),
        end=date(2026, 10, 20),
    )
    requested = set(plan.requests["precipitation_flux"])
    assert date(2026, 9, 1) not in requested
    assert date(2026, 9, 2) in requested and date(2026, 9, 2) <= final_until
    assert date(2026, 10, 10) not in requested
    assert date(2026, 10, 11) in requested
    assert plan.reasons["prelim_upgrade"] == 1
    assert plan.reasons["retry_missing"] == 1
    assert plan.reasons["up_to_date"] == 2


def test_incremental_uses_lookback_and_availability() -> None:
    plan = plan_source("agera5", AGERA5, ["temp_mean_2m"], {}, today=TODAY, incremental=True)
    assert plan.window_start == TODAY - timedelta(days=45)
    assert plan.window_end == TODAY - timedelta(days=8)


def test_force_refetches_final_data() -> None:
    statuses = {("temp_mean_2m", date(2026, 1, 1)): ProductStatus.FINAL}
    plan = plan_source(
        "agera5",
        AGERA5,
        ["temp_mean_2m"],
        statuses,
        today=TODAY,
        start=date(2026, 1, 1),
        end=date(2026, 1, 1),
        force=True,
    )
    assert plan.reasons["forced"] == 1


def test_window_entirely_in_the_future_is_empty_not_an_error() -> None:
    plan = plan_source(
        "agera5", AGERA5, ["temp_mean_2m"], {}, today=TODAY, start=date(2026, 10, 25), end=date(2026, 10, 27)
    )
    assert plan.total_requests == 0
    assert plan.reasons["not_yet_expected"] == 3


def test_invalid_windows() -> None:
    with pytest.raises(ConfigurationError, match="required"):
        plan_source("agera5", AGERA5, ["t"], {}, today=TODAY)
    with pytest.raises(ConfigurationError, match="before"):
        plan_source("agera5", AGERA5, ["t"], {}, today=TODAY, start=date(2026, 2, 1), end=date(2026, 1, 1))


def test_default_latencies_are_ordered() -> None:
    config = load_pipeline_config(None)
    for name, source in config.sources.items():
        latency = source.latency
        if latency.prelim_lag_days is not None:
            assert latency.prelim_lag_days <= latency.final_lag_days, name
    assert config.sources["agera5"].latency.final_lag_days in range(5, 11)
