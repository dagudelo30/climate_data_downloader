#!/usr/bin/env bash
# Operational example: Cali (urban perimeter), from epi week 2026-W35 up to TODAY, with every
# selected daily climate / satellite source. Sources are downloaded as they come (native grid)
# and Gold is built at each requested resolution directly from the native data:
#
#   GRIDS: 100 m, 500 m, 1 km and 5 km (EPSG:9377). Change them freely (name=CRS@resolution).
#   Add --nested to derive the coarser grids as exact block means of the finest one instead.
#
#   GEE_PROJECT=<your-gcp-project> bash examples/cali_operational/run_pipeline.sh
#
# Optional: START (default 2026-08-30, a Sunday), END (default today), CLIMATE_DATA_ROOT
# (default data/operational), OUT (default outputs/cali_operational). MSWEP is included only
# when MSWEP_OAUTH_CLIENT / MSWEP_SERVICE_ACCOUNT_KEY are configured (GloH2O access).
set -euo pipefail
cd "$(dirname "$0")/../.."

AOI=data/auxiliary/shapefiles/cali/cali.shp
CONFIG=examples/cali_operational/pipeline.yaml
START=${START:-2026-08-30}
END=${END:-$(date +%F)}
export CLIMATE_DATA_ROOT=${CLIMATE_DATA_ROOT:-data/operational}
OUT=${OUT:-outputs/cali_operational}
SOURCES=imerg,agera5,gee,era5land,modis,viirs,smap,landsat,goes,sentinel1
if [[ -n "${MSWEP_OAUTH_CLIENT:-}${MSWEP_SERVICE_ACCOUNT_KEY:-}" ]]; then SOURCES="$SOURCES,mswep"; fi
GRIDS=(--target-grid "100m=EPSG:9377@100" --target-grid "500m=EPSG:9377@500"
  --target-grid "1km=EPSG:9377@1000" --target-grid "5km=EPSG:9377@5000")
mkdir -p "$OUT/reports"

# 1) What can each source deliver today? (live probe: latest complete day, lag, epi week)
uv run climate-pipeline availability --aoi-path "$AOI" --config "$CONFIG" \
  --sources "$SOURCES" --output "$OUT/reports/availability_$(date +%F).json"

# 2) Bronze → Silver for every source, Gold on every requested grid (Parquet). Dates the providers
#    have not published yet are recorded as `missing` and retried by later runs.
uv run climate-pipeline run --aoi-path "$AOI" --config "$CONFIG" --sources "$SOURCES" \
  --start-date "$START" --end-date "$END" "${GRIDS[@]}" --output-format parquet \
  --report-path "$OUT/reports/run.json" || echo "run reported failures, see $OUT/reports/run.json"

# 3) Static covariates (terrain + urban form + OSM roads), computed on each grid from the native layers.
uv run climate-pipeline build-static --aoi-path "$AOI" --grid-name 100m --grid-name 500m --grid-name 1km --grid-name 5km \
  --layers terrain,urban,osm --year "$((${END:0:4} - 1))" > "$OUT/reports/static.txt"

# 4) Evidence: Gold inventory per grid (columns, coverage, last date with data)
#    and the operational latency page.
uv run python examples/cali_operational/make_figures.py --data-root "$CLIMATE_DATA_ROOT" --output-dir "$OUT"
uv run python examples/cali_operational/summarize_gold.py --data-root "$CLIMATE_DATA_ROOT" --output-dir "$OUT"
uv run python examples/cali_operational/make_latency_report.py "$OUT/reports/availability_$(date +%F).json" \
  --output "$OUT/latencia_operativa.html"
