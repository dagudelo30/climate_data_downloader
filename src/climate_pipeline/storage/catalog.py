"""Ingestion catalog (manifest): what was fetched, harmonised and built, per source and day.

Every row is keyed by ``(source, variable, date, layer)`` and stores the product status and
a monotonically increasing ``updated_ns`` timestamp. Two back-ends share the same contract:

* :class:`ParquetCatalog`: one Parquet file per source under ``state/manifest/`` (parallel CI
  jobs ingest different sources without write conflicts);
* :class:`~climate_pipeline.storage.postgres.PostgresCatalog`: the ``ingest_manifest`` table,
  so an ephemeral runner asks the database what is missing.

Gold builds are recorded **per source**: the row ``(_gold__<build key>, <source>, day, gold)``
says when the columns of ``<source>`` were last written for ``day``. A day is pending when the
Silver of any source is newer than the Gold of that same source, so a runner that only holds
the Silver of some sources never marks the others as built.
"""

from __future__ import annotations

import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

import pandas as pd

from climate_pipeline.domain.artifacts import Layer, ProductStatus
from climate_pipeline.storage.layout import DataLake

COLUMNS = ["source", "variable", "date", "layer", "status", "path", "origin", "updated_ns"]
KEY = ["source", "variable", "date", "layer"]
GOLD_PREFIX = "_gold__"
#: Gold rows written before builds were recorded per source apply to every source.
ALL_SOURCES = "*"

_clock_lock = threading.Lock()
_last_ns = 0


def monotonic_ns() -> int:
    """Wall-clock nanoseconds, strictly increasing within the process."""
    global _last_ns
    with _clock_lock:
        _last_ns = max(time.time_ns(), _last_ns + 1)
        return _last_ns


def gold_source(build_key: str) -> str:
    return f"{GOLD_PREFIX}{build_key}"


@dataclass(frozen=True)
class CatalogEntry:
    source: str
    variable: str
    day: date
    layer: Layer
    status: ProductStatus
    path: str = ""
    origin: str = ""

    def to_row(self, updated_ns: int | None = None) -> dict:
        return {
            "source": self.source,
            "variable": self.variable,
            "date": self.day.isoformat(),
            "layer": self.layer.value,
            "status": self.status.value,
            "path": self.path,
            "origin": self.origin,
            "updated_ns": updated_ns if updated_ns is not None else monotonic_ns(),
        }


def empty_entries() -> pd.DataFrame:
    return pd.DataFrame({column: pd.Series(dtype="object") for column in COLUMNS})


class Catalog(ABC):
    """Back-end independent queries over the manifest rows (``date`` as ISO strings)."""

    # --- storage primitives -----------------------------------------------------------
    @abstractmethod
    def record(self, entries: Iterable[CatalogEntry]) -> None: ...

    @abstractmethod
    def entries(self, source: str, layer: Layer | None = None) -> pd.DataFrame: ...

    @abstractmethod
    def sources(self) -> list[str]:
        """Ingested sources (Gold build bookkeeping excluded)."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release connections (no-op for file back-ends)."""

    # --- derived queries --------------------------------------------------------------
    def status_map(self, source: str, layer: Layer = Layer.BRONZE) -> dict[tuple[str, date], ProductStatus]:
        frame = self.entries(source, layer)
        return {
            (variable, date.fromisoformat(day)): ProductStatus(status)
            for variable, day, status in zip(frame["variable"], frame["date"], frame["status"], strict=True)
        }

    def bronze_artifacts(self, source: str, day: date) -> pd.DataFrame:
        """Non-missing Bronze rows of ``source`` for ``day``."""
        frame = self.entries(source, Layer.BRONZE)
        frame = frame[(frame["date"] == day.isoformat()) & (frame["status"] != ProductStatus.MISSING)]
        return frame.reset_index(drop=True)

    def silver_versions(self) -> dict[tuple[str, date], int]:
        """``updated_ns`` of the non-missing Silver of every ``(source, day)``."""
        versions: dict[tuple[str, date], int] = {}
        for source in self.sources():
            frame = self.entries(source, Layer.SILVER)
            frame = frame[frame["status"] != ProductStatus.MISSING]
            for day, updated in zip(frame["date"], frame["updated_ns"], strict=True):
                versions[(source, date.fromisoformat(day))] = int(updated)
        return versions

    def gold_versions(self, build_key: str) -> dict[tuple[str, date], int]:
        """``updated_ns`` of the last Gold build of every ``(source, day)`` on ``build_key``."""
        frame = self.entries(gold_source(build_key), Layer.GOLD)
        return {
            (variable, date.fromisoformat(day)): int(updated)
            for variable, day, updated in zip(
                frame["variable"], frame["date"], frame["updated_ns"], strict=True
            )
        }

    def pending_gold_dates(self, build_key: str) -> list[date]:
        """Days with Silver of some source newer than the Gold of that source on ``build_key``."""
        built = self.gold_versions(build_key)
        pending = {
            day
            for (source, day), silver_ns in self.silver_versions().items()
            if silver_ns > max(built.get((source, day), -1), built.get((ALL_SOURCES, day), -1))
        }
        return sorted(pending)

    def mark_gold_built(self, build_key: str, sources_by_day: Mapping[date, Iterable[str]]) -> None:
        self.record(
            CatalogEntry(gold_source(build_key), source, day, Layer.GOLD, ProductStatus.FINAL)
            for day, sources in sources_by_day.items()
            for source in sorted(set(sources))
        )

    def all_entries(self) -> pd.DataFrame:
        frames = [frame for source in self.sources() if not (frame := self.entries(source)).empty]
        return pd.concat(frames, ignore_index=True) if frames else empty_entries()

    def summary(self) -> pd.DataFrame:
        """Row counts and date range per source/layer/status, handy for reports."""
        combined = self.all_entries()
        if combined.empty:
            return pd.DataFrame(columns=["source", "layer", "status", "count", "first", "last"])
        return (
            combined.groupby(["source", "layer", "status"])["date"]
            .agg(count="size", first="min", last="max")
            .reset_index()
        )


class ParquetCatalog(Catalog):
    """One Parquet file per source under ``<lake>/state/manifest/``; paths relative to the lake."""

    def __init__(self, lake: DataLake) -> None:
        self._lake = lake
        self._lock = threading.Lock()

    def _load(self, source: str) -> pd.DataFrame:
        path = self._lake.manifest_path(source)
        return pd.read_parquet(path) if path.exists() else empty_entries()

    def _save(self, source: str, frame: pd.DataFrame) -> None:
        path = self._lake.manifest_path(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            frame.to_parquet(temporary, index=False)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def record(self, entries: Iterable[CatalogEntry]) -> None:
        rows_by_source: dict[str, list[dict]] = {}
        for entry in entries:
            rows_by_source.setdefault(entry.source, []).append(entry.to_row())
        with self._lock:
            for source, rows in rows_by_source.items():
                merged = pd.concat(
                    [self._load(source), pd.DataFrame(rows, columns=COLUMNS)], ignore_index=True
                )
                merged = merged.drop_duplicates(subset=KEY, keep="last")
                self._save(source, merged.sort_values(KEY, ignore_index=True))

    def entries(self, source: str, layer: Layer | None = None) -> pd.DataFrame:
        frame = self._load(source)
        if layer is not None:
            frame = frame[frame["layer"] == layer.value]
        return frame.reset_index(drop=True)

    def sources(self) -> list[str]:
        directory = self._lake.state / "manifest"
        if not directory.exists():
            return []
        return sorted(
            path.stem for path in directory.glob("*.parquet") if not path.stem.startswith(GOLD_PREFIX)
        )
