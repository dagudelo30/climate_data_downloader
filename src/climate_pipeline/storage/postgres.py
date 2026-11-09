"""PostgreSQL back-end: Gold tables, ingestion catalog and variable dictionary in one database.

Design
======

* **Schema by migrations.** ``storage/migrations/*.sql`` are applied in order (``db init``,
  ``sync``) under an advisory lock and recorded in ``schema_migrations``. Objects are
  unqualified; the connection's ``search_path`` selects the schema (``climate`` by default),
  so tests run in throw-away schemas.
* **Wide Gold tables, one LIST partition per grid.** ``gold_daily``, ``gold_epiweekly`` and
  ``static_covariates`` keep one column per variable (the shape the models consume). Columns
  are added (``ALTER TABLE … ADD COLUMN IF NOT EXISTS``) when a source starts contributing.
  Coordinates live once in ``cell`` (and, with PostGIS, the cell polygon in ``cell.geom``).
* **Column-wise upserts.** A daily write only touches the columns it carries:
  ``INSERT … ON CONFLICT DO UPDATE SET <those columns>``. A runner that only holds today's
  Silver of some sources never erases the columns of the others, which makes the database the
  single source of truth for ephemeral CI runners.
* **Bulk I/O through Arrow.** Frames go in with ``COPY … FROM STDIN (FORMAT csv)`` from a
  pyarrow CSV buffer into a temporary staging table, and come out with ``COPY (…) TO STDOUT``
  parsed by pyarrow with the column types reported by the server (no per-row Python loops).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from importlib import resources

import numpy as np
import pandas as pd
import psycopg
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
from psycopg import sql
from pyproj import Transformer

from climate_pipeline.config import Settings
from climate_pipeline.domain.artifacts import Layer
from climate_pipeline.domain.dates import EpiWeek
from climate_pipeline.domain.errors import ConfigurationError, GridMismatchError
from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.domain.variables import VariableCatalog
from climate_pipeline.storage.catalog import COLUMNS, GOLD_PREFIX, Catalog, CatalogEntry, empty_entries
from climate_pipeline.storage.gold import GoldStore, GoldTable
from climate_pipeline.storage.layout import GoldLayout

logger = logging.getLogger(__name__)

DEFAULT_SCHEMA = "climate"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
#: Columns every Gold table owns in the DDL (never value columns).
_SYSTEM_COLUMNS = {"grid_name", "updated_at"}
_COORDINATES = ("latitude", "longitude")
_KEYS: dict[GoldTable, tuple[str, ...]] = {
    GoldTable.DAILY: ("date", "cell_id"),
    GoldTable.EPIWEEKLY: ("epi_year", "epi_week", "cell_id"),
    GoldTable.STATIC: ("cell_id",),
}
#: Postgres type OID → Arrow type used to parse ``COPY … TO STDOUT`` output.
_ARROW_TYPES: dict[int, pa.DataType] = {
    16: pa.bool_(),
    20: pa.int64(),
    21: pa.int16(),
    23: pa.int32(),
    700: pa.float32(),
    701: pa.float64(),
    1082: pa.date32(),
}


def identifier(*names: str) -> sql.Identifier:
    for name in names:
        if not _IDENTIFIER.match(name):
            raise ConfigurationError(f"invalid SQL identifier {name!r}")
    return sql.Identifier(*names)


def pg_type(series: pd.Series) -> str:
    """Postgres column type for a pandas column (float32 → real, int8 → smallint, ...)."""
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"
    if pd.api.types.is_float_dtype(dtype):
        return "real" if dtype.itemsize == 4 else "double precision"
    if pd.api.types.is_integer_dtype(dtype):
        return {1: "smallint", 2: "smallint", 4: "integer"}.get(dtype.itemsize, "bigint")
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "timestamp"
    sample = series.dropna()
    if not sample.empty and isinstance(sample.iloc[0], date) and not isinstance(sample.iloc[0], datetime):
        return "date"
    return "text"


def _arrow_without_nan(frame: pd.DataFrame) -> pa.Table:
    """Arrow table where float NaN is NULL (COPY writes NULL as an empty CSV field)."""
    table = pa.Table.from_pandas(frame, preserve_index=False)
    for index, field in enumerate(table.schema):
        if pa.types.is_floating(field.type):
            column = table.column(index)
            table = table.set_column(
                index, field, pc.if_else(pc.is_nan(column), pa.scalar(None, field.type), column)
            )
    return table


def copy_frame(connection: psycopg.Connection, table: sql.Composable, frame: pd.DataFrame) -> None:
    """``COPY table (columns of frame) FROM STDIN`` through a pyarrow CSV buffer."""
    buffer = io.BytesIO()
    pacsv.write_csv(_arrow_without_nan(frame), buffer, pacsv.WriteOptions(include_header=False))
    statement = sql.SQL("COPY {} ({}) FROM STDIN (FORMAT csv)").format(
        table, sql.SQL(", ").join(identifier(column) for column in frame.columns)
    )
    with connection.cursor() as cursor, cursor.copy(statement) as copy:
        copy.write(buffer.getbuffer())


def read_frame(
    connection: psycopg.Connection, query: sql.Composable, parameters: Sequence[object] | None = None
) -> pd.DataFrame:
    """Result of ``query`` as a DataFrame, streamed with ``COPY (…) TO STDOUT`` and parsed by pyarrow."""
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("SELECT * FROM ({}) AS q LIMIT 0").format(query), parameters)
        description = cursor.description or []
        names = [column.name for column in description]
        types = {column.name: _ARROW_TYPES.get(column.type_code, pa.string()) for column in description}
        buffer = io.BytesIO()
        with cursor.copy(sql.SQL("COPY ({}) TO STDOUT (FORMAT csv)").format(query), parameters) as copy:
            for chunk in copy:
                buffer.write(chunk)
    if buffer.tell() == 0:
        return pa.schema([(name, types[name]) for name in names]).empty_table().to_pandas()
    buffer.seek(0)
    table = pacsv.read_csv(
        buffer,
        read_options=pacsv.ReadOptions(column_names=names),
        convert_options=pacsv.ConvertOptions(column_types=types, strings_can_be_null=True),
    )
    return table.to_pandas()


def migrations() -> list[tuple[str, str]]:
    """``(version, sql)`` of the packaged migrations, in order."""
    folder = resources.files("climate_pipeline.storage") / "migrations"
    files = sorted(
        (item for item in folder.iterdir() if item.name.endswith(".sql")), key=lambda item: item.name
    )
    return [(item.name.removesuffix(".sql"), item.read_text()) for item in files]


def _partition_name(table: str, grid_name: str) -> str:
    slug = re.sub(r"[^a-z0-9_]", "_", grid_name.lower())
    if slug != grid_name:  # keep distinct grids distinct ("1.km" vs "1_km")
        slug = f"{slug}_{hashlib.md5(grid_name.encode()).hexdigest()[:6]}"
    return f"{table}__{slug}"[:63]


class Database:
    """A lazily opened autocommit connection bound to one schema, plus DDL helpers."""

    def __init__(self, url: str, schema: str = DEFAULT_SCHEMA) -> None:
        if not url:
            raise ConfigurationError("DATABASE_URL is not set (postgresql://user:pass@host:5432/db)")
        identifier(schema)
        self.url = url
        self.schema = schema
        self._connection: psycopg.Connection | None = None
        self._columns: dict[str, dict[str, str]] = {}
        self._partitions: set[tuple[str, str]] = set()

    # --- connection -------------------------------------------------------------------
    def connection(self) -> psycopg.Connection:
        if self._connection is None or self._connection.closed:
            self._connection = psycopg.connect(
                self.url,
                autocommit=True,
                options=f"-c search_path={self.schema},public",
                application_name="climate-pipeline",
            )
        return self._connection

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        connection = self.connection()
        with connection.transaction():
            yield connection

    def close(self) -> None:
        if self._connection is not None and not self._connection.closed:
            self._connection.close()
        self._connection = None

    def _lock(self, connection: psycopg.Connection) -> None:
        """Serialise DDL across processes (two CI jobs creating the same column or partition)."""
        connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"climate_pipeline:{self.schema}"])

    def table(self, name: str) -> sql.Identifier:
        return identifier(self.schema, name)

    # --- schema -----------------------------------------------------------------------
    def migrate(self) -> list[str]:
        """Apply pending migrations; return the versions applied now."""
        applied_now = []
        with self.transaction() as connection:
            self._lock(connection)
            connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(identifier(self.schema)))
            connection.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} (version text PRIMARY KEY, "
                    "applied_at timestamptz NOT NULL DEFAULT now())"
                ).format(self.table("schema_migrations"))
            )
            done = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
            for version, statement in migrations():
                if version in done:
                    continue
                connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", [version])
                applied_now.append(version)
        self._columns.clear()
        if applied_now:
            logger.info("postgres: applied migrations %s in schema %s", applied_now, self.schema)
        return applied_now

    def columns(self, table: str, refresh: bool = False) -> dict[str, str]:
        """``{column: data_type}`` of ``table`` in the bound schema (cached)."""
        if refresh or table not in self._columns:
            rows = (
                self.connection()
                .execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                    [self.schema, table],
                )
                .fetchall()
            )
            self._columns[table] = dict(rows)
        return self._columns[table]

    def ensure_columns(self, table: str, types: Mapping[str, str]) -> None:
        missing = {name: kind for name, kind in types.items() if name not in self.columns(table)}
        if not missing:
            return
        with self.transaction() as connection:
            self._lock(connection)
            for name, kind in missing.items():
                connection.execute(
                    sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                        self.table(table), identifier(name), sql.SQL(kind)
                    )
                )
        logger.info("postgres: %s gained columns %s", table, sorted(missing))
        self.columns(table, refresh=True)

    def ensure_partition(self, table: str, grid_name: str) -> None:
        if (table, grid_name) in self._partitions:
            return
        with self.transaction() as connection:
            self._lock(connection)
            connection.execute(
                sql.SQL("CREATE TABLE IF NOT EXISTS {} PARTITION OF {} FOR VALUES IN ({})").format(
                    self.table(_partition_name(table, grid_name)), self.table(table), sql.Literal(grid_name)
                )
            )
        self._partitions.add((table, grid_name))

    def has_postgis(self) -> bool:
        return "geom" in self.columns("cell")

    # --- dictionary -------------------------------------------------------------------
    def sync_variables(self, catalog: VariableCatalog, selected: set[str]) -> int:
        """Mirror the code's variable catalog into ``variable`` and create the Gold columns of
        every selected variable."""
        self.ensure_gold_columns(selected)
        rows = [
            (
                spec.gold_column,
                spec.source,
                spec.name,
                spec.units,
                spec.bounds.minimum,
                spec.bounds.maximum,
                spec.resampling.value,
                spec.weekly_aggregation.value,
                spec.min_valid_days,
                spec.gold_column in selected,
                spec.description,
            )
            for spec in catalog.all()
        ]
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO variable (gold_column, source, name, units, valid_min, valid_max, resampling,
                                      weekly_aggregation, min_valid_days, selected, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (gold_column) DO UPDATE SET
                    source = EXCLUDED.source, name = EXCLUDED.name, units = EXCLUDED.units,
                    valid_min = EXCLUDED.valid_min, valid_max = EXCLUDED.valid_max,
                    resampling = EXCLUDED.resampling, weekly_aggregation = EXCLUDED.weekly_aggregation,
                    min_valid_days = EXCLUDED.min_valid_days, selected = EXCLUDED.selected,
                    description = EXCLUDED.description, updated_at = now()
                """,
                rows,
            )
        return len(rows)

    def ensure_gold_columns(self, gold_columns: Iterable[str]) -> None:
        """Every selected variable as a column of the Gold tables (``real``; weekly also
        ``<column>_ndays``), so the schema is complete before any source publishes."""
        columns = sorted(gold_columns)
        self.ensure_columns(str(GoldTable.DAILY), dict.fromkeys(columns, "real"))
        weekly = {}
        for column in columns:
            weekly[column] = "real"
            weekly[f"{column}_ndays"] = "smallint"
        self.ensure_columns(str(GoldTable.EPIWEEKLY), weekly)

    def status(self) -> dict[str, pd.DataFrame]:
        """Monitoring tables: catalog freshness, grids and Gold row counts per grid."""
        connection = self.connection()
        counts = sql.SQL(" UNION ALL ").join(
            sql.SQL(
                "SELECT {} AS gold_table, grid_name, count(*) AS rows, min({}) AS first, max({}) AS last "
                "FROM {} GROUP BY grid_name"
            ).format(sql.Literal(str(table)), sql.SQL(first), sql.SQL(first), self.table(str(table)))
            for table, first in (
                (GoldTable.DAILY, "date"),
                (GoldTable.EPIWEEKLY, "week_start"),
                (GoldTable.STATIC, "NULL::date"),
            )
        )
        return {
            "freshness": read_frame(connection, sql.SQL("SELECT * FROM source_freshness ORDER BY source")),
            "grids": read_frame(
                connection,
                sql.SQL(
                    "SELECT g.grid_name, g.crs, g.resolution, g.width, g.height, "
                    "count(c.cell_id) AS cells FROM grid g LEFT JOIN cell c USING (grid_name) "
                    "GROUP BY g.grid_name ORDER BY g.resolution"
                ),
            ),
            "gold": read_frame(connection, sql.SQL("SELECT * FROM ({}) AS t ORDER BY 1, 2").format(counts)),
        }


