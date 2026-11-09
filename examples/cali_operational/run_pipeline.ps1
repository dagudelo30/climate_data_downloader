# Operational exercise (Windows PowerShell): same steps as run_pipeline.sh.
# Everything (AOI, sources, dates, grids, static layers) is declared in pipeline.yaml (`run:`).
#
#   $env:GEE_PROJECT = "<your-gcp-project>"
#   powershell -ExecutionPolicy Bypass -File examples\cali_operational\run_pipeline.ps1
#
# With $env:DATABASE_URL set, the local lake is also loaded into PostgreSQL.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\..")

$Config = "examples/cali_operational/pipeline.yaml"
$Out = if ($env:OUT) { $env:OUT } else { "outputs/cali_operational" }
$Today = Get-Date -Format "yyyy-MM-dd"
New-Item -ItemType Directory -Force -Path "$Out/reports" | Out-Null
Remove-Item "$Out/reports/availability_*.json" -ErrorAction SilentlyContinue

# 1) What can each source deliver today?
uv run climate-pipeline availability --config $Config --output "$Out/reports/availability_$Today.json"

# 2) Bronze -> Silver -> Gold (Parquet) on every grid, from run.start_date to today.
uv run climate-pipeline run --config $Config --report-path "$Out/reports/run.json"
if ($LASTEXITCODE -ne 0) { Write-Warning "run reported failures, see $Out/reports/run.json" }

# 3) Static covariates (terrain + urban form + OSM roads) on each grid.
uv run climate-pipeline build-static --config $Config | Out-File -Encoding utf8 "$Out/reports/static.txt"

# 4) Coverage: per source, days in Gold, gaps and days behind real time.
uv run climate-pipeline coverage --config $Config --report-path "$Out/reports/coverage.json"

# 5) Optional: the same tables in PostgreSQL.
if ($env:DATABASE_URL) {
    uv run climate-pipeline db init --config $Config
    uv run climate-pipeline db import-lake
    uv run climate-pipeline db status | Out-File -Encoding utf8 "$Out/reports/db_status.txt"
}

# 6) Evidence: figures, Gold inventory per grid and the operational latency page.
uv run python examples/cali_operational/make_figures.py --data-root data --output-dir $Out
uv run python examples/cali_operational/summarize_gold.py --data-root data --output-dir $Out
uv run python examples/cali_operational/make_latency_report.py "$Out/reports/availability_$Today.json" `
    --output "$Out/latencia_operativa.html"
