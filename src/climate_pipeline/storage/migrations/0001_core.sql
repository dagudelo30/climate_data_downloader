-- Core schema of the climate database. Objects are unqualified: the connection's search_path
-- selects the schema (default "climate"), so tests can run in throw-away schemas.

-- Ingestion catalog: one row per (source, variable, day, layer); Gold builds are stored with
-- source = '_gold__<grid>__<format>' and variable = the source whose columns were written.
CREATE TABLE ingest_manifest (
    source      text        NOT NULL,
    variable    text        NOT NULL,
    date        date        NOT NULL,
    layer       text        NOT NULL CHECK (layer IN ('bronze', 'silver', 'gold')),
    status      text        NOT NULL CHECK (status IN ('final', 'prelim', 'missing')),
    path        text        NOT NULL DEFAULT '',
    origin      text        NOT NULL DEFAULT '',
    updated_ns  bigint      NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, layer, variable, date)
);
CREATE INDEX ingest_manifest_layer_date ON ingest_manifest (layer, date);

-- Target grids (GridSpec) and the cells of each grid that touch the AOI.
CREATE TABLE grid (
    grid_name   text PRIMARY KEY,
    crs         text               NOT NULL,
    transform   double precision[] NOT NULL CHECK (cardinality(transform) = 6),
    width       integer            NOT NULL CHECK (width > 0),
    height      integer            NOT NULL CHECK (height > 0),
    resolution  double precision GENERATED ALWAYS AS (transform[1]) STORED,
    fingerprint text               NOT NULL,
    created_at  timestamptz        NOT NULL DEFAULT now()
);

CREATE TABLE cell (
    grid_name text    NOT NULL REFERENCES grid ON DELETE CASCADE,
    cell_id   bigint  NOT NULL CHECK (cell_id >= 0),
    row_index integer NOT NULL,
    col_index integer NOT NULL,
    latitude  double precision NOT NULL,
    longitude double precision NOT NULL,
    PRIMARY KEY (grid_name, cell_id)
);

-- Variable dictionary, refreshed from the code catalog on every `db init` / `sync`.
CREATE TABLE variable (
    gold_column        text PRIMARY KEY,
    source             text     NOT NULL,
    name               text     NOT NULL,
    units              text     NOT NULL,
    valid_min          double precision NOT NULL,
    valid_max          double precision NOT NULL,
    resampling         text     NOT NULL,
    weekly_aggregation text     NOT NULL,
    min_valid_days     smallint NOT NULL,
    selected           boolean  NOT NULL,
    description        text     NOT NULL DEFAULT '',
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Gold tables: one LIST partition per grid, value columns added as sources appear.
CREATE TABLE gold_daily (
    grid_name  text   NOT NULL,
    date       date   NOT NULL,
    cell_id    bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (grid_name, date, cell_id),
    FOREIGN KEY (grid_name, cell_id) REFERENCES cell ON DELETE CASCADE
) PARTITION BY LIST (grid_name);

CREATE TABLE gold_epiweekly (
    grid_name     text     NOT NULL,
    epi_year      smallint NOT NULL,
    epi_week      smallint NOT NULL CHECK (epi_week BETWEEN 1 AND 53),
    cell_id       bigint   NOT NULL,
    week_start    date     NOT NULL,
    week_end      date     NOT NULL,
    days_observed smallint NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (grid_name, epi_year, epi_week, cell_id),
    FOREIGN KEY (grid_name, cell_id) REFERENCES cell ON DELETE CASCADE,
    CHECK (week_end = week_start + 6)
) PARTITION BY LIST (grid_name);

CREATE TABLE static_covariates (
    grid_name  text   NOT NULL,
    cell_id    bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (grid_name, cell_id),
    FOREIGN KEY (grid_name, cell_id) REFERENCES cell ON DELETE CASCADE
) PARTITION BY LIST (grid_name);

-- Monitoring views over the catalog.
CREATE VIEW ingest_status AS
SELECT source, layer, status, count(*) AS n, min(date) AS first_date, max(date) AS last_date,
       max(recorded_at) AS last_recorded_at
FROM ingest_manifest
WHERE source NOT LIKE '\_gold\_\_%'
GROUP BY source, layer, status;

CREATE VIEW source_freshness AS
SELECT source,
       max(date) FILTER (WHERE layer = 'bronze' AND status <> 'missing') AS last_bronze_date,
       max(date) FILTER (WHERE layer = 'silver' AND status <> 'missing') AS last_silver_date,
       max(date) FILTER (WHERE layer = 'silver' AND status = 'final')   AS last_final_date,
       count(*)  FILTER (WHERE layer = 'bronze' AND status = 'missing') AS bronze_missing,
       max(recorded_at) AS last_recorded_at
FROM ingest_manifest
WHERE source NOT LIKE '\_gold\_\_%'
GROUP BY source;