# --- catalog -----------------------------------------------------------------------------
class PostgresCatalog(Catalog):
    """The ingestion manifest as the ``ingest_manifest`` table."""

    def __init__(self, database: Database) -> None:
        self.db = database
        #: Sources are ingested in parallel threads that share this connection.
        self._lock = threading.RLock()

    def record(self, entries: Iterable[CatalogEntry]) -> None:
        rows = [tuple(entry.to_row()[column] for column in COLUMNS) for entry in entries]
        if not rows:
            return
        with self._lock, self.db.transaction() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO ingest_manifest (source, variable, date, layer, status, path, origin, updated_ns)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, layer, variable, date) DO UPDATE SET
                    status = EXCLUDED.status, path = EXCLUDED.path, origin = EXCLUDED.origin,
                    updated_ns = EXCLUDED.updated_ns, recorded_at = now()
                """,
                rows,
            )

    def _select(self, where: sql.Composable, parameters: Sequence[object]) -> pd.DataFrame:
        query = sql.SQL(
            "SELECT source, variable, to_char(date, 'YYYY-MM-DD') AS date, layer, status, path, origin, "
            "updated_ns FROM ingest_manifest WHERE {} ORDER BY source, layer, variable, date"
        ).format(where)
        with self._lock:
            frame = read_frame(self.db.connection(), query, parameters)
        if frame.empty:
            return empty_entries()
        frame[["path", "origin"]] = frame[["path", "origin"]].fillna("")
        return frame

    def entries(self, source: str, layer: Layer | None = None) -> pd.DataFrame:
        if layer is None:
            return self._select(sql.SQL("source = %s"), [source])
        return self._select(sql.SQL("source = %s AND layer = %s"), [source, layer.value])

    def all_entries(self) -> pd.DataFrame:
        return self._select(sql.SQL("source NOT LIKE %s"), [rf"{GOLD_PREFIX.replace('_', r'\_')}%"])

    def sources(self) -> list[str]:
        with self._lock:
            return self._sources()

    def _sources(self) -> list[str]:
        rows = (
            self.db.connection()
            .execute(
                "SELECT DISTINCT source FROM ingest_manifest WHERE source NOT LIKE %s ORDER BY source",
                [rf"{GOLD_PREFIX.replace('_', r'\_')}%"],
            )
            .fetchall()
        )
        return [row[0] for row in rows]

    def import_rows(self, frame: pd.DataFrame) -> int:
        """Bulk upsert of manifest rows keeping their ``updated_ns`` (``db import-lake``)."""
        if frame.empty:
            return 0
        rows = frame[COLUMNS].copy()
        rows["date"] = pd.to_datetime(rows["date"]).dt.date
        rows[["path", "origin"]] = rows[["path", "origin"]].fillna("")
        rows["updated_ns"] = rows["updated_ns"].astype("int64")
        with self._lock, self.db.transaction() as connection:
            connection.execute(
                "CREATE TEMP TABLE _manifest (LIKE ingest_manifest INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            copy_frame(connection, sql.Identifier("_manifest"), rows)
            connection.execute(
                "INSERT INTO ingest_manifest (source, variable, date, layer, status, path, origin, "
                "updated_ns) SELECT source, variable, date, layer, status, path, origin, updated_ns "
                "FROM _manifest "
                "ON CONFLICT (source, layer, variable, date) DO UPDATE SET status = EXCLUDED.status, "
                "path = EXCLUDED.path, origin = EXCLUDED.origin, updated_ns = EXCLUDED.updated_ns, "
                "recorded_at = now() WHERE ingest_manifest.updated_ns < EXCLUDED.updated_ns"
            )
        return len(rows)

    def silver_versions(self) -> dict[tuple[str, date], int]:
        with self._lock:
            return self._silver_versions()

    def _silver_versions(self) -> dict[tuple[str, date], int]:
        rows = (
            self.db.connection()
            .execute(
                "SELECT source, date, updated_ns FROM ingest_manifest "
                "WHERE layer = 'silver' AND status <> 'missing' AND source NOT LIKE %s",
                [rf"{GOLD_PREFIX.replace('_', r'\_')}%"],
            )
            .fetchall()
        )
        return {(source, day): int(updated) for source, day, updated in rows}

    def close(self) -> None:
        self.db.close()


# --- Gold store --------------------------------------------------------------------------
def cell_polygons_wkt(grid: GridSpec, cell_ids: np.ndarray) -> list[str]:
    """WGS84 polygons (WKT) of grid cells, for ``cell.geom``."""
    a, _, c, _, e, f = grid.transform
    rows, cols = np.divmod(cell_ids.astype("int64"), grid.width)
    x0, x1 = c + cols * a, c + (cols + 1) * a
    y0, y1 = f + rows * e, f + (rows + 1) * e
    xs = np.stack([x0, x1, x1, x0, x0], axis=1)
    ys = np.stack([y0, y0, y1, y1, y0], axis=1)
    if not grid.is_geographic:
        transformer = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
        xs, ys = transformer.transform(xs, ys)
    return [
        "POLYGON((" + ",".join(f"{x:.8f} {y:.8f}" for x, y in zip(xr, yr, strict=True)) + "))"
        for xr, yr in zip(xs, ys, strict=True)
    ]


def _to_pyformat(statement: str) -> str:
    """DuckDB-style ``?`` placeholders → psycopg ``%s`` (``%`` escaped, quoted text untouched)."""
    parts = statement.strip().rstrip(";").split("'")
    for index in range(0, len(parts), 2):  # even parts are outside single quotes
        parts[index] = parts[index].replace("%", "%%").replace("?", "%s")
    for index in range(1, len(parts), 2):
        parts[index] = parts[index].replace("%", "%%")
    return "'".join(parts)


class PostgresGoldStore(GoldStore):
    """Gold of one grid (``layout.name``) in the partitioned Postgres tables."""

    merges_columns = True

    def __init__(self, layout: GoldLayout, database: Database) -> None:
        super().__init__(layout)
        self.db = database
        self.grid_name = layout.name
        self._cells: set[int] | None = None
        self._grid_checked = False
        self._restore_grid_file()

    # --- grid and cells ---------------------------------------------------------------
    def _stored_grid(self) -> GridSpec | None:
        row = (
            self.db.connection()
            .execute("SELECT crs, transform, width, height FROM grid WHERE grid_name = %s", [self.grid_name])
            .fetchone()
        )
        if row is None:
            return None
        return GridSpec(crs=row[0], transform=tuple(row[1]), width=row[2], height=row[3])

    def _restore_grid_file(self) -> None:
        """A fresh checkout (CI runner) gets ``grid.json`` back from the database."""
        if self.layout.grid_path.exists():
            return
        stored = self._stored_grid()
        if stored is not None:
            stored.save(self.layout.grid_path)

    def _ensure_grid(self) -> GridSpec:
        if not self.layout.grid_path.exists():
            raise ConfigurationError(f"gold/{self.grid_name} has no grid.json: the grid is not resolved yet")
        local = GridSpec.load(self.layout.grid_path)
        if self._grid_checked:
            return local
        stored = self._stored_grid()
        if stored is None:
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO grid (grid_name, crs, transform, width, height, fingerprint) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (grid_name) DO NOTHING",
                    [
                        self.grid_name,
                        local.crs,
                        [float(value) for value in local.transform],
                        local.width,
                        local.height,
                        local.fingerprint(),
                    ],
                )
        elif stored.fingerprint() != local.fingerprint():
            raise GridMismatchError(
                f"database grid {self.grid_name!r} is {stored.fingerprint()} but "
                f"gold/{self.grid_name}/grid.json is {local.fingerprint()}: use another grid name"
            )
        for table in GoldTable:
            self.db.ensure_partition(str(table), self.grid_name)
        self._grid_checked = True
        return local

    def _ensure_cells(self, frame: pd.DataFrame) -> None:
        if not set(_COORDINATES) <= set(frame.columns):
            raise ConfigurationError("Gold frames written to Postgres need latitude/longitude columns")
        grid = self._ensure_grid()
        if self._cells is None:
            rows = self.db.connection().execute(
                "SELECT cell_id FROM cell WHERE grid_name = %s", [self.grid_name]
            )
            self._cells = {row[0] for row in rows}
        cells = frame[["cell_id", *_COORDINATES]].drop_duplicates("cell_id")
        cells = cells[~cells["cell_id"].isin(self._cells)]
        if cells.empty:
            return
        cell_ids = cells["cell_id"].to_numpy(dtype="int64")
        if cell_ids.min() < 0 or cell_ids.max() >= grid.width * grid.height:
            raise GridMismatchError(f"cell_id outside grid {self.grid_name}")
        rows, cols = np.divmod(cell_ids, grid.width)
        staged = pd.DataFrame(
            {
                "cell_id": cell_ids,
                "row_index": rows.astype("int32"),
                "col_index": cols.astype("int32"),
                "latitude": cells["latitude"].to_numpy("float64"),
                "longitude": cells["longitude"].to_numpy("float64"),
            }
        )
        geometry = self.db.has_postgis()
        if geometry:
            staged["wkt"] = cell_polygons_wkt(grid, cell_ids)
        with self.db.transaction() as connection:
            connection.execute(
                "CREATE TEMP TABLE _cells (cell_id bigint, row_index integer, col_index integer, "
                "latitude double precision, longitude double precision, wkt text) ON COMMIT DROP"
            )
            copy_frame(connection, sql.Identifier("_cells"), staged)
            connection.execute(
                sql.SQL(
                    "INSERT INTO cell (grid_name, cell_id, row_index, col_index, latitude, longitude"
                    "{geom_column}) SELECT %s, cell_id, row_index, col_index, latitude, longitude"
                    "{geom_value} FROM _cells "
                    "ON CONFLICT (grid_name, cell_id) DO NOTHING"
                ).format(
                    geom_column=sql.SQL(", geom" if geometry else ""),
                    geom_value=sql.SQL(", public.ST_GeomFromText(wkt, 4326)" if geometry else ""),
                ),
                [self.grid_name],
            )
        self._cells.update(int(value) for value in cell_ids)

    # --- writes -----------------------------------------------------------------------
    def _write(self, table: GoldTable, frame: pd.DataFrame, *, replace: bool = False) -> None:
        if frame.empty:
            return
        self._ensure_cells(frame)
        keys = _KEYS[table]
        values = [
            c for c in frame.columns if c not in keys and c not in _COORDINATES and c not in _SYSTEM_COLUMNS
        ]
        payload = frame[[*keys, *values]]
        types = {column: pg_type(payload[column]) for column in payload.columns}
        self.db.ensure_columns(str(table), {column: types[column] for column in values})

        target = self.db.table(str(table))
        columns = sql.SQL(", ").join(identifier(column) for column in payload.columns)
        updates = sql.SQL(", ").join(
            [sql.SQL("{0} = EXCLUDED.{0}").format(identifier(column)) for column in values]
            + [sql.SQL("updated_at = now()")]
        )
        with self.db.transaction() as connection:
            connection.execute(
                sql.SQL("CREATE TEMP TABLE _stage ({}) ON COMMIT DROP").format(
                    sql.SQL(", ").join(
                        sql.SQL("{} {}").format(identifier(column), sql.SQL(types[column]))
                        for column in payload.columns
                    )
                )
            )
            copy_frame(connection, sql.Identifier("_stage"), payload)
            if replace:
                connection.execute(
                    sql.SQL("DELETE FROM {} WHERE grid_name = %s").format(target), [self.grid_name]
                )
            connection.execute(
                sql.SQL(
                    "INSERT INTO {target} (grid_name, {columns}) SELECT %s, {columns} FROM _stage "
                    "ORDER BY {keys} ON CONFLICT (grid_name, {keys}) DO UPDATE SET {updates}"
                ).format(
                    target=target,
                    columns=columns,
                    keys=sql.SQL(", ").join(identifier(key) for key in keys),
                    updates=updates,
                ),
                [self.grid_name],
            )

    def upsert_daily(self, day: date, frame: pd.DataFrame) -> None:
        self._write(GoldTable.DAILY, frame)

    def upsert_weekly(self, week: EpiWeek, frame: pd.DataFrame) -> None:
        self._write(GoldTable.EPIWEEKLY, frame)

    def write_static(self, frame: pd.DataFrame) -> None:
        self._write(GoldTable.STATIC, frame, replace=True)

    # --- reads ------------------------------------------------------------------------
    def _value_columns(self, table: GoldTable) -> list[str]:
        fixed = {*_KEYS[table], *_SYSTEM_COLUMNS, "week_start", "week_end", "days_observed"}
        return [column for column in self.db.columns(str(table), refresh=True) if column not in fixed]

    def _view(self, table: GoldTable) -> sql.Composed:
        """``table`` of this grid with coordinates, in the column order of the file back-ends."""
        leading = {
            GoldTable.DAILY: ["cell_id", "date"],
            GoldTable.EPIWEEKLY: ["cell_id", "epi_year", "epi_week", "week_start", "week_end"],
            GoldTable.STATIC: ["cell_id"],
        }[table]
        trailing = ["days_observed"] if table is GoldTable.EPIWEEKLY else []
        selected = [
            *(sql.SQL("t.{}").format(identifier(column)) for column in leading),
            sql.SQL("c.latitude"),
            sql.SQL("c.longitude"),
            *(sql.SQL("t.{}").format(identifier(column)) for column in trailing),
            *(sql.SQL("t.{}").format(identifier(column)) for column in self._value_columns(table)),
        ]
        return sql.SQL(
            "SELECT {} FROM {} AS t JOIN {} AS c ON c.grid_name = t.grid_name AND c.cell_id = t.cell_id "
            "WHERE t.grid_name = {}"
        ).format(
            sql.SQL(", ").join(selected),
            self.db.table(str(table)),
            self.db.table("cell"),
            sql.Literal(self.grid_name),
        )

    def read_daily(self, days: Sequence[date]) -> pd.DataFrame:
        if not days:
            return pd.DataFrame()
        query = sql.SQL(
            'SELECT * FROM ({}) AS d WHERE d."date" = ANY(%s::date[]) ORDER BY d."date", d.cell_id'
        )
        frame = read_frame(self.db.connection(), query.format(self._view(GoldTable.DAILY)), [list(days)])
        return frame if not frame.empty else pd.DataFrame()

    def query(self, sql_text: str, parameters: Sequence[object] | None = None) -> pd.DataFrame:
        """SQL over ``gold_daily``, ``gold_epiweekly`` and ``static_covariates`` of this grid
        (DuckDB-style ``?`` placeholders accepted, as with the file back-ends)."""
        views = sql.SQL(", ").join(
            sql.SQL("{} AS ({})").format(sql.Identifier(str(table)), self._view(table)) for table in GoldTable
        )
        statement = sql.SQL("WITH {} {}").format(views, sql.SQL(_to_pyformat(sql_text)))
        return read_frame(self.db.connection(), statement, list(parameters or []))

    def stale_weeks(self) -> list[EpiWeek]:
        """Epi weeks whose daily rows changed after their weekly rows were written."""
        connection = self.db.connection()
        daily = connection.execute(
            "SELECT date, max(updated_at) FROM gold_daily WHERE grid_name = %s GROUP BY date",
            [self.grid_name],
        ).fetchall()
        weekly = dict(
            connection.execute(
                "SELECT week_start, min(updated_at) FROM gold_epiweekly WHERE grid_name = %s "
                "GROUP BY week_start",
                [self.grid_name],
            ).fetchall()
        )
        stale = set()
        for day, changed in daily:
            week = EpiWeek.from_date(day)
            built = weekly.get(week.start)
            if built is None or changed > built:
                stale.add(week)
        return sorted(stale)

    def close(self) -> None:
        self.db.close()


def database_from(settings: Settings | None = None) -> Database:
    """Database of ``settings`` (default: ``DATABASE_URL`` / ``DATABASE_SCHEMA`` from the environment)."""
    settings = settings or Settings()
    return Database(settings.database_url_value() or "", settings.database_schema)
