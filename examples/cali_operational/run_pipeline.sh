#!/usr/bin/env bash
# Operational exercise: Cali (urban perimeter), from 1 January 2026 up to TODAY, every selected
# daily source on 100 m, 500 m, 1 km and 5 km (EPSG:9377), plus the static covariates.
# Everything (AOI, sources, dates, grids, static layers) is declared in pipeline.yaml (`run:`).
#
#   GEE_PROJECT=<your-gcp-project> bash examples/cali_operational/run_pipeline.sh
#
# Re-running only downloads what is new, missing or still preliminary. With DATABASE_URL set,
# the local lake is also loaded into PostgreSQL. Optional: OUT (default outputs/cali_operational).
set -euo pipefail
cd "$(dirname "$0")/../.."

CONFIG=examples/cali_operational/pipeline.yaml
OUT=${OUT:-outputs/cali_operational}
TODAY=$(date +%F)
mkdir -p "$OUT/reports"
rm -f "$OUT"/reports/availability_*.json

# 1) What can each source deliver today? (live probe: latest complete day, lag, epi week)
uv run climate-pipeline availability --config "$CONFIG" --output "$OUT/reports/availability_$TODAY.json"

# 2) Bronze → Silver → Gold (Parquet) on every grid, from run.start_date to today.
uv run climate-pipeline run --config "$CONFIG" --report-path "$OUT/reports/run.json" \
  || echo "run reported failures, see $OUT/reports/run.json"

# 3) Static covariates (terrain + urban form + OSM roads) on each grid, from the native layers.
uv run climate-pipeline build-static --config "$CONFIG" > "$OUT/reports/static.txt"

# 4) Coverage: per source, days in Gold, gaps and days behind real time.
uv run climate-pipeline coverage --config "$CONFIG" --report-path "$OUT/reports/coverage.json"

# 5) Optional: the same tables in PostgreSQL (DATABASE_URL, e.g. `docker compose up -d db`).
if [[ -n "${DATABASE_URL:-}" ]]; then
  uv run climate-pipeline db init --config "$CONFIG"
  uv run climate-pipeline db import-lake
  uv run climate-pipeline db status > "$OUT/reports/db_status.txt"
fi

# 6) Evidence: figures, Gold inventory per grid and the operational latency page.
uv run python examples/cali_operational/make_figures.py --data-root data --output-dir "$OUT"
uv run python examples/cali_operational/summarize_gold.py --data-root data --output-dir "$OUT"
uv run python examples/cali_operational/make_latency_report.py "$OUT/reports/availability_$TODAY.json" \
  --output "$OUT/latencia_operativa.html"
