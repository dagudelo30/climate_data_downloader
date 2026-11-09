# 🌎 Climate Data Pipeline

[![CI](https://github.com/Felipep17/climate-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/Felipep17/climate-pipeline/actions/workflows/ci.yml)
[![DB sync](https://github.com/Felipep17/climate-pipeline/actions/workflows/db-sync.yml/badge.svg)](https://github.com/Felipep17/climate-pipeline/actions/workflows/db-sync.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![PostgreSQL 17 + PostGIS](https://img.shields.io/badge/PostgreSQL-17%20%2B%20PostGIS-336791)
[![uv](https://img.shields.io/badge/deps-uv-6340ac)](https://docs.astral.sh/uv/)
[![Ruff](https://img.shields.io/badge/lint-ruff-d7ff64)](https://docs.astral.sh/ruff/)

> Desarrollado a partir de [dagudelo30/climate_data_downloader](https://github.com/dagudelo30/climate_data_downloader)
> (los notebooks originales siguen en el historial de Git, commit `6ae015c`).

Backend de datos del proyecto **Dengue.IA Cali**. Mantiene, **en tiempo real y en PostgreSQL**, las
covariables climáticas, agroclimáticas y satelitales **diarias** que alimentan los modelos de
precipitación, temperatura (media, máxima y mínima), humedad relativa, viento y radiación solar, y los
modelos de dengue. Arquitectura **Medallion** (Bronze → Silver → Gold).

## Qué entrega

* **73 variables diarias** de 10 fuentes operativas y **24 covariables estáticas** (terreno y forma
  urbana). Ninguna fuente es más gruesa que **0.1°**. Una columna por magnitud física: si dos fuentes
  miden lo mismo, queda la **más reciente**, luego la de **mejor resolución**, luego la de más sentido.
* **Gold en las resoluciones que se declaren**. El ejercicio de Cali usa **100 m, 500 m, 1 km y 5 km**
  (EPSG:9377, grillas anidadas), cada una calculada desde los datos nativos.
* Por grilla, **`gold_daily`**, **`gold_epiweekly`** (semana epidemiológica domingo-sábado con `_ndays`)
  y **`static_covariates`**, unidas por `cell_id`, en **PostgreSQL** (con el polígono de cada celda en
  PostGIS), Parquet o DuckDB.
* **Sincronización diaria contra la base**. `sync` le pregunta a PostgreSQL qué `(variable, día)` falta
  entre la fecha inicial y **hoy**, baja solo eso, actualiza Gold y reporta la cobertura: huecos y
  días de atraso respecto de lo que el proveedor ya podría haber publicado.

**Ejercicio en tiempo real:** Cali, **1 de enero de 2026 → hoy**, todas las fuentes y las cuatro
resoluciones. Las evidencias están en [`outputs/cali_operational/`](outputs/cali_operational/README.md).

![Variables clave de la última semana epidemiológica completa en 100 m, 500 m, 1 km y 5 km](outputs/cali_operational/maps/resoluciones_semana.png)

## Instalación y ejecución

Requisitos: [uv](https://docs.astral.sh/uv/) (instala Python 3.12 solo), Git y, para la base local,
[Docker Desktop](https://www.docker.com/products/docker-desktop/). Credenciales: cuenta de Earth Engine
y token del Copernicus CDS (ver [Credenciales](#credenciales)).

### macOS / Linux

```bash
# 1. uv (una vez)
curl -LsSf https://astral.sh/uv/install.sh | sh          # o: brew install uv

# 2. Proyecto
git clone https://github.com/Felipep17/climate-pipeline.git && cd climate-pipeline
uv sync                                                    # Python 3.12 + dependencias fijadas (uv.lock)
cp .env.example .env                                       # completar CDSAPI_KEY, GEE_PROJECT, DATABASE_URL
uv run earthengine authenticate                            # una vez por máquina

# 3. Base local (PostGIS en localhost:5433)
docker compose up -d db

# 4. Llenar la base desde run.start_date hasta hoy y ver qué falta
uv run climate-pipeline sync --config examples/cali_operational/pipeline.yaml
uv run climate-pipeline coverage --config examples/cali_operational/pipeline.yaml --output-format postgres

# Ejercicio completo con evidencias (lago Parquet en data/, figuras en outputs/)
bash examples/cali_operational/run_pipeline.sh
```

### Windows (PowerShell)

```powershell
# 1. uv (una vez)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # o: winget install astral-sh.uv

# 2. Proyecto
git clone https://github.com/Felipep17/climate-pipeline.git; cd climate-pipeline
uv sync
Copy-Item .env.example .env                                # completar CDSAPI_KEY, GEE_PROJECT, DATABASE_URL
uv run earthengine authenticate

# 3. Base local (Docker Desktop con WSL 2)
docker compose up -d db

# 4. Llenar la base y ver qué falta
uv run climate-pipeline sync --config examples/cali_operational/pipeline.yaml
uv run climate-pipeline coverage --config examples/cali_operational/pipeline.yaml --output-format postgres

# Ejercicio completo con evidencias
powershell -ExecutionPolicy Bypass -File examples\cali_operational\run_pipeline.ps1
```

Notas:

* Todas las dependencias (GDAL, HDF5, libpq) vienen en ruedas binarias: no hace falta instalar nada del
  sistema en macOS ni en Windows.
* Sin Docker, cualquier PostgreSQL ≥ 14 sirve (PostGIS es opcional): basta con poner su
  `DATABASE_URL` en `.env`. Sin base de datos, `run`, `coverage` y las figuras funcionan sobre el lago
  Parquet local (`run.output_format: parquet`).
* En Apple Silicon la imagen `postgis/postgis` corre emulada (amd64): funciona, un poco más lenta.
* Con Docker también se puede correr la CLI sin instalar Python:
  `docker compose run --rm pipeline sync --config examples/cali_operational/pipeline.yaml`.

### Ejercicio declarado

Todo el ejercicio está **declarado** en [`examples/cali_operational/pipeline.yaml`](examples/cali_operational/pipeline.yaml):

```yaml
run:
  aoi_path: data/auxiliary/shapefiles/cali/cali.shp
  sources: [imerg, agera5, gee, era5land, modis, viirs, smap, landsat, goes, sentinel1]
  start_date: 2026-01-01        # sync/coverage llenan todo hueco desde aquí…
                                # …hasta hoy (end_date omitido = tiempo real)
  target_grids: {100m: EPSG:9377@100, 500m: EPSG:9377@500, 1km: EPSG:9377@1000, 5km: EPSG:9377@5000}
  static: {layers: [terrain, urban, osm], year: 2026}
  max_behind_days: 3            # coverage marca "stale" a la fuente con más de 3 días de atraso
```

Cualquier opción de la CLI sobrescribe el YAML (`--start-date`, `--sources`, `--target-grid`, ...).
Un lago local ya descargado se carga a la base sin volver a descargar con
`uv run climate-pipeline db import-lake`.

## Configuración

Un YAML (`--config`) solo necesita las claves que cambia; el resto sale de `DEFAULT_SOURCES` en
`src/climate_pipeline/config.py`. Usa el mismo `--config` en todos los comandos.

| Clave | Qué controla |
|---|---|
| `run.aoi_path`, `run.sources`, `run.start_date`, `run.end_date` | el ejercicio: AOI, fuentes y ventana (sin `end_date` = hasta hoy) |
| `run.target_grids`, `run.nested` | grillas Gold `{nombre: "EPSG:XXXX@resolución"}` y si las gruesas se derivan de la más fina |
| `run.output_format`, `run.static`, `run.max_behind_days` | backend de `run` (`parquet` \| `duckdb` \| `postgres`), capas y año de las estáticas, tolerancia de atraso |
| `clip_mode` | `polygon` (por defecto) o `bbox`; ambos conservan todo píxel que toque el AOI |
| `deduplicate` | `true`: una columna por magnitud física (regla de redundancia) |
| `parallel_sources` | fuentes que se descargan a la vez (por defecto 4); cada fuente además paraleliza sus propias peticiones (`max_workers`) |
| `sources.<fuente>.latency` | `prelim_lag_days`, `final_lag_days`, `lookback_days` (ventana de reintentos y de `--incremental`) |
| `sources.<fuente>.max_workers`, `.variables`, `.options` | peticiones en paralelo, subconjunto de variables, opciones (`gee.products`, `gee.cloud_mask`, `utc_offset_hours`, `chirps.version`, …) |

Variables de entorno (`.env`): `CLIMATE_DATA_ROOT`, `DATABASE_URL`, `DATABASE_SCHEMA`, `CDSAPI_KEY`,
`GEE_PROJECT`, `GEE_SERVICE_ACCOUNT_KEY`, `MSWEP_*`.

## Comandos

| Comando | Qué hace |
|---|---|
| `sync` | Ciclo completo contra PostgreSQL: migraciones → **plan desde la base** → Bronze → Silver → Gold (todas las grillas, upsert por columnas) → semanas tocadas → estáticas de grillas nuevas → **cobertura**. Es lo que corre el cron diario. |
| `coverage` | Entre dos fechas, por fuente y grilla: días con dato, días sin píxel válido (nubes), días no publicados, **huecos** y **días de atraso** respecto de la latencia. `--fail-on-gaps` sale con 1. |
| `run` | Bronze → Silver → Gold en Parquet, DuckDB o Postgres (`--output-format`). `--incremental` usa la ventana de latencia de cada fuente; `--nested` deriva las grillas gruesas de la más fina. |
| `plan` | Qué `(variable, fecha)` se pediría según la latencia y el catálogo del backend (con Postgres: lo que le falta a la base). |
| `build-gold` / `build-static` | Reconstruye Gold de los días cuyo Silver cambió / covariables estáticas (`terrain`, `urban`, `osm`). |
| `availability` | Consulta en vivo a cada proveedor: último día completo, rezago y última semana epidemiológica completa. |
| `db init` · `db status` · `db import-lake` | Migraciones y diccionario de variables · grillas, filas y frescura por fuente · carga de un lago local. |
| `make-raster` | Tabla Gold → GeoTIFF/COG (día, semana o estática), opcionalmente remuestreado. |
| `variables` / `catalog` | Catálogo de variables y decisiones de redundancia / estado de la ingesta. |

`uv run climate-pipeline <comando> --help` documenta cada opción.

## Fuentes seleccionadas

| Fuente (`sources`) | Organismo | Variables | Qué aporta | Resolución original | Latencia | Documentación |
|---|---|---|---|---|---|---|
| `imerg` | NASA GPM | 1 | precipitación diaria (30 min → día civil de Colombia) | 0.1° (~11 km) | ~2 d (provisional) | [NASA GPM IMERG](https://gpm.nasa.gov/data/imerg) · [GEE IMERG V07](https://developers.google.com/earth-engine/datasets/catalog/NASA_GPM_L3_IMERG_V07) |
| `agera5` | Copernicus C3S | 21 | T media/máx/mín y diurna/nocturna, rocío, presión de vapor, VPD en Tmáx, HR a las 06/09/12/15/18 h, HR máx/mín, viento, radiación solar, ET₀ FAO-56, fracción de horas con lluvia | 0.1° (~11 km) | ~8 d | [Copernicus CDS AgERA5](https://cds.climate.copernicus.eu/datasets/sis-agrometeorological-indicators) |
| `era5land` | ECMWF | 13 | T de piel, presión, viento u/v, temperatura del suelo, evaporación, calor sensible, onda larga, escorrentía, LAI y albedo | 0.1° (~11 km) | ~7 d | [GEE ERA5-Land daily](https://developers.google.com/earth-engine/datasets/catalog/ECMWF_ERA5_LAND_DAILY_AGGR) |
| `goes` | NOAA GOES-East ABI | 14 | brillo IR y de vapor de agua (bandas 8-15), diferencias de bandas, mínimo diario, duración de nube fría (<235 K, <220 K), reflectancia visible diurna | 2 km (10 min → día) | ~1 d | [GEE GOES-19 MCMIP](https://developers.google.com/earth-engine/datasets/catalog/NOAA_GOES_19_MCMIPF) · [GEE GOES-16 MCMIP](https://developers.google.com/earth-engine/datasets/catalog/NOAA_GOES_16_MCMIPF) |
| `modis` | NASA | 7 | LST día y noche de Terra y Aqua, fracción nubosa, agua precipitable y aerosoles (MAIAC) | 1 km | ~3 d | [MOD11A1](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD11A1) · [MYD11A1](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MYD11A1) · [MOD09GA](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD09GA) · [MCD19A2 MAIAC](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD19A2_GRANULES) |
| `gee` | NASA / ESA | 10 | NDVI, NDWI, EVI y MNDWI de MODIS (diario) y Sentinel-2, más NDRE y BSI de Sentinel-2 | 500 m / 10-20 m | ~2-4 d | [MOD09GA](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD09GA) · [MYD09GA](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MYD09GA) · [Sentinel-2 SR](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED) · [Cloud Score+](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_CLOUD_SCORE_PLUS_V1_S2_HARMONIZED) |
| `landsat` | USGS | 1 | LST a 30 m (islas de calor) | 30 m | ~7 d | [Landsat 8 C2 L2](https://developers.google.com/earth-engine/datasets/catalog/LANDSAT_LC08_C02_T1_L2) · [Landsat 9 C2 L2](https://developers.google.com/earth-engine/datasets/catalog/LANDSAT_LC09_C02_T1_L2) |
| `sentinel1` | ESA | 3 | radar VV, VH y VH−VV (atraviesa nubes) | 10 m | ~3 d | [Sentinel-1 GRD](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S1_GRD) |
| `smap` | NASA | 2 | humedad del suelo superficial y de la zona de raíces | 9 km | ~3 d | [SMAP L4 SPL4SMGP](https://developers.google.com/earth-engine/datasets/catalog/NASA_SMAP_SPL4SMGP_008) |
| `viirs` | NASA | 1 | luces nocturnas diarias | ~460 m | ~6 d | [VIIRS VNP46A2](https://developers.google.com/earth-engine/datasets/catalog/NASA_VIIRS_002_VNP46A2) |
| `build-static` | Copernicus, ESA, Google, JRC, OSM | 24 | terreno (elevación, pendiente, orientación, TPI a 500 m y 2 km, rugosidad) y forma urbana (NDBI, LST anual, edificios, alturas, vías, intersecciones, luces) | 4-100 m | fija o anual | [Copernicus DEM](https://registry.opendata.aws/copernicus-dem/) · [Dynamic World](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1) · [ESA WorldCover](https://developers.google.com/earth-engine/datasets/catalog/ESA_WorldCover_v200) · [GHS-BUILT-S](https://developers.google.com/earth-engine/datasets/catalog/JRC_GHSL_P2023A_GHS_BUILT_S) · [GHS-BUILT-H](https://developers.google.com/earth-engine/datasets/catalog/JRC_GHSL_P2023A_GHS_BUILT_H) · [Open Buildings 2.5D](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_Research_open-buildings-temporal_v1) · [VIIRS DNB](https://developers.google.com/earth-engine/datasets/catalog/NOAA_VIIRS_DNB_MONTHLY_V1_VCMSLCFG) · [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API) |

Disponibles en el código pero **no seleccionadas** porque otra fuente mide lo mismo con menos rezago o
más fino: `chirps` ([CHIRPS v3](https://www.chc.ucsb.edu/data/chirps3), 5.5 km, ~8 d), `mswep` y
`gsmap` (0.1°, empatan con IMERG; MSWEP necesita acceso de GloH2O). `uv run climate-pipeline variables`
imprime cada decisión; `deduplicate: false` en el YAML las conserva todas.

Revisadas y no incluidas: estaciones CVC/IDEAM (puntos: flujo de variable objetivo aparte), WorldPop
(fuera de alcance), Himawari/HRRR (no cubren Colombia), NOAA CPC, NASA POWER y OISST (más gruesos que
0.1°), SPEI/TerraClimate/PERSIANN-CDR/CHIRTS (mensuales o con meses de rezago; P − ET₀ con IMERG y
AgERA5 es el insumo para un SPEI propio). ENSO entra al modelo como índice, no como grilla.

## Documentación

| Documento | Contenido |
|---|---|
| [`docs/diccionario_variables.html`](docs/diccionario_variables.html) | Cada variable: qué explica, fuente, resolución, latencia, regla semanal y para qué objetivo sirve |
| [`docs/resolucion_fuentes.html`](docs/resolucion_fuentes.html) | Fuente y resolución espacial y temporal original, de la más fina a la más gruesa |
| [`docs/flujo_proceso.html`](docs/flujo_proceso.html) | El proceso de punta a punta: cron, plan desde la base, Bronze, Silver, Gold, cobertura |
| [`docs/modelo_relacional.html`](docs/modelo_relacional.html) | Tablas de PostgreSQL y Parquet, claves, particiones y cómo se cruzan las escalas |
| [`docs/predictores_por_variable.md`](docs/predictores_por_variable.md) ([HTML](docs/predictores_por_variable.html)) | Literatura por variable objetivo y para dengue (rezagos, no linealidad, Cali), con las columnas Gold de cada predictor |
| [`outputs/cali_operational/README.md`](outputs/cali_operational/README.md) | Evidencias del ejercicio en tiempo real: filas por grilla, cobertura, consistencia entre escalas, figuras |
| [`outputs/cali_operational/latencia_operativa.html`](outputs/cali_operational/latencia_operativa.html) | Si consulto hoy, ¿hasta qué día tengo datos? Por fuente y variable |
| [`HANDOFF.md`](HANDOFF.md) | Estado actual, cómo operar, pendientes y decisiones |

`uv run python docs/make_docs.py` regenera los HTML de `docs/` desde el catálogo del código.

---

## Arquitectura

```
          cron diario (db-sync.yml) ──► sync
                                         │  ¿qué falta entre run.start_date y hoy?
                                         ▼
 CDS / GEE / CHC ──► BRONZE ──► SILVER ──────────► GOLD (por grilla) ──► coverage
                     crudo,     armonizado,        gold_daily · gold_epiweekly · static_covariates
                     grilla     QC físico,         ┌───────────────────────────────────────────┐
                     nativa     recorte            │ PostgreSQL (esquema climate, particiones) │
                                all_touched        │ Parquet / DuckDB (lago local)             │
                                                   └───────────────────────────────────────────┘
          ingest_manifest (Postgres) o state/manifest (Parquet): qué se bajó, en qué estado
```

```
src/climate_pipeline/
├── config.py      Settings (.env) + PipelineConfig: fuentes, latencias y la sección declarativa run:
├── domain/        objetos de valor puros: AOI, GridSpec (anidamiento), catálogo de variables,
│                  regla de redundancia, semanas epidemiológicas, errores
├── extractors/    upstream → Bronze: agera5, gee, gee_datasets (era5land, imerg, gsmap, modis, viirs,
│                  smap, landsat, goes, sentinel1), chirps, mswep + registro y reintentos
├── processing/    transformaciones puras: spatial (all_touched), harmonize, quality, resample,
│                  aggregation (semana epidemiológica), terrain
├── storage/       layout del lago, catálogo (Parquet | Postgres), Gold (Parquet | DuckDB | Postgres),
│                  migrations/*.sql, NetCDF atómico, make_raster
├── pipeline/      casos de uso: latency (planificador), orchestrator, gold, static + urban,
│                  availability, coverage, sync
└── cli.py         interfaz typer (comandos de arriba + grupo db)
tests/             pruebas offline con datos sintéticos + integración PostgreSQL (TEST_DATABASE_URL)
examples/          cali_operational: pipeline.yaml, run_pipeline.sh / .ps1, figuras, inventario, página de latencia
.github/workflows  ci.yml (lint, pruebas con PostGIS, imagen Docker) y db-sync.yml (cron diario)
Dockerfile, docker-compose.yml   imagen de la CLI y PostGIS local
```

### PostgreSQL

Esquema `climate`, creado por migraciones versionadas (`src/climate_pipeline/storage/migrations/`):

| Tabla | Clave | Contenido |
|---|---|---|
| `grid` | `grid_name` | CRS, transformada, tamaño y huella de cada grilla |
| `cell` | `(grid_name, cell_id)` | celdas del AOI con centroide y, con PostGIS, polígono `geom` (EPSG:4326) |
| `gold_daily` | `(grid_name, date, cell_id)` | una columna `real` por variable; particionada por grilla (`gold_daily__100m`, …) |
| `gold_epiweekly` | `(grid_name, epi_year, epi_week, cell_id)` | agregados semanales con `<columna>_ndays` |
| `static_covariates` | `(grid_name, cell_id)` | terreno, forma urbana y OSM |
| `variable` | `gold_column` | diccionario desde el catálogo del código (unidades, rango, reglas, seleccionada) |
| `ingest_manifest` | `(source, layer, variable, date)` | estado de cada descarga; vistas `ingest_status` y `source_freshness` |

* **Upsert por columnas.** Una escritura diaria solo toca las columnas que trae
  (`INSERT … ON CONFLICT DO UPDATE SET <esas columnas>`). Un runner efímero que solo tiene el Silver de
  hoy de algunas fuentes nunca borra lo que otras escribieron antes. Por eso la base es la única fuente
  de verdad y el CI no necesita bucket.
* **Gold pendiente por fuente.** El catálogo registra qué fuentes escribió cada día de Gold; un día se
  reconstruye solo si el Silver de alguna fuente es más nuevo que su Gold.
* **Carga masiva vía Arrow.** `COPY … FROM STDIN` desde un CSV de pyarrow a una tabla temporal, y
  `COPY (…) TO STDOUT` leído por pyarrow con los tipos del servidor. No hay bucles fila a fila en Python.
* **Esquema evolutivo.** Una fuente nueva agrega sus columnas (`ALTER TABLE … ADD COLUMN IF NOT EXISTS`)
  bajo un advisory lock; dos procesos nunca chocan creando la misma columna o partición.
* La consulta típica del modelo, con geometría para QGIS:

```sql
SELECT c.geom, w.*, s.static_elevation_m, s.urban_buildings_frac
FROM climate.gold_epiweekly w
JOIN climate.cell c USING (grid_name, cell_id)
JOIN climate.static_covariates s USING (grid_name, cell_id)
WHERE w.grid_name = '1km' AND w.epi_year = 2026;
```

### Lago local (Parquet)

```
data/
├── bronze/{source}/{YYYY}/{MM}/{source}[_{variable}]_{YYYY-MM-DD}.nc
├── silver/{source}/{YYYY}/{MM}/{source}_{YYYY-MM-DD}.nc
├── gold/{grilla}/grid.json                       ← definición exacta de la grilla (cell_id)
├── gold/{grilla}/parquet/daily/year=YYYY/month=MM/date=YYYY-MM-DD.parquet
├── gold/{grilla}/parquet/epiweekly/epi_year=YYYY/epi_week=WW.parquet
├── gold/{grilla}/parquet/static/covariates.parquet
└── state/manifest/{source}.parquet               ← catálogo de ingesta (un archivo por fuente)
```

Bronze guarda el dato crudo de una ventana alrededor del AOI con su procedencia (`origin`,
`product_status`, `retrieved_at`). Silver armoniza por fuente en su grilla nativa (nombres, unidades,
QC físico, recorte `all_touched`). Gold remuestrea a cada grilla y une todo en una tabla. Todas las
escrituras son **atómicas** e **idempotentes**.

## Reglas espaciales y de remuestreo

* **`all_touched=True`:** se conserva todo píxel cuya celda toque el polígono, aunque su centro quede
  fuera (`rasterio.features.geometry_mask`). Un AOI más pequeño que un píxel conserva ese píxel.
* **Grillas** (`name=EPSG:XXXX@resolución` o un ráster plantilla). En un CRS proyectado se alinean a
  múltiplos de la resolución, así que 100 m, 500 m, 1 km y 5 km anidan y se cruzan sin operaciones
  espaciales: `parent_cell_ids(fina, gruesa, cell_ids)`. Pedir otra grilla con el mismo nombre produce
  `GridMismatchError`, también contra la base.
* **Remuestreo por variable** (`rasterio.warp.reproject` directo sobre numpy):

| Tipo | Método | Semana epidemiológica |
|---|---|---|
| Flujos y acumulados (`imerg_precip`, `agera5_et0`, escorrentía) | promedio por área | **suma**, exige 7/7 días |
| Campos continuos (temperaturas, HR, viento, radiación, brillos GOES) | bilineal (promedio por área en el borde del AOI) | media, ≥4 días |
| Ópticos y radar con nubes o revisita (LST, índices, MAIAC, Sentinel-1) | promedio por área | media, ≥1 día válido |
| Fracciones (`modis_cloud_frac`, `goes_cold_cloud_frac_235`) | promedio por área | media |

Gold incluye `cell_id = fila × ancho + columna`; `make_raster` reconstruye el ráster sin interpolar
(la ida y vuelta es exacta).

## Variables y control de calidad

1. **Rangos físicos (Silver).** Un valor imposible pasa a NaN y se cuenta (`below_minimum`,
   `above_maximum`, `non_finite`); dentro de la tolerancia se recorta (HR = 100.4 % → 100 %).
2. **Consistencia (Gold diario).** Tmín ≤ Tmedia ≤ Tmáx y Tdew ≤ Tmedia con 0.5 °C de tolerancia; si
   falla, ambas columnas pasan a NaN.
3. **Invariantes.** Claves únicas `(cell_id, date)` y `(cell_id, epi_year, epi_week)`, columnas
   requeridas, valores finitos: si fallan, `QualityError` detiene el pipeline (es un bug, no un dato malo).
4. **Panel y esquema completos.** Una fila por celda del AOI y día, y en cada partición (diaria y semanal)
   **todas** las variables seleccionadas, ya remuestreadas a la grilla: lo que una fuente aún no publicó
   es NA, nunca una fila o una columna ausente.

**Semana epidemiológica** (MMWR / INS): domingo a sábado; la semana 1 contiene el 4 de enero; una semana
pertenece al año de su miércoles. Cada columna `c` trae `c_ndays`. Una suma de lluvia con días faltantes
parecería una semana seca, así que queda en NA.

## Latencia, plan y cobertura

| Fuente | Preliminar | Final | Estrategia |
|---|---|---|---|
| IMERG V07 | ~2 días | ~13 meses (Final Run en Earth Engine) | el estado de la imagen decide `prelim`/`final`; el día espera las 48 medias horas; el provisional se reemplaza cuando llega el final |
| GOES-East, SMAP | — | 1-3 días | el día espera ≥120 de 144 escaneos (GOES) u 8 de 8 (SMAP) |
| AgERA5, ERA5-Land | — | 7-8 días | no pide fechas que aún no pueden existir |
| MODIS, Sentinel-1/2, Landsat | — | 2-7 días | los días sin pasada quedan `missing` |
| VIIRS Black Marble | — | ~6 días | las imágenes vacías cuentan como no publicadas |

* El **planificador** decide por `(variable, fecha)`: `new`, `retry_missing`, `prelim_upgrade`,
  `forced`, `up_to_date`, `missing_given_up` o `not_yet_expected`. `sync` solo reintenta un `missing`
  dentro de la ventana `lookback_days` de la fuente: un día de Landsat sin pasada no se vuelve a pedir
  cada noche.
* **Fallos esperados vs inesperados.** Un dato no publicado se registra como `missing` y se reintenta.
  Un error real (credenciales, red, archivo corrupto) se aísla por tarea y fuente; la CLI sale con 1.
* **Cobertura** (`coverage`, y al final de cada `sync`): por fuente y grilla, `in_gold`,
  `no_valid_pixels`, `upstream_missing`, huecos (`not_built`, `not_requested`) y `behind_days`.

## Rendimiento

| Paso | Qué lo hace rápido |
|---|---|
| Descarga | solo `(variable, día)` que faltan según la latencia y el catálogo; **fuentes en paralelo** (`parallel_sources`), y dentro de cada fuente peticiones en paralelo (AgERA5 manda varios meses a la cola del CDS a la vez); ventanas del AOI, nunca archivos globales |
| Silver → Gold | cada día de Silver se lee **una vez** y se remuestrea a todas las grillas; `rasterio.warp.reproject` directo sobre numpy (sin el envoltorio de xarray: ~1.7× más rápido); Gold solo reconstruye los días cuyo Silver cambió, por fuente |
| PostgreSQL | carga y lectura con `COPY` vía Arrow (sin bucles fila a fila); tablas particionadas por grilla con clave `(grid_name, date, cell_id)`, que sirve tanto al upsert como a los filtros por fecha |
| Consultas sobre Parquet | particiones por día y semana con zstd y `cell_id` ordenado; DuckDB lee solo las columnas y archivos pedidos |

Referencias medidas en una Mac (Apple Silicon): un año de las 10 fuentes ~40 min (dominado por la cola
del CDS), Gold de un día en 100 m ~0.5 s, upsert de un día de 100 m en Postgres ~0.5 s, carga de un año
completo a Postgres (`db import-lake`) ~4 min.

## CI/CD (GitHub Actions)

* **`ci.yml`** (push y PR): `uv sync --locked`, `ruff check`, `ruff format --check` y `pytest` con
  cobertura (mínimo 70 %) **contra un servicio PostGIS**, de modo que las pruebas de integración de
  PostgreSQL corren en cada PR. En `main` construye la imagen Docker y la publica en
  `ghcr.io/<owner>/climate-pipeline`.
* **`db-sync.yml`** (cron 07:30 UTC = 02:30 Colombia, y manual con fechas y fuentes): `db init` →
  `plan` contra la base (al resumen del job) → `sync` → cobertura y `db status` al resumen. Se activa con
  `vars.DB_SYNC_ENABLED = true` y necesita `secrets.DATABASE_URL`, `secrets.CDSAPI_KEY`,
  `vars.GEE_PROJECT` y credenciales de Earth Engine (`secrets.GEE_SERVICE_ACCOUNT_JSON` o Workload Identity).
  La primera corrida llena toda la ventana; las siguientes solo bajan lo nuevo.

## Credenciales

Ver [`.env.example`](.env.example). CHIRPS y OpenStreetMap no requieren credenciales.

* **PostgreSQL:** `DATABASE_URL` (local: `docker compose up -d db`; en la nube, agregar
  `?sslmode=require`). `DATABASE_SCHEMA` (por defecto `climate`).
* **AgERA5:** token de https://cds.climate.copernicus.eu y aceptar una vez la licencia; sin `CDSAPI_KEY`
  se usa `~/.cdsapirc`.
* **Earth Engine:** `earthengine authenticate` en local y `GEE_PROJECT`; en CI una cuenta de servicio
  (`GEE_SERVICE_ACCOUNT_KEY`) o Workload Identity.
* **MSWEP** (opcional): acceso de GloH2O y OAuth de escritorio o cuenta de servicio.

## Desarrollo

```bash
uv sync                                               # incluye el grupo dev (pytest, ruff, matplotlib)
docker compose up -d db                               # PostGIS para las pruebas de integración
TEST_DATABASE_URL=postgresql://climate:climate@localhost:5433/climate uv run pytest
uv run pytest -m network                              # 2 pruebas contra endpoints públicos reales
uv run ruff check . && uv run ruff format --check .
```

Sin `TEST_DATABASE_URL`, las pruebas de PostgreSQL se omiten y el resto corre offline. Cada prueba de
integración crea y borra su propio esquema.

* **Agregar una fuente:** una subclase de `BaseExtractor` en `extractors/` con `@register_extractor`
  (un producto nuevo de Earth Engine es solo un `GeeDataset` en `extractors/gee_datasets.py`), sus
  `VariableSpec` (unidades, rango, remuestreo, regla semanal) y su latencia en `DEFAULT_SOURCES`. Silver,
  Gold, QC, planificador, PostgreSQL (columnas nuevas) y CLI la toman sin más cambios.
* **Agregar una grilla:** otra entrada en `run.target_grids`; `sync` la construye desde el Silver
  disponible y agrega sus estáticas.
* **Cambiar el esquema de la base:** un archivo nuevo `storage/migrations/000N_*.sql` (se aplica una
  vez, en orden, con `db init` o `sync`).
* **Qué se versiona:** código, configuración, AOI de `data/auxiliary/shapefiles/` y evidencias de
  `outputs/`. El lago (`data/bronze`, `silver`, `gold`, `state`), `.env` y tokens no se versionan.

## Decisiones y hallazgos

* **Solo fuentes diarias y operativas hasta hoy.** Se descartaron productos mensuales o con meses de
  rezago: el modelo corre cada semana y necesita covariables con ~2-3 semanas de rezago como máximo.
* **La precipitación de AgERA5 no tiene estructura espacial a escala urbana** (es la de ERA5 a ~0.25°
  replicada): la lluvia espacial viene de IMERG (o CHIRPS).
* **Sesgo frío de la temperatura de reanálisis en Cali** (orografía suavizada): las covariables de
  terreno y LST están para corregirlo en el downscaling.
* **Máscara de nubes:** sin máscara, el NDVI de píxeles nublados se ve como vegetación rala (mediana 0.12
  frente a 0.67 despejado). `cloud_mask: standard` quita nube, sombra y nube interna; MODIS diario más
  Sentinel-2 con Cloud Score+ dejan valor en ~78 % de las celdas-semana de 1 km. Lo que sigue sin dato
  queda en NA, sin imputar.
