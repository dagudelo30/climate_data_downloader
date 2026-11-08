"""Ingestion catalog (manifest) persisted as one Parquet file per source.

One file per source lets parallel CI jobs ingest different sources without write
conflicts. Every row is keyed by ``(source, variable, date, layer)`` and stores the
product status and a monotonically increasing ``updated_ns`` timestamp. Comparing
Silver and Gold timestamps tells which Gold dates must be rebuilt.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from climate_pipeline.domain.artifacts import Layer, ProductStatus
from climate_pipeline.storage.layout import DataLake

_COLUMNS = ["source", "variable", "date", "layer", "status", "path", "origin", "updated_ns"]
_KEY = ["source", "variable", "date", "layer"]
_GOLD_PREFIX = "_gold__"

_clock_lock = threading.Lock()
_last_ns = 0


def _monotonic_ns() -> int:
    """Wall-clock nanoseconds, strictly increasing within the process."""
    global _last_ns
    with _clock_lock:
        _last_ns = max(time.time_ns(), _last_ns + 1)
        return _last_ns


@dataclass(frozen=True)
class CatalogEntry:
    source: str
    variable: str
    day: date
    layer: Layer
    status: ProductStatus
    path: str = ""
    origin: str = ""


class Catalog:
    def __init__(self, lake: DataLake) -> None:
        self._lake = lake
        self._lock = threading.Lock()

    # --- persistence ----------------------------------------------------------------
    def _load(self, source: str) -> pd.DataFrame:
        path = self._lake.manifest_path(source)
        if not path.exists():
            return pd.DataFrame({column: pd.Series(dtype="object") for column in _COLUMNS})
        return pd.read_parquet(path)

    def _save(self, source: str, frame: pd.DataFrame) -> None:
        path = self._lake.manifest_path(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            frame.to_parquet(temporary, index=False)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    # --- writes ---------------------------------------------------------------------
    def record(self, entries: Iterable[CatalogEntry]) -> None:
        rows_by_source: dict[str, list[dict]] = {}
        for entry in entries:
            rows_by_source.setdefault(entry.source, []).append(
                {
                    "source": entry.source,
                    "variable": entry.variable,
                    "date": entry.day.isoformat(),
                    "layer": entry.layer.value,
                    "status": entry.status.value,
                    "path": entry.path,
                    "origin": entry.origin,
                    "updated_ns": _monotonic_ns(),
                }
            )
        with self._lock:
            for source, rows in rows_by_source.items():
                current = self._load(source)
                incoming = pd.DataFrame(rows, columns=_COLUMNS)
                merged = pd.concat([current, incoming], ignore_index=True)
                merged = merged.drop_duplicates(subset=_KEY, keep="last")
                self._save(source, merged.sort_values(_KEY, ignore_index=True))

    # --- reads ----------------------------------------------------------------------
    def entries(self, source: str, layer: Layer | None = None) -> pd.DataFrame:
        frame = self._load(source)
        if layer is not None:
            frame = frame[frame["layer"] == layer.value]
        return frame.reset_index(drop=True)

    def status_map(self, source: str, layer: Layer = Layer.BRONZE) -> dict[tuple[str, date], ProductStatus]:
        frame = self.entries(source, layer)
        return {
            (row.variable, date.fromisoformat(row.date)): ProductStatus(row.status)
            for row in frame.itertuples(index=False)
        }

    def bronze_artifacts(self, source: str, day: date) -> pd.DataFrame:
        """Non-missing Bronze rows of ``source`` for ``day``."""
        frame = self.entries(source, Layer.BRONZE)
        frame = frame[(frame["date"] == day.isoformat()) & (frame["status"] != ProductStatus.MISSING)]
        return frame.reset_index(drop=True)

    def _sources(self) -> list[str]:
        directory = self._lake.state / "manifest"
        if not directory.exists():
            return []
        return sorted(
            path.stem for path in directory.glob("*.parquet") if not path.stem.startswith(_GOLD_PREFIX)
        )

    def silver_dates(self) -> dict[date, int]:
        """Latest Silver ``updated_ns`` per date across all sources."""
        latest: dict[date, int] = {}
        for source in self._sources():
            frame = self.entries(source, Layer.SILVER)
            frame = frame[frame["status"] != ProductStatus.MISSING]
            for row in frame.itertuples(index=False):
                day = date.fromisoformat(row.date)
                latest[day] = max(latest.get(day, 0), int(row.updated_ns))
        return latest

    def pending_gold_dates(self, grid_name: str) -> list[date]:
        """Dates whose Silver data changed after the last Gold build on ``grid_name``."""
        built = self.entries(f"{_GOLD_PREFIX}{grid_name}", Layer.GOLD)
        built_ns = {
            date.fromisoformat(row.date): int(row.updated_ns) for row in built.itertuples(index=False)
        }
        return sorted(
            day for day, silver_ns in self.silver_dates().items() if silver_ns > built_ns.get(day, -1)
        )

    def mark_gold_built(self, grid_name: str, days: Iterable[date]) -> None:
        self.record(
            CatalogEntry(
                source=f"{_GOLD_PREFIX}{grid_name}",
                variable="*",
                day=day,
                layer=Layer.GOLD,
                status=ProductStatus.FINAL,
            )
            for day in days
        )

    def summary(self) -> pd.DataFrame:
        """Row counts per source/layer/status, handy for reports."""
        frames = [self.entries(source) for source in self._sources()]
        frames = [frame for frame in frames if not frame.empty]
        if not frames:
            return pd.DataFrame(columns=["source", "layer", "status", "count"])
        combined = pd.concat(frames, ignore_index=True)
        return combined.groupby(["source", "layer", "status"]).size().rename("count").reset_index()


def path_str(path: Path | None) -> str:
    return "" if path is None else str(path)
