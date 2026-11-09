"""Gold analytical stores: partitioned Parquet, a DuckDB file or PostgreSQL.

Tables (identical schema in every back-end):

* ``gold_daily``      key ``(cell_id, date)``
* ``gold_epiweekly``  key ``(cell_id, epi_year, epi_week)``
* ``static_covariates`` key ``cell_id``

Writes are idempotent upserts per partition (one date / one epi week), and the schema
evolves by adding columns when a new source starts contributing. PostgreSQL lives in
:mod:`climate_pipeline.storage.postgres` (column-wise upserts, one partition per grid).
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from pathlib import Path

import duckdb
import pandas as pd

from climate_pipeline.config import Settings
from climate_pipeline.domain.dates import EpiWeek
from climate_pipeline.storage.layout import GoldLayout


class OutputFormat(StrEnum):
    PARQUET = "parquet"
    DUCKDB = "duckdb"
    POSTGRES = "postgres"


class GoldTable(StrEnum):
    DAILY = "gold_daily"
    EPIWEEKLY = "gold_epiweekly"
    STATIC = "static_covariates"


def _write_parquet_atomic(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.to_parquet(temporary, index=False, compression="zstd")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class GoldStore(ABC):
    #: True when a write only touches the columns it carries (Postgres); False when it replaces
    #: the whole partition (Parquet, DuckDB), so writers must send every column.
    merges_columns = False

    def __init__(self, layout: GoldLayout) -> None:
        self.layout = layout

    @abstractmethod
    def upsert_daily(self, day: date, frame: pd.DataFrame) -> None: ...

    @abstractmethod
    def upsert_weekly(self, week: EpiWeek, frame: pd.DataFrame) -> None: ...

    @abstractmethod
    def write_static(self, frame: pd.DataFrame) -> None: ...

    @abstractmethod
    def read_daily(self, days: Sequence[date]) -> pd.DataFrame: ...

    @abstractmethod
    def query(self, sql: str, parameters: Sequence[object] | None = None) -> pd.DataFrame:
        """Run SQL against the views/tables ``gold_daily``, ``gold_epiweekly``, ``static_covariates``."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release connections (no-op for file back-ends)."""

    def stale_weeks(self) -> list[EpiWeek]:
        """Epi weeks whose daily rows changed after the weekly rows (tracked by Postgres only)."""
        return []

    def columns(self, table: GoldTable) -> list[str]:
        """Columns of ``table`` (empty when the table does not exist yet)."""
        try:
            return list(self.query(f"SELECT * FROM {table} LIMIT 0").columns)
        except Exception:  # missing Parquet folder / DuckDB table / Postgres partition
            return []

    def daily_value_counts(self, start: date, end: date, columns: Sequence[str]) -> pd.DataFrame:
        """Per day in ``[start, end]``: rows and non-null values of each existing ``columns``."""
        present = [column for column in columns if column in set(self.columns(GoldTable.DAILY))]
        counts = "".join(f', count("{column}") AS "{column}"' for column in present)
        try:
            frame = self.query(
                f'SELECT "date", count(*) AS n_rows{counts} FROM {GoldTable.DAILY} '
                'WHERE "date" BETWEEN ? AND ? GROUP BY "date" ORDER BY "date"',
                [start, end],
            )
        except Exception:
            return pd.DataFrame(columns=["date", "n_rows", *present])
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        return frame


class ParquetGoldStore(GoldStore):
    def upsert_daily(self, day: date, frame: pd.DataFrame) -> None:
        _write_parquet_atomic(frame, self.layout.daily_parquet(day))

    def upsert_weekly(self, week: EpiWeek, frame: pd.DataFrame) -> None:
        _write_parquet_atomic(frame, self.layout.weekly_parquet(week))

    def write_static(self, frame: pd.DataFrame) -> None:
        _write_parquet_atomic(frame, self.layout.static_parquet)

    def read_daily(self, days: Sequence[date]) -> pd.DataFrame:
        paths = [self.layout.daily_parquet(day) for day in days]
        paths = [str(path) for path in paths if path.exists()]
        if not paths:
            return pd.DataFrame()
        with duckdb.connect() as connection:
            return connection.execute(
                "SELECT * FROM read_parquet(?, union_by_name = true, hive_partitioning = false)",
                [paths],
            ).df()

    def _globs(self) -> dict[GoldTable, Path]:
        root = self.layout.parquet_root
        return {
            GoldTable.DAILY: root / "daily",
            GoldTable.EPIWEEKLY: root / "epiweekly",
            GoldTable.STATIC: root / "static",
        }

    def query(self, sql: str, parameters: Sequence[object] | None = None) -> pd.DataFrame:
        with duckdb.connect() as connection:
            for table, directory in self._globs().items():
                if any(directory.rglob("*.parquet")):
                    pattern = str(directory / "**" / "*.parquet")
                    connection.execute(
                        f"CREATE VIEW {table} AS SELECT * FROM read_parquet('{pattern}', "
                        "union_by_name = true, hive_partitioning = false)"
                    )
            return connection.execute(sql, parameters or []).df()


