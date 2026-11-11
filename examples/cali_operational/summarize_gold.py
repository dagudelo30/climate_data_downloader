"""Evidence of the operational run: Gold inventory per nested grid.

Writes, under ``--output-dir``:

* ``tables/column_inventory.csv``: per grid and Gold column, source, units, coverage (share
  of non-null daily values), first/last date with a value;
* ``tables/static_<grid>.csv``: summary of the static covariates;
* ``qc/scale_consistency.csv``: for every coarser grid, the difference and correlation between
  its daily values and the mean of its finest-grid children;
* ``README.md``: the same in readable form, with the coverage report (gaps and days behind
  real time, ``reports/coverage.json``) and the operational availability of today.

    uv run python examples/cali_operational/summarize_gold.py --data-root data \
        --output-dir outputs/cali_operational
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from climate_pipeline.config import load_pipeline_config
from climate_pipeline.domain.grid import GridSpec, parent_cell_ids
from climate_pipeline.pipeline.orchestrator import build_variable_catalog
from climate_pipeline.storage.gold import OutputFormat, open_gold_store
from climate_pipeline.storage.layout import DataLake

ID_COLUMNS = {"cell_id", "date", "latitude", "longitude"}


def daily_glob(lake: DataLake, grid: str) -> str:
    return str(lake.gold_layout(grid).parquet_root / "daily" / "**" / "*.parquet")


def column_inventory(lake: DataLake, grid: str, catalog) -> pd.DataFrame:
    pattern = daily_glob(lake, grid)
    with duckdb.connect() as connection:
        source = f"read_parquet('{pattern}', union_by_name = true, hive_partitioning = false)"
        columns = [row[0] for row in connection.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()]
        values = [column for column in columns if column not in ID_COLUMNS]
        selects = ", ".join(
            f'avg(CASE WHEN "{c}" IS NOT NULL AND NOT isnan("{c}") THEN 1.0 ELSE 0.0 END) AS "cov__{c}", '
            f'min(CASE WHEN "{c}" IS NOT NULL AND NOT isnan("{c}") THEN "date" END) AS "first__{c}", '
            f'max(CASE WHEN "{c}" IS NOT NULL AND NOT isnan("{c}") THEN "date" END) AS "last__{c}"'
            for c in values
        )
        stats = connection.execute(f"SELECT count(*) AS rows, {selects} FROM {source}").df().iloc[0]
    rows = []
    for column in values:
        spec = catalog.by_gold_column(column) if catalog.has_gold_column(column) else None
        rows.append(
            {
                "grid": grid,
                "column": column,
                "source": spec.source if spec else "",
                "units": spec.units if spec else "",
                "coverage_pct": round(100 * float(stats[f"cov__{column}"]), 1),
                "first_date": stats[f"first__{column}"],
                "last_date": stats[f"last__{column}"],
                "description": spec.description if spec else "",
            }
        )
    return pd.DataFrame(rows)


def scale_consistency(lake: DataLake, grids: list[str], day: date, columns: list[str]) -> pd.DataFrame:
    """Coarse value vs mean of its finest-grid children (``parent_cell_ids``), per column.

    Grids are built independently from the native data, so they are not identical; a mean
    absolute difference close to 0 and a correlation close to 1 show that the scales agree.
    """
    specs = {name: GridSpec.load(lake.gold_layout(name).grid_path) for name in grids}
    finest = min(grids, key=lambda name: specs[name].resolution[0])
    fine = open_gold_store(lake.gold_layout(finest), OutputFormat.PARQUET).read_daily([day])
    rows = []
    for name in grids:
        if name == finest:
            continue
        coarse = (
            open_gold_store(lake.gold_layout(name), OutputFormat.PARQUET)
            .read_daily([day])
            .set_index("cell_id")
        )
        parents = parent_cell_ids(specs[finest], specs[name], fine["cell_id"].to_numpy())
        for column in columns:
            if column not in fine or column not in coarse:
                continue
            means = fine.assign(parent=parents).groupby("parent")[column].mean()
            joined = pd.concat(
                [means.rename("fine"), coarse[column].rename("coarse")], axis=1, join="inner"
            ).dropna()
            rows.append(
                {
                    "fine": finest,
                    "grid": name,
                    "date": day.isoformat(),
                    "column": column,
                    "cells": len(joined),
                    "mean_abs_diff": round(float((joined["fine"] - joined["coarse"]).abs().mean()), 4)
                    if len(joined)
                    else None,
                    "correlation": round(float(joined["fine"].corr(joined["coarse"])), 4)
                    if len(joined) > 2
                    else None,
                }
            )
    return pd.DataFrame(rows)


def markdown_table(frame: pd.DataFrame) -> str:
    header = "| " + " | ".join(frame.columns) + " |"
    rule = "|" + "|".join("---" for _ in frame.columns) + "|"
    body = [
        "| " + " | ".join("" if pd.isna(value) else str(value) for value in row) + " |"
        for row in frame.itertuples(index=False)
    ]
    return "\n".join([header, rule, *body])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/cali_operational"))
    parser.add_argument("--grids", nargs="+", default=["1km", "2km", "5km"])
    args = parser.parse_args()

    lake = DataLake(args.data_root)
    catalog = build_variable_catalog(load_pipeline_config(Path("examples/cali_operational/pipeline.yaml")))
    (args.output_dir / "tables").mkdir(parents=True, exist_ok=True)
    (args.output_dir / "qc").mkdir(parents=True, exist_ok=True)

    inventories, grid_rows = [], []
    for name in args.grids:
        layout = lake.gold_layout(name)
        grid = GridSpec.load(layout.grid_path)
        inventory = column_inventory(lake, name, catalog)
        inventories.append(inventory)
        store = open_gold_store(layout, OutputFormat.PARQUET)
        counts = store.query(
            "SELECT (SELECT count(*) FROM gold_daily) AS daily, (SELECT count(*) FROM gold_epiweekly) AS weekly, "
            "(SELECT count(DISTINCT cell_id) FROM gold_daily) AS cells, (SELECT min(date) FROM gold_daily) AS first, "
            "(SELECT max(date) FROM gold_daily) AS last, (SELECT count(*) FROM static_covariates) AS static_cells"
        ).iloc[0]
        static = store.query("SELECT * FROM static_covariates")
        static.drop(columns=["cell_id", "latitude", "longitude"]).describe().T.round(3).to_csv(
            args.output_dir / "tables" / f"static_{name}.csv"
        )
        grid_rows.append(
            {
                "grid": name,
                "resolution_m": grid.resolution[0],
                "shape (h×w)": f"{grid.height}×{grid.width}",
                "cells_in_aoi": int(counts["cells"]),
                "daily_rows": int(counts["daily"]),
                "epiweekly_rows": int(counts["weekly"]),
                "first_day": str(counts["first"])[:10],
                "last_day": str(counts["last"])[:10],
                "daily_columns": len(inventory),
                "static_columns": static.shape[1] - 3,
            }
        )
    inventory = pd.concat(inventories, ignore_index=True)
    inventory.to_csv(args.output_dir / "tables" / "column_inventory.csv", index=False)
    grids = pd.DataFrame(grid_rows)

    sample_day = date.fromisoformat(grid_rows[0]["first_day"]) if grid_rows else date.today()
    nesting = scale_consistency(
        lake,
        args.grids,
        sample_day,
        ["imerg_precip", "agera5_temp_max", "goes_cold_cloud_frac_235", "modis_cloud_frac"],
    )
    nesting.to_csv(args.output_dir / "qc" / "scale_consistency.csv", index=False)

    template = args.grids[0]
    by_source = (
        inventory[inventory["grid"] == template]
        .groupby("source")
        .agg(
            columns=("column", "count"),
            mean_coverage_pct=("coverage_pct", "mean"),
            last_date=("last_date", "max"),
        )
        .reset_index()
        .round({"mean_coverage_pct": 1})
        .assign(last_date=lambda frame: frame["last_date"].astype(str).str[:10])
    )
    availability_files = sorted((args.output_dir / "reports").glob("availability_*.json"))
    availability = json.loads(availability_files[-1].read_text()) if availability_files else None

    lines = [
        "# Evidencias — ejercicio en tiempo real de Cali (fuentes diarias seleccionadas, tres resoluciones)",
        "",
        f"Periodo: **{grid_rows[0]['first_day']} → {grid_rows[0]['last_day']}** (de `run.start_date` a hoy). Generado por "
        "`examples/cali_operational/run_pipeline.sh` (o `.ps1`). Las fuentes se descargan en su grilla nativa y Gold "
        "se genera en cada resolución pedida (**1 km, 2 km y 5 km**) directamente desde esos datos. Cada "
        "partición diaria y semanal trae **todas** las variables seleccionadas: NA donde la fuente aún no publicó.",
        "",
        "## Tablas Gold por grilla",
        "",
        markdown_table(grids),
        "",
        "Rutas: `data/gold/<grilla>/parquet/{daily,epiweekly,static}/` (y las mismas tablas en PostgreSQL con "
        "`climate-pipeline db import-lake` o `sync`).",
        "",
        "## Consistencia entre escalas",
        "",
        "Cada grilla se calcula directamente desde los datos nativos a su resolución. Para comprobar que las escalas "
        "coinciden se compara cada celda gruesa con el promedio de sus celdas de la grilla más fina (`parent_cell_ids`):",
        "",
        markdown_table(nesting.round(8)),
        "",
        f"## Cobertura por fuente (grilla {template})",
        "",
        "`mean_coverage_pct` = porcentaje de valores diarios no nulos (las fuentes ópticas bajan por nubes; "
        "los días aún no publicados quedan como NA).",
        "",
        markdown_table(by_source),
        "",
        "Detalle por columna: [`tables/column_inventory.csv`](tables/column_inventory.csv).",
    ]
    figures = [
        (
            "maps/resoluciones_semana.png",
            "Variables clave de la última semana completa en 100 m, 500 m, 1 km y 5 km",
        ),
        ("maps/estaticas_resoluciones.png", "Terreno y forma urbana en las cuatro resoluciones"),
        (
            "animations/semanal_resoluciones.gif",
            "Precipitación IMERG y Tmáx AgERA5 semana a semana, cuatro resoluciones",
        ),
        ("maps/variables_1km.png", "Una variable por familia en la última semana completa, 1 km"),
    ]
    present = [(path, caption) for path, caption in figures if (args.output_dir / path).exists()]
    if present:
        lines += ["", "## Figuras", "", "Generadas con `examples/cali_operational/make_figures.py`.", ""]
        for path, caption in present:
            lines += [f"**{caption}**", "", f"![{caption}]({path})", ""]
    coverage_path = args.output_dir / "reports" / "coverage.json"
    if coverage_path.exists():
        from climate_pipeline.pipeline.coverage import coverage_table

        report = json.loads(coverage_path.read_text())[template]
        lines += [
            "",
            f"## Cobertura del periodo {report['start']} → {report['end']} (grilla {template})",
            "",
            "`climate-pipeline coverage`: días con dato (`in_gold`), días sin píxel válido (nubes / sin pasada), "
            "días que el proveedor no publicó, huecos (`not_built`, `not_requested`) y cuántos días va atrás "
            "cada fuente respecto de lo que ya podría estar publicado (`behind_days`).",
            "",
            markdown_table(coverage_table(report)),
        ]
    if availability:
        table = pd.DataFrame(
            [
                {
                    "source": item["source"],
                    "latest_day": item["latest_day"],
                    "lag_days": item["lag_days"],
                    "last_complete_epiweek": item["last_complete_epiweek"],
                    "status": item["status"],
                    "method": item["method"],
                }
                for item in availability["sources"]
            ]
        )
        lines += [
            "",
            f"## Disponibilidad operativa al {availability['today']}",
            "",
            markdown_table(table),
            "",
            f"Todas las fuentes completas hasta **{availability['all_sources_complete_through']}** "
            f"(semana epidemiológica **{availability['all_sources_last_complete_epiweek']}**).",
        ]
    (args.output_dir / "README.md").write_text("\n".join(lines) + "\n")
    print(grids.to_string(index=False))
    print(nesting.to_string(index=False))


if __name__ == "__main__":
    main()
