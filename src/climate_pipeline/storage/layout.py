"""Physical layout of the Medallion data lake.

::

    <root>/
      bronze/{source}/{YYYY}/{MM}/{source}[_{variable}]_{YYYY-MM-DD}.nc
      silver/{source}/{YYYY}/{MM}/{source}_{YYYY-MM-DD}.nc
      gold/{grid_name}/grid.json
      gold/{grid_name}/parquet/daily/year=YYYY/month=MM/date=YYYY-MM-DD.parquet
      gold/{grid_name}/parquet/epiweekly/epi_year=YYYY/epi_week=WW.parquet
      gold/{grid_name}/parquet/static/covariates.parquet
      gold/{grid_name}/climate.duckdb
      state/manifest/{source}.parquet
      tmp/
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from climate_pipeline.domain.dates import EpiWeek

_GRID_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def validate_grid_name(name: str) -> str:
    if not _GRID_NAME_PATTERN.match(name):
        raise ValueError(f"invalid grid name {name!r}: use letters, digits, '_', '-' or '.'")
    return name


@dataclass(frozen=True)
class DataLake:
    root: Path

    @property
    def bronze(self) -> Path:
        return self.root / "bronze"

    @property
    def silver(self) -> Path:
        return self.root / "silver"

    @property
    def gold(self) -> Path:
        return self.root / "gold"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def tmp(self) -> Path:
        path = self.root / "tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def relative(self, path: Path) -> str:
        """Catalog paths are stored relative to the lake root so the lake can be moved
        (e.g. synced between CI jobs or machines)."""
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(path)

    def resolve(self, stored: str) -> Path:
        path = Path(stored)
        return path if path.is_absolute() else self.root / path

    def bronze_path(self, source: str, day: date, variable: str | None = None) -> Path:
        stem = f"{source}_{variable}_{day.isoformat()}" if variable else f"{source}_{day.isoformat()}"
        return self.bronze / source / f"{day:%Y}" / f"{day:%m}" / f"{stem}.nc"

    def silver_path(self, source: str, day: date) -> Path:
        return self.silver / source / f"{day:%Y}" / f"{day:%m}" / f"{source}_{day.isoformat()}.nc"

    def silver_files_for(self, day: date) -> dict[str, Path]:
        """Existing Silver files of every source for ``day``."""
        files: dict[str, Path] = {}
        if not self.silver.exists():
            return files
        for source_dir in sorted(p for p in self.silver.iterdir() if p.is_dir()):
            path = self.silver_path(source_dir.name, day)
            if path.exists():
                files[source_dir.name] = path
        return files

    def manifest_path(self, source: str) -> Path:
        return self.state / "manifest" / f"{source}.parquet"

    def gold_layout(self, grid_name: str) -> GoldLayout:
        return GoldLayout(self.gold / validate_grid_name(grid_name))


@dataclass(frozen=True)
class GoldLayout:
    """Files of the Gold store built on one target grid."""

    root: Path

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def grid_path(self) -> Path:
        return self.root / "grid.json"

    @property
    def duckdb_path(self) -> Path:
        return self.root / "climate.duckdb"

    @property
    def parquet_root(self) -> Path:
        return self.root / "parquet"

    def daily_parquet(self, day: date) -> Path:
        return (
            self.parquet_root
            / "daily"
            / f"year={day:%Y}"
            / f"month={day:%m}"
            / f"date={day.isoformat()}.parquet"
        )

    def weekly_parquet(self, week: EpiWeek) -> Path:
        return self.parquet_root / "epiweekly" / f"epi_year={week.year}" / f"epi_week={week.week:02d}.parquet"

    @property
    def static_parquet(self) -> Path:
        return self.parquet_root / "static" / "covariates.parquet"