class DuckDBGoldStore(GoldStore):
    def _connect(self) -> duckdb.DuckDBPyConnection:
        self.layout.root.mkdir(parents=True, exist_ok=True)
        return duckdb.connect(str(self.layout.duckdb_path))

    @staticmethod
    def _upsert(
        connection: duckdb.DuckDBPyConnection,
        table: GoldTable,
        frame: pd.DataFrame,
        where: str | None,
        parameters: Sequence[object],
    ) -> None:
        connection.register("incoming", frame)
        try:
            connection.execute(f"CREATE TABLE IF NOT EXISTS {table} AS SELECT * FROM incoming LIMIT 0")
            existing = {row[0] for row in connection.execute(f"DESCRIBE {table}").fetchall()}
            for name, column_type, *_ in connection.execute("DESCRIBE incoming").fetchall():
                if name not in existing:
                    connection.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {column_type}')
            connection.execute("BEGIN TRANSACTION")
            try:
                if where is None:
                    connection.execute(f"DELETE FROM {table}")
                else:
                    connection.execute(f"DELETE FROM {table} WHERE {where}", list(parameters))
                connection.execute(f"INSERT INTO {table} BY NAME SELECT * FROM incoming")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.unregister("incoming")

    def upsert_daily(self, day: date, frame: pd.DataFrame) -> None:
        with self._connect() as connection:
            self._upsert(connection, GoldTable.DAILY, frame, '"date" = ?', [day])

    def upsert_weekly(self, week: EpiWeek, frame: pd.DataFrame) -> None:
        with self._connect() as connection:
            self._upsert(
                connection,
                GoldTable.EPIWEEKLY,
                frame,
                "epi_year = ? AND epi_week = ?",
                [week.year, week.week],
            )

    def write_static(self, frame: pd.DataFrame) -> None:
        with self._connect() as connection:
            connection.execute(f"DROP TABLE IF EXISTS {GoldTable.STATIC}")
            self._upsert(connection, GoldTable.STATIC, frame, None, [])

    def read_daily(self, days: Sequence[date]) -> pd.DataFrame:
        if not self.layout.duckdb_path.exists():
            return pd.DataFrame()
        with self._connect() as connection:
            tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
            if GoldTable.DAILY not in tables:
                return pd.DataFrame()
            return connection.execute(
                f'SELECT * FROM {GoldTable.DAILY} WHERE "date" IN (SELECT unnest(?::DATE[])) '
                'ORDER BY "date", cell_id',
                [list(days)],
            ).df()

    def query(self, sql: str, parameters: Sequence[object] | None = None) -> pd.DataFrame:
        with duckdb.connect(str(self.layout.duckdb_path), read_only=True) as connection:
            return connection.execute(sql, parameters or []).df()


def open_gold_store(
    layout: GoldLayout, output_format: OutputFormat | str, settings: Settings | None = None
) -> GoldStore:
    """Gold store of one grid. ``postgres`` connects with ``settings.database_url`` and
    ``settings.database_schema`` (default: the environment / ``.env``)."""
    output_format = OutputFormat(output_format)
    if output_format is OutputFormat.PARQUET:
        return ParquetGoldStore(layout)
    if output_format is OutputFormat.DUCKDB:
        return DuckDBGoldStore(layout)
    from climate_pipeline.storage.postgres import PostgresGoldStore, database_from

    return PostgresGoldStore(layout, database_from(settings))
