"""Product status (final vs provisional) and frozen Gold snapshots."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from climate_pipeline.domain.artifacts import Layer, ProductStatus
from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.pipeline.provenance import create_snapshot, product_status, weekly_status
from climate_pipeline.storage.catalog import CatalogEntry, ParquetCatalog

SAT, SUN, MON = date(2026, 1, 3), date(2026, 1, 4), date(2026, 1, 5)  # 2025-W53 ends Saturday 3 Jan


def _catalog(lake) -> ParquetCatalog:
    catalog = ParquetCatalog(lake)
    catalog.record(
        [
            CatalogEntry("imerg", "*", SAT, Layer.SILVER, ProductStatus.PRELIM),
            CatalogEntry("imerg", "*", SUN, Layer.SILVER, ProductStatus.PRELIM),
            CatalogEntry("imerg", "*", MON, Layer.SILVER, ProductStatus.FINAL),
            CatalogEntry("agera5", "*", SUN, Layer.SILVER, ProductStatus.FINAL),
            CatalogEntry("landsat", "*", SUN, Layer.SILVER, ProductStatus.MISSING),
            CatalogEntry("imerg", "precip", SUN, Layer.BRONZE, ProductStatus.PRELIM),
        ]
    )
    return catalog


def test_product_status_lists_silver_status_per_day_and_source(lake) -> None:
    status = product_status(_catalog(lake))
    assert status.to_dict("records") == [
        {"date": SAT, "source": "imerg", "status": "prelim"},
        {"date": SUN, "source": "agera5", "status": "final"},
        {"date": SUN, "source": "imerg", "status": "prelim"},
        {"date": MON, "source": "imerg", "status": "final"},
    ]  # missing days are absent; Bronze rows are ignored


def test_weekly_status_counts_final_and_provisional_days(lake) -> None:
    weekly = weekly_status(product_status(_catalog(lake)))
    rows = {(r.epi_year, r.epi_week, r.source): (r.final_days, r.prelim_days) for r in weekly.itertuples()}
    assert rows == {
        (2025, 53, "imerg"): (0, 1),
        (2026, 1, "agera5"): (1, 0),
        (2026, 1, "imerg"): (1, 1),
    }
    assert weekly_status(pd.DataFrame(columns=["date", "source", "status"])).empty


def test_snapshot_freezes_gold_with_checksums(lake) -> None:
    catalog = _catalog(lake)
    partition = lake.gold / "1km" / "parquet" / "daily" / "year=2026" / "month=01" / "date=2026-01-04.parquet"
    partition.parent.mkdir(parents=True)
    pd.DataFrame({"cell_id": [1, 2], "imerg_precip": [1.0, 2.0]}).to_parquet(partition)
    (lake.gold / "1km" / "grid.json").write_text("{}")

    manifest = create_snapshot(lake, catalog, "v1")
    root = lake.root / "snapshots" / "v1"
    assert manifest["rows"] == {"1km/daily": 2}
    assert manifest["period"] == ["2026-01-03", "2026-01-05"]
    paths = {item["path"] for item in manifest["files"]}
    assert {"gold/1km/grid.json", "gold/product_status.parquet", "state/manifest/imerg.parquet"} <= paths
    assert json.loads((root / "snapshot.json").read_text())["tag"] == "v1"

    # Gold partitions are replaced atomically: the snapshot keeps the old content.
    replacement = partition.with_name("new.parquet")
    pd.DataFrame({"cell_id": [1, 2], "imerg_precip": [9.0, 9.0]}).to_parquet(replacement)
    replacement.replace(partition)
    frozen = pd.read_parquet(root / partition.relative_to(lake.root))
    assert frozen["imerg_precip"].tolist() == [1.0, 2.0]

    with pytest.raises(ConfigurationError, match="already exists"):
        create_snapshot(lake, catalog, "v1")
    with pytest.raises(ConfigurationError, match="invalid snapshot tag"):
        create_snapshot(lake, catalog, "../escape")


@pytest.mark.postgres
def test_postgres_product_status_views(pg_database) -> None:
    from climate_pipeline.storage.postgres import PostgresCatalog

    PostgresCatalog(pg_database).record(
        [
            CatalogEntry("imerg", "*", SUN, Layer.SILVER, ProductStatus.PRELIM),
            CatalogEntry("imerg", "*", MON, Layer.SILVER, ProductStatus.FINAL),
            CatalogEntry("landsat", "*", MON, Layer.SILVER, ProductStatus.MISSING),
        ]
    )
    rows = pg_database.connection().execute(
        "SELECT epi_year, epi_week, source, final_days, prelim_days FROM product_status_epiweekly"
    )
    assert rows.fetchall() == [(2026, 1, "imerg", 1, 1)]
