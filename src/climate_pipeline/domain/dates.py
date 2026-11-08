"""Calendar helpers, including epidemiological weeks.

Epidemiological weeks follow the MMWR convention used by CDC and by Colombia's
Instituto Nacional de Salud (INS):

* weeks run Sunday to Saturday;
* week 1 is the first week with at least four days in January, i.e. the week that
  contains January 4th;
* therefore a week belongs to the epidemiological year of its Wednesday, and some
  years have 53 weeks.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta


def date_range(start: date, end: date) -> list[date]:
    """Inclusive list of days between ``start`` and ``end``."""
    if end < start:
        raise ValueError(f"end date {end} is before start date {start}")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def group_by_month(days: Iterable[date]) -> dict[tuple[int, int], list[date]]:
    """Group days by ``(year, month)`` preserving chronological order."""
    groups: dict[tuple[int, int], list[date]] = defaultdict(list)
    for day in sorted(set(days)):
        groups[(day.year, day.month)].append(day)
    return dict(groups)


@dataclass(frozen=True, order=True)
class EpiWeek:
    """An epidemiological week (MMWR / INS convention)."""

    year: int
    week: int

    def __post_init__(self) -> None:
        if not 1 <= self.week <= weeks_in_epi_year(self.year):
            raise ValueError(f"epi week {self.week} does not exist in epi year {self.year}")

    @classmethod
    def from_date(cls, day: date) -> EpiWeek:
        wednesday = week_start(day) + timedelta(days=3)
        week = (wednesday.timetuple().tm_yday - 1) // 7 + 1
        return cls(year=wednesday.year, week=week)

    @property
    def start(self) -> date:
        """Sunday that opens the week."""
        return first_epi_week_start(self.year) + timedelta(weeks=self.week - 1)

    @property
    def end(self) -> date:
        """Saturday that closes the week."""
        return self.start + timedelta(days=6)

    @property
    def days(self) -> list[date]:
        return date_range(self.start, self.end)

    @property
    def label(self) -> str:
        return f"{self.year}-W{self.week:02d}"


def week_start(day: date) -> date:
    """Sunday on or before ``day``."""
    days_since_sunday = (day.weekday() + 1) % 7  # Monday=0 ... Sunday=6
    return day - timedelta(days=days_since_sunday)


def first_epi_week_start(year: int) -> date:
    """Sunday that opens epidemiological week 1 of ``year`` (the week holding Jan 4th)."""
    return week_start(date(year, 1, 4))


def weeks_in_epi_year(year: int) -> int:
    return (first_epi_week_start(year + 1) - first_epi_week_start(year)).days // 7


def epi_weeks_touched(days: Iterable[date]) -> list[EpiWeek]:
    """Distinct epidemiological weeks that contain any of ``days``."""
    return sorted({EpiWeek.from_date(day) for day in days})
