"""Provenance of Gold: which values are provisional, and frozen, citable snapshots.

* **Product status.** Gold mixes final and provisional products (e.g. IMERG Late Run until the
  Final Run replaces it ~13 months later). ``product_status`` lists, per day and source, the
  status of the Silver that fed Gold (``final`` | ``prelim``); ``product_status_epiweekly``
  counts final and provisional days per epi week, so a model can filter or weight them.
  File back-ends get ``gold/product_status*.parquet``; PostgreSQL has the same two views
  (migration ``0003_product_status``).
* **Snapshots.** Gold changes every day (provisional → final, late days filled in). A trained
  model needs the exact data it saw: ``create_snapshot`` freezes every Gold Parquet file,
  the product status and the catalog under ``<lake>/snapshots/<tag>/`` with a ``snapshot.json``
  (git commit, configuration hash, row counts, SHA-256 of every file). Files are hard links:
  Gold partitions are always replaced by atomic rename, never edited in place, so the snapshot
  keeps the old content at no extra disk cost.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from climate_pipeline.domain.artifacts import Layer, ProductStatus
from climate_pipeline.domain.dates import EpiWeek
from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.storage.catalog import Catalog
from climate_pipeline.storage.layout import DataLake

logger = logging.getLogger(__name__)

STATUS_COLUMNS = ["date", "source", "status"]
WEEKLY_STATUS_COLUMNS = ["epi_year", "epi_week", "source", "final_days", "prelim_days"]


def product_status(catalog: Catalog) -> pd.DataFrame:
    """Status of the Silver behind each ``(date, source)`` of Gold (missing days are absent)."""
    frames = []
    for source in catalog.sources():
        silver = catalog.entries(source, Layer.SILVER)
        silver = silver[silver["status"] != ProductStatus.MISSING]
        frames.append(pd.DataFrame({"date": silver["date"], "source": source, "status": silver["status"]}))
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame(columns=STATUS_COLUMNS)
    status = pd.concat(frames, ignore_index=True)
    status["date"] = pd.to_datetime(status["date"]).dt.date
    return status.sort_values(["date", "source"], ignore_index=True)


def weekly_status(daily: pd.DataFrame) -> pd.DataFrame:
    """Final and provisional days per epi week and source."""
    if daily.empty:
        return pd.DataFrame(columns=WEEKLY_STATUS_COLUMNS)
    weeks = [EpiWeek.from_date(day) for day in daily["date"]]
    frame = daily.assign(
        epi_year=[week.year for week in weeks],
        epi_week=[week.week for week in weeks],
        final=daily["status"].eq(ProductStatus.FINAL.value),
        prelim=daily["status"].eq(ProductStatus.PRELIM.value),
    )
    weekly = (
        frame.groupby(["epi_year", "epi_week", "source"])
        .agg(final_days=("final", "sum"), prelim_days=("prelim", "sum"))
        .reset_index()
    )
    return weekly.astype(
        {"epi_year": "int16", "epi_week": "int8", "final_days": "int8", "prelim_days": "int8"}
    )


def _write_parquet(frame: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.to_parquet(temporary, index=False, compression="zstd")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def write_product_status(lake: DataLake, catalog: Catalog) -> list[Path]:
    """``gold/product_status.parquet`` and ``gold/product_status_epiweekly.parquet`` (all grids)."""
    daily = product_status(catalog)
    return [
        _write_parquet(daily, lake.gold / "product_status.parquet"),
        _write_parquet(weekly_status(daily), lake.gold / "product_status_epiweekly.parquet"),
    ]


# --- snapshots ---------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:  # another file system, or links not supported
        shutil.copy2(source, target)


def _git_commit(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def create_snapshot(
    lake: DataLake,
    catalog: Catalog,
    tag: str | None = None,
    *,
    config_path: Path | None = None,
    archive: bool = False,
) -> dict:
    """Freeze Gold (every grid), product status and catalog under ``<lake>/snapshots/<tag>/``."""
    tag = tag or datetime.now(UTC).date().isoformat()
    if not tag.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ConfigurationError(f"invalid snapshot tag {tag!r}: use letters, digits, '-', '_' or '.'")
    root = lake.root / "snapshots" / tag
    if root.exists():
        raise ConfigurationError(f"snapshot {tag!r} already exists: {root}")
    write_product_status(lake, catalog)

    sources = [
        path
        for path in sorted(lake.gold.rglob("*"))
        if path.is_file() and path.suffix in {".parquet", ".json"}
    ]
    sources += (
        sorted((lake.state / "manifest").glob("*.parquet")) if (lake.state / "manifest").exists() else []
    )
    if not any(path.suffix == ".parquet" for path in sources):
        raise ConfigurationError(f"no Gold Parquet under {lake.gold}: build Gold first")

    files, rows = [], {}
    for path in sources:
        relative = path.relative_to(lake.root)
        target = root / relative
        _link_or_copy(path, target)
        files.append({"path": relative.as_posix(), "bytes": target.stat().st_size, "sha256": _sha256(target)})
        parts = relative.parts
        if path.suffix == ".parquet" and parts[0] == "gold" and len(parts) > 3:
            key = f"{parts[1]}/{parts[3]}"  # <grid>/<daily|epiweekly|static>
            rows[key] = rows.get(key, 0) + pq.ParquetFile(target).metadata.num_rows

    config = None
    if config_path is not None:
        shutil.copy2(config_path, root / config_path.name)
        config = {"file": config_path.name, "sha256": _sha256(config_path)}
    daily_dates = [
        pd.Timestamp(day).date()
        for day in pd.read_parquet(lake.gold / "product_status.parquet", columns=["date"])["date"]
    ]
    manifest = {
        "tag": tag,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": _git_commit(Path.cwd()),
        "config": config,
        "period": [min(daily_dates).isoformat(), max(daily_dates).isoformat()] if daily_dates else None,
        "rows": rows,
        "files": files,
        "attribution": "See ATTRIBUTION.md of the repository: derived data inherit the terms of each source.",
    }
    (root / "snapshot.json").write_text(json.dumps(manifest, indent=2, default=str))
    if archive:
        manifest["archive"] = shutil.make_archive(str(root), "zip", root_dir=root)
    logger.info("snapshot %s: %d files, %s", tag, len(files), rows)
    return manifest
