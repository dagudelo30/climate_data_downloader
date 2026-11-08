from datetime import date, timedelta

import pytest

from climate_pipeline.domain.dates import (
    EpiWeek,
    date_range,
    epi_weeks_touched,
    first_epi_week_start,
    group_by_month,
    week_start,
    weeks_in_epi_year,
)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        # Reference values from the MMWR / INS calendars.
        (date(2026, 1, 1), (2025, 53)),  # Thursday: Jan 1-3 belong to the last 2025 week
        (date(2026, 1, 3), (2025, 53)),
        (date(2026, 1, 4), (2026, 1)),  # Sunday Jan 4th always opens week 1 when it is a Sunday
        (date(2024, 1, 1), (2024, 1)),  # week Sun 2023-12-31 .. Sat 2024-01-06 has 6 days in 2024
        (date(2023, 12, 31), (2024, 1)),
        (date(2022, 1, 1), (2021, 52)),  # Saturday
        (date(2021, 1, 1), (2020, 53)),  # 2020 had 53 epi weeks
        (date(2023, 1, 1), (2023, 1)),  # Sunday
        (date(2026, 5, 31), (2026, 22)),
        (date(2026, 3, 15), (2026, 11)),
    ],
)
def test_epi_week_reference_values(day: date, expected: tuple[int, int]) -> None:
    week = EpiWeek.from_date(day)
    assert (week.year, week.week) == expected


@pytest.mark.parametrize(("year", "weeks"), [(2020, 53), (2021, 52), (2024, 52), (2025, 53), (2026, 52)])
def test_weeks_in_epi_year(year: int, weeks: int) -> None:
    assert weeks_in_epi_year(year) == weeks


def test_every_day_falls_inside_its_week_and_weeks_are_contiguous() -> None:
    day, previous = date(2015, 1, 1), None
    while day <= date(2030, 12, 31):
        week = EpiWeek.from_date(day)
        assert week.start <= day <= week.end
        assert week.start.weekday() == 6  # Sunday
        assert week.end.weekday() == 5  # Saturday
        assert len(week.days) == 7
        if previous is not None and previous != week:
            assert week.start == previous.end + timedelta(days=1)
            assert (week.year, week.week) in {(previous.year, previous.week + 1), (previous.year + 1, 1)}
        previous = week
        day += timedelta(days=1)


def test_week_one_contains_january_fourth() -> None:
    for year in range(2000, 2040):
        assert EpiWeek(year, 1).start <= date(year, 1, 4) <= EpiWeek(year, 1).end
        assert first_epi_week_start(year).weekday() == 6


def test_invalid_week_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not exist"):
        EpiWeek(2026, 53)
    with pytest.raises(ValueError, match="does not exist"):
        EpiWeek(2026, 0)


def test_label_and_touched_weeks() -> None:
    assert EpiWeek(2026, 4).label == "2026-W04"
    weeks = epi_weeks_touched(date_range(date(2025, 12, 30), date(2026, 1, 10)))
    assert [week.label for week in weeks] == ["2025-W53", "2026-W01"]


def test_week_start_is_previous_sunday() -> None:
    assert week_start(date(2026, 3, 18)) == date(2026, 3, 15)  # Wednesday → Sunday
    assert week_start(date(2026, 3, 15)) == date(2026, 3, 15)


def test_date_helpers() -> None:
    assert len(date_range(date(2026, 1, 1), date(2026, 5, 31))) == 151
    with pytest.raises(ValueError, match="before"):
        date_range(date(2026, 2, 1), date(2026, 1, 1))
    groups = group_by_month([date(2026, 2, 2), date(2026, 1, 31), date(2026, 2, 1)])
    assert groups == {(2026, 1): [date(2026, 1, 31)], (2026, 2): [date(2026, 2, 1), date(2026, 2, 2)]}
