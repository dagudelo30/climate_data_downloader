#!/usr/bin/env bash
# Reproducible example: Cali (urban perimeter, 335 barrios), January-May 2026.
#   Sources: CHIRPS (precipitation), AgERA5 (10 variables), NDVI/NDWI from GEE (MODIS Terra+Aqua
#            and Sentinel-2 + Cloud Score+)
#   Gold:    two grids, 1 km and 5 km (MAGNA-SIRGAS 2018 / Origen-Nacional, EPSG:9377),
#            daily + epidemiological-week tables, in Parquet and DuckDB
#
#   GEE_PROJECT=<your-gcp-project> bash examples/cali_2026/run_pipeline.sh
#
# Optional overrides: CLIMATE_DATA_ROOT (data lake, default data/) and OUT (evidence dir).
# The cloud-mask study behind `cloud_mask: standard` is a separate, slower analysis:
#   uv run python examples/cali_2026/gee_cloud_mask_diagnostic.py --output-dir "$OUT"
set -euo pipefail
cd "$(dirname "$0")/../.."

AOI=data/auxiliary/shapefiles/cali/cali.shp
CONFIG=examples/cali_2026/pipeline.yaml
DATA_ROOT=${CLIMATE_DATA_ROOT:-data}
OUT=${OUT:-outputs/cali_2026-01_2026-05}
GRIDS=(--target-grid "1km=EPSG:9377@1000" --target-grid "5km=EPSG:9377@5000")
export CLIMATE_DATA_ROOT=$DATA_ROOT
mkdir -p "$OUT/reports" "$OUT/rasters"

# 1) Bronze -> Silver -> Gold (Parquet). Exit code 1 means some source reported failures:
#    keep going, the evidence step below decides whether the result is acceptable.
uv run climate-pipeline run \
  --aoi-path "$AOI" --sources chirps,agera5,gee \
  --start-date 2026-01-01 --end-date 2026-05-31 \
  "${GRIDS[@]}" --output-format parquet --config "$CONFIG" \
  --report-path "$OUT/reports/run_parquet.json" || echo "run reported failures, see $OUT/reports/run_parquet.json"

# 2) Same Gold materialised in DuckDB (reuses Silver, no downloads).
uv run climate-pipeline build-gold \
  --aoi-path "$AOI" "${GRIDS[@]}" --output-format duckdb --config "$CONFIG" \
  --start-date 2026-01-01 --end-date 2026-05-31 \
  --report-path "$OUT/reports/build_gold_duckdb.json" > /dev/null

# 3) Static covariates (Copernicus DEM GLO-90) on both grids and both back-ends.
for grid in 1km 5km; do
  for format in parquet duckdb; do
    uv run climate-pipeline build-static --aoi-path "$AOI" --grid-name "$grid" --output-format "$format" \
      > "$OUT/reports/static_${grid}_${format}.txt"
  done
done

# 4) Inverse operation: Gold table -> Cloud-Optimized GeoTIFF.
uv run climate-pipeline make-raster --grid-name 1km --variable agera5_temp_mean --day 2026-03-15 \
  --output "$OUT/rasters/agera5_temp_mean_1km_2026-03-15.tif"
uv run climate-pipeline make-raster --grid-name 5km --variable chirps_precip --epi-week 2026-W14 \
  --output "$OUT/rasters/chirps_precip_5km_2026-W14.tif"
uv run climate-pipeline make-raster --grid-name 1km --variable gee_ndvi --epi-week 2026-W10 \
  --output-format duckdb --output "$OUT/rasters/gee_ndvi_1km_2026-W10_from_duckdb.tif"
uv run climate-pipeline make-raster --grid-name 1km --variable static_elevation_m \
  --output "$OUT/rasters/static_elevation_1km.tif"

# 5) Idempotency evidence: a second plan must find nothing to download.
uv run climate-pipeline plan --aoi-path "$AOI" --sources chirps,agera5,gee --no-incremental \
  --start-date 2026-01-01 --end-date 2026-05-31 --config "$CONFIG" > "$OUT/reports/plan_after_runs.json"

# 6) Epidemiological-week maps of every Gold field (GIFs, 1 km vs 5 km) over OpenStreetMap.
uv run python examples/cali_2026/make_weekly_gif.py --output-dir "$OUT" --data-root "$DATA_ROOT"

# 7) Evidence: maps, time series, QC checks (exit 1 if any fails) and table extracts.
uv run python examples/cali_2026/make_evidence.py --output-dir "$OUT" --data-root "$DATA_ROOT"
