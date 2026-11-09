# 🌎 Climate Data Pipeline

[![CI](https://github.com/Felipep17/climate-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/Felipep17/climate-pipeline/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
[![uv](https://img.shields.io/badge/deps-uv-6340ac)](https://docs.astral.sh/uv/)
[![Ruff](https://img.shields.io/badge/lint-ruff-d7ff64)](https://docs.astral.sh/ruff/)

> Desarrollado a partir de [dagudelo30/climate_data_downloader](https://github.com/dagudelo30/climate_data_downloader)
> (descargadores originales en notebooks; su historial se conserva en este repositorio).

Pipeline de producción que arma, para el proyecto **Dengue.IA Cali**, las covariables climáticas,
agroclimáticas y satelitales **diarias y al día de hoy** que alimentan los modelos de
precipitación, temperatura (media, máxima y mínima), humedad relativa, viento y radiación
solar. Usa una **arquitectura Medallion** (Bronze → Silver → Gold).

## Qué entrega

* **73 variables diarias seleccionadas** de 10 fuentes y **24 covariables estáticas** (terreno y
  forma urbana). Ninguna fuente es más gruesa que **0.1°** (AgERA5).
* **Una columna por magnitud física.** Si dos fuentes miden lo mismo, queda primero la **más
  reciente**, luego la de **mejor resolución** y luego la de más sentido.
* **Gold a la resolución que se pida** (`--target-grid`, repetible): el ejemplo genera **100 m,
  500 m, 1 km y 5 km** (EPSG:9377). Las fuentes se descargan tal como vienen (grilla nativa) y cada
  grilla se calcula directamente desde esos datos; `--nested` las deriva de la más fina por promedio
  exacto de bloques.
* Por grilla, tablas **`gold_daily`**, **`gold_epiweekly`** (semana epidemiológica de domingo a
  sábado, con `_ndays`) y **`static_covariates`**, unidas por `cell_id`, en Parquet o DuckDB.
* **Latencia operativa medida en vivo** (`climate-pipeline availability`): el último día
  disponible hoy por fuente y la última semana epidemiológica completa.

**Datos de la corrida de ejemplo** (Cali, 30 ago – 7 nov 2026): los Parquet de Gold en 100 m,
500 m, 1 km y 5 km están en la Release
[`cali-operativo-2026-11-08`](https://github.com/Felipep17/climate-pipeline/releases/tag/cali-operativo-2026-11-08)
(no se versionan en git porque pesan ~240 MB; se regeneran con `examples/cali_operational/run_pipeline.sh`).

## Documentación

| Documento | Contenido |
|---|---|
| [`docs/resolucion_fuentes.html`](docs/resolucion_fuentes.html) | Solo fuente y resolución espacial y temporal original, de la más fina a la más gruesa |
| [`docs/diccionario_variables.html`](docs/diccionario_variables.html) | Cada variable seleccionada: qué explica, fuente, resolución original, latencia de publicación y efectiva, regla semanal y para qué objetivo sirve |
| [`outputs/cali_operational/latencia_operativa.html`](outputs/cali_operational/latencia_operativa.html) | Si consulto hoy, ¿hasta qué día tengo datos? Por fuente y por variable, con la semana epidemiológica completa |
| [`docs/flujo_proceso.html`](docs/flujo_proceso.html) | El proceso de punta a punta: proveedores, plan por latencia, Bronze, Silver, Gold anidado y estáticas |
| [`docs/modelo_relacional.html`](docs/modelo_relacional.html) | Tablas, claves, rutas y cómo se cruzan las escalas (`parent_cell_ids`) |
| [`docs/predictores_por_variable.html`](docs/predictores_por_variable.html) | Papers por variable objetivo: qué predictores usa la literatura y en qué columnas están ([versión Markdown](docs/predictores_por_variable.md)) |
| [`outputs/cali_operational/README.md`](outputs/cali_operational/README.md) | Evidencias del ejercicio operativo: filas por grilla, cobertura por fuente, consistencia entre escalas y figuras (mapas en 100 m, 500 m, 1 km y 5 km, GIF semanal, series diarias) |

`uv run python docs/make_docs.py` regenera los HTML de `docs/` desde el catálogo de variables.

## Fuentes seleccionadas

| Fuente (`--sources`) | Organismo | Variables | Qué aporta | Resolución original | Latencia | Documentación |
|---|---|---|---|---|---|---|
| `imerg` | NASA GPM | 1 | precipitación diaria (30 min → día civil de Colombia) | 0.1° (~11 km) | ~2 d (provisional) | [NASA GPM IMERG](https://gpm.nasa.gov/data/imerg) · [GEE IMERG V07](https://developers.google.com/earth-engine/datasets/catalog/NASA_GPM_L3_IMERG_V07) |
| `agera5` | Copernicus C3S | 21 | T media/máx/mín y diurna/nocturna, rocío, presión de vapor, VPD en Tmáx, HR a las 06/09/12/15/18 h, HR máx/mín, viento, radiación solar, ET₀ FAO-56, fracción de horas con lluvia | 0.1° (~11 km) | ~8 d | [Copernicus CDS AgERA5](https://cds.climate.copernicus.eu/datasets/sis-agrometeorological-indicators) |
| `era5land` | ECMWF | 13 | T de piel, presión, viento u/v (dirección), temperatura del suelo, evaporación, calor sensible, onda larga, escorrentía, LAI y albedo | 0.1° (~11 km) | ~7 d | [GEE ERA5-Land daily](https://developers.google.com/earth-engine/datasets/catalog/ECMWF_ERA5_LAND_DAILY_AGGR) |
| `goes` | NOAA GOES-East ABI | 14 | brillo IR y de vapor de agua (bandas 8-15), diferencias de bandas, mínimo diario, duración de nube fría (<235 K, <220 K), reflectancia visible diurna | 2 km (10 min → día) | ~1 d | [GEE GOES-19 MCMIP](https://developers.google.com/earth-engine/datasets/catalog/NOAA_GOES_19_MCMIPF) · [GEE GOES-16 MCMIP](https://developers.google.com/earth-engine/datasets/catalog/NOAA_GOES_16_MCMIPF) |
| `modis` | NASA | 7 | LST día y noche de Terra y Aqua, fracción nubosa, agua precipitable y aerosoles (MAIAC) | 1 km | ~3 d | [MOD11A1](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD11A1) · [MYD11A1](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MYD11A1) · [MOD09GA](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD09GA) · [MCD19A2 MAIAC](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD19A2_GRANULES) |
| `gee` | NASA / ESA | 10 | NDVI, NDWI, EVI y MNDWI de MODIS (diario) y Sentinel-2, más NDRE y BSI de Sentinel-2 | 500 m / 10-20 m | ~2-4 d | [MOD09GA](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD09GA) · [MYD09GA](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MYD09GA) · [Sentinel-2 SR](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED) · [Cloud Score+](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_CLOUD_SCORE_PLUS_V1_S2_HARMONIZED) |
| `landsat` | USGS | 1 | LST a 30 m (islas de calor) | 30 m | ~7 d | [Landsat 8 C2 L2](https://developers.google.com/earth-engine/datasets/catalog/LANDSAT_LC08_C02_T1_L2) · [Landsat 9 C2 L2](https://developers.google.com/earth-engine/datasets/catalog/LANDSAT_LC09_C02_T1_L2) |
| `sentinel1` | ESA | 3 | radar VV, VH y VH−VV (atraviesa nubes) | 10 m | ~3 d | [Sentinel-1 GRD](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S1_GRD) |
| `smap` | NASA | 2 | humedad del suelo superficial y de la zona de raíces | 9 km | ~3 d | [SMAP L4 SPL4SMGP](https://developers.google.com/earth-engine/datasets/catalog/NASA_SMAP_SPL4SMGP_008) |
| `viirs` | NASA | 1 | luces nocturnas diarias | ~460 m | ~9 d | [VIIRS VNP46A2](https://developers.google.com/earth-engine/datasets/catalog/NASA_VIIRS_002_VNP46A2) |
| `build-static` | Copernicus, ESA, Google, JRC, OSM | 24 | terreno (elevación, pendiente, orientación, TPI a 500 m y 2 km, rugosidad) y forma urbana (NDBI, LST anual, edificios, alturas, vías, intersecciones, luces) | 4-100 m | fija o anual | [Copernicus DEM (AWS Open Data)](https://registry.opendata.aws/copernicus-dem/) · [Sentinel-2 SR](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED) · [Landsat 9 C2 L2](https://developers.google.com/earth-engine/datasets/catalog/LANDSAT_LC09_C02_T1_L2) · [Dynamic World](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1) · [ESA WorldCover](https://developers.google.com/earth-engine/datasets/catalog/ESA_WorldCover_v200) · [GHS-BUILT-S](https://developers.google.com/earth-engine/datasets/catalog/JRC_GHSL_P2023A_GHS_BUILT_S) · [GHS-BUILT-H](https://developers.google.com/earth-engine/datasets/catalog/JRC_GHSL_P2023A_GHS_BUILT_H) · [Open Buildings 2.5D Temporal](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_Research_open-buildings-temporal_v1) · [VIIRS DNB mensual](https://developers.google.com/earth-engine/datasets/catalog/NOAA_VIIRS_DNB_MONTHLY_V1_VCMSLCFG) · [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API) |

Fuentes disponibles en el código pero **no seleccionadas** porque otra mide lo mismo más reciente
o más fino: `chirps` ([CHC CHIRPS v3](https://www.chc.ucsb.edu/data/chirps3); 5.5 km, ~8 d), `mswep` y `gsmap`
([GloH2O MSWEP](https://www.gloh2o.org/mswep/), [GEE GSMaP v8](https://developers.google.com/earth-engine/datasets/catalog/JAXA_GPM_L3_GSMaP_v8_operational); 0.1°, empatan con IMERG; MSWEP necesita
acceso de GloH2O) y las columnas de ERA5-Land, Landsat y VIIRS duplicadas con AgERA5, MODIS,
SMAP o Sentinel-2. `uv run climate-pipeline variables` lista cada decisión con su razón;
`deduplicate: false` en el YAML las conserva todas.

## 1. Inicio rápido (`uv`)

```bash
uv sync                                   # Python 3.12 + dependencias fijadas (uv.lock)
cp .env.example .env                      # credenciales (CDSAPI_KEY, GEE_PROJECT, ...)
uv run climate-pipeline variables         # catálogo y decisiones de redundancia
uv run pytest                             # suite offline (sin red ni credenciales)
uv run ruff check . && uv run ruff format --check .
```

Ejercicio operativo completo (Cali, de la semana epidemiológica 35 hasta hoy, todas las fuentes
seleccionadas, grillas de 100 m, 500 m, 1 km y 5 km, estáticas, evidencias y página de latencia):

```bash
GEE_PROJECT=<proyecto> bash examples/cali_operational/run_pipeline.sh
```

Corrida diaria en producción (cada fuente pide solo los días que ya pueden existir y reintenta
los faltantes o preliminares):

```bash
uv run climate-pipeline run --aoi-path data/auxiliary/shapefiles/cali/cali.shp \
  --config examples/cali_operational/pipeline.yaml \
  --sources imerg,agera5,gee,era5land,modis,viirs,smap,landsat,goes,sentinel1 \
  --incremental \
  --target-grid 100m=EPSG:9377@100 --target-grid 500m=EPSG:9377@500 \
  --target-grid 1km=EPSG:9377@1000 --target-grid 5km=EPSG:9377@5000
```

![Variables clave de una semana epidemiológica en 100 m, 500 m, 1 km y 5 km](outputs/cali_operational/maps/resoluciones_semana.png)

> Ejemplo histórico: Cali, enero a mayo de 2026, con CHIRPS, AgERA5 y NDVI/NDWI en 1 km y 5 km
> independientes ([`outputs/cali_2026-01_2026-05/`](outputs/cali_2026-01_2026-05/README.md),
> `examples/cali_2026/run_pipeline.sh`).

**Contenido:** [1. Inicio rápido](#1-inicio-rápido-uv) ·
[2. Configuración](#2-configuración) · [3. Arquitectura](#3-arquitectura) ·
[4. Reglas espaciales](#4-reglas-espaciales-y-de-remuestreo) ·
[5. Variables y QC](#5-variables-y-control-de-calidad) ·
[6. Latencia y CI/CD](#6-latencia-de-datos-y-robustez-operativa) ·
[7. Hallazgos](#7-hallazgos-del-ejemplo-cali-ene-may-2026) ·
[8. Credenciales](#8-credenciales) · [9. Desarrollo](#9-desarrollo) ·
[10. Migración](#10-migración-desde-el-código-original) ·
[11. Latencia operativa](#11-latencia-operativa-qué-dato-tengo-hoy) ·
[12. Resoluciones de Gold](#12-resoluciones-de-gold-las-que-se-pidan)

### Comandos de la CLI

| Comando | Qué hace |
|---|---|
| `run` | Bronze → Silver → Gold. `--nested` deriva todas las grillas de la más fina; `--incremental` usa la ventana de latencia de cada fuente; `--skip-gold` se detiene en Silver (jobs de CI). |
| `plan` | Muestra qué `(variable, fecha)` se descargaría según la latencia y el catálogo. `--github-matrix` emite la matriz para Actions. |
| `build-gold` | Reconstruye Gold solo para las fechas cuyo Silver cambió desde el último build (por grilla y formato). |
| `build-static` | Covariables estáticas (`--layers terrain,urban,osm`) en una o varias grillas Gold. |
| `make-raster` | Tabla Gold → GeoTIFF/COG (día, semana epidemiológica o tabla estática), opcionalmente remuestreado a otra plantilla. |
| `variables` / `catalog` | Catálogo de variables y decisiones de redundancia / estado de la ingesta por fuente, capa y estado. |
| `availability` | Consulta en vivo a cada proveedor: último día completo, rezago y última semana epidemiológica completa (JSON). |

Parámetros principales: `--start-date`, `--end-date`, `--aoi-path` (Shapefile, GeoJSON
o GPKG con CRS), `--sources`, `--target-grid` (repetible: `nombre=EPSG:XXXX@resolución`
o un ráster plantilla) y `--output-format` (`parquet` | `duckdb`). `uv run climate-pipeline
<comando> --help` documenta todas las opciones.

---

## 2. Configuración

Sin `--config` se usan los valores por defecto de `src/climate_pipeline/config.py`
(`DEFAULT_SOURCES`), que son los mismos de [`config/pipeline.yaml`](config/pipeline.yaml).
Un YAML propio solo necesita las claves que cambia; el resto se combina con los defaults.
Usa el mismo `--config` en `run`, `plan` y `build-gold`: Gold rechaza columnas de Silver que
el catálogo de variables no conoce.

| Clave | Qué controla |
|---|---|
| `clip_mode` | `polygon` (por defecto) o `bbox`; ambos conservan todo píxel que toque el AOI |
| `sources.<fuente>.max_workers` | peticiones en paralelo hacia el proveedor |
| `sources.<fuente>.latency` | `prelim_lag_days`, `final_lag_days`, `lookback_days` (ventana de `--incremental`) |
| `sources.<fuente>.variables` | subconjunto de variables; `null` = las del catálogo |
| `sources.chirps.options` | `version: v3 \| v2`, `disaggregation: rnl \| sat`, `mirror: gee` (CHIRPS v2.0 desde Earth Engine) |
| `sources.agera5.options` | `version: "2_0"`, `area_margin_deg` |
| `sources.mswep.options` | `folders`: IDs de Google Drive con su estado (`final` / `prelim`) |
| `sources.gee.options` | `products` (`modis_terra_aqua_indices`, `modis_mod09ga_indices`, `sentinel2_indices`) y `cloud_mask: standard \| strict` |

La raíz del lago se toma de `CLIMATE_DATA_ROOT` (por defecto `data/`) o de `--data-root`.
[`examples/cali_2026/pipeline.yaml`](examples/cali_2026/pipeline.yaml) es un ejemplo real:
la configuración por defecto, pero con CHIRPS leído desde el espejo de Earth Engine.

---

## 3. Arquitectura

```
                 ┌──────────── plan (latencia + catálogo) ────────────┐
                 ▼                                                    │
 CHC / CDS / GEE / Drive ──► BRONZE ──► SILVER ──────────► GOLD ──────┴─► make_raster (COG)
                            crudo,      armonizado por     una tabla por grilla:
                            ventana     fuente, grilla     gold_daily, gold_epiweekly,
                            del AOI     nativa, recorte    static_covariates
                                        all_touched, QC    (Parquet particionado / DuckDB)
```

```
src/climate_pipeline/
├── domain/        objetos de valor puros: AOI, GridSpec (anidamiento), catálogo de variables, regla de
│                  redundancia, semanas epi., errores
├── extractors/    upstream → Bronze: chirps, agera5, mswep, gee, gee_datasets (era5land, imerg, gsmap,
│                  modis, viirs, smap, landsat, goes, sentinel1) + registro, reintentos
├── processing/    transformaciones puras: spatial (all_touched), harmonize, quality, resample,
│                  aggregation (semana epidemiológica), terrain
├── storage/       layout del lago, catálogo (manifiesto), Gold Parquet/DuckDB, NetCDF atómico, make_raster
├── pipeline/      casos de uso: latency (planificador), orchestrator, gold (builder por grilla y anidado),
│                  static + urban (terreno, forma urbana, OSM), availability (latencia operativa)
└── cli.py         interfaz typer
tests/             pruebas offline con datos sintéticos (+2 de red con `-m network`)
config/            pipeline.yaml (latencias, variables, opciones por fuente)
data/auxiliary/    AOI de ejemplo: barrios de Cali
examples/cali_2026 script reproducible, generador de evidencias, mapas/GIFs y diagnóstico de nubes
outputs/           evidencias de la corrida de ejemplo (mapas, GIFs, QC, tablas, reportes)
.github/workflows  ci.yml (lint + pruebas) y data-pipeline.yml (ingesta programada)
```

### Layout del lago de datos

```
data/
├── bronze/{source}/{YYYY}/{MM}/{source}[_{variable}]_{YYYY-MM-DD}.nc
├── silver/{source}/{YYYY}/{MM}/{source}_{YYYY-MM-DD}.nc
├── gold/{grilla}/grid.json                       ← definición exacta de la grilla (cell_id)
├── gold/{grilla}/parquet/daily/year=YYYY/month=MM/date=YYYY-MM-DD.parquet
├── gold/{grilla}/parquet/epiweekly/epi_year=YYYY/epi_week=WW.parquet
├── gold/{grilla}/parquet/static/covariates.parquet
├── gold/{grilla}/climate.duckdb                  ← mismas tablas en DuckDB
└── state/manifest/{source}.parquet               ← catálogo de ingesta (un archivo por fuente)
```

* **Bronze** guarda el dato crudo (valores, unidades y nombre originales) de una ventana
  alrededor del AOI, con atributos de procedencia (`origin`, `product_status`,
  `retrieved_at`). Bajar el archivo global de CHIRPS (~30 MB/día) no aporta nada: la
  lectura por ventana vía `/vsicurl/` baja solo las tiras necesarias.
* **Silver** es una armonización por fuente en su **grilla nativa**: nombres canónicos,
  unidades destino, QC de rangos físicos y recorte `all_touched`.
* **Gold** remuestrea cada fuente a la grilla objetivo y la fusiona en una tabla por grilla.

Todas las escrituras son **atómicas** (archivo temporal + `rename`) e **idempotentes**: se
reescribe la partición de la fecha o semana, nunca se agrega a ciegas.

---

## 4. Reglas espaciales y de remuestreo

**Regla fundamental `all_touched=True`:** se conserva todo píxel cuya celda toque el
polígono, aunque su centro quede fuera. Los bordes de celda se calculan explícitamente
y el polígono se rasteriza con `rasterio.features.geometry_mask(all_touched=True)`, sin
depender de heurísticas de `rio.clip_box`. Un AOI más pequeño que un píxel conserva ese
píxel. `clip_mode: bbox` conserva toda celda que toque el bounding box.

**Grilla objetivo (`--target-grid`):**
* `1km=EPSG:9377@1000`: grilla de 1000 m en MAGNA-SIRGAS 2018 / Origen-Nacional, alineada a
  múltiplos de la resolución (las celdas de 5 km anidan las de 1 km).
* `ruta/plantilla.tif`: copia CRS, transformada y forma de un ráster existente.
* Si se omite, se usa la grilla nativa más fina entre las fuentes de Silver.
* Cada grilla vive en `gold/{nombre}/` con su `grid.json`. Pedir otra grilla con el mismo
  nombre produce `GridMismatchError` en lugar de mezclar grillas.

**Métodos de remuestreo (por variable, en el catálogo):**

| Tipo | Método | Motivo |
|---|---|---|
| Continuas (temperatura, HR, presión de vapor, viento, radiación) | `bilinear`, con respaldo de promedio de área en el borde | campo suave; en el borde, donde el kernel bilineal leería píxeles enmascarados, se usa el promedio de área de los píxeles válidos que cubren la celda |
| Flujos/acumulados (precipitación) e índices (NDVI/NDWI) | `average` (ponderado por área) | aproximadamente conservativo en masa |

Gold incluye `cell_id = fila × ancho + columna`. `make_raster` reconstruye el ráster
dispersando valores por `cell_id`, sin interpolación: la ida y vuelta es exacta.

---

## 5. Variables y control de calidad

`uv run climate-pipeline variables` lista el catálogo completo (unidades, rango físico,
remuestreo, regla semanal y si la columna está seleccionada). El detalle de cada variable
seleccionada (qué explica, fuente, resolución, latencia y para qué objetivo sirve) está en
[`docs/diccionario_variables.html`](docs/diccionario_variables.html). Las reglas por tipo:

| Tipo de variable | Ejemplos | Remuestreo | Semana epidemiológica |
|---|---|---|---|
| Flujos y acumulados | `imerg_precip`, `agera5_et0`, `era5land_runoff`, `era5land_evaporation` | promedio por área | **suma**, exige 7/7 días |
| Campos continuos | temperaturas, HR, presión de vapor, viento, radiación, brillos de GOES | bilineal (promedio por área en el borde del AOI) | media, ≥4 días |
| Ópticos y radar con nubes o revisita | LST de MODIS y Landsat, índices de MODIS y Sentinel-2, MAIAC, Sentinel-1 | promedio por área | media, ≥1 día válido |
| Fracciones | `modis_cloud_frac`, `goes_cold_cloud_frac_235`, `goes_refl_c02_day` | promedio por área | media |
| Estáticas | `static_*`, `urban_*` | promedio por área | (sin tiempo) |

**El QC se aplica en tres niveles:**
1. **Rangos físicos (Silver):** un valor imposible pasa a NaN y se cuenta en el reporte
   (`below_minimum`, `above_maximum`, `non_finite`). Un valor dentro de la tolerancia se
   recorta, por ejemplo HR = 100.4 % → 100 %. Un NDVI de 1.7 o una temperatura de 297 °C
   (Kelvin sin convertir) nunca llegan a Gold.
2. **Consistencia entre variables (Gold diario):** Tmín ≤ Tmedia ≤ Tmáx y Tdew ≤ Tmedia
   (AgERA5 y ERA5-Land), con tolerancia de 0.5 °C. Si una regla falla, ambas columnas pasan a NaN, porque no se
   sabe cuál está mal.
3. **Invariantes estructurales:** claves únicas `(cell_id, date)` y
   `(cell_id, epi_year, epi_week)`, columnas requeridas y valores finitos dentro de rango.
   Si fallan, se lanza `QualityError` y el pipeline se detiene: indican un bug, no un dato
   malo de origen.

### Semana epidemiológica

Convención MMWR / INS: semanas de **domingo a sábado**; la semana 1 es la que contiene el
4 de enero; una semana pertenece al año de su miércoles, así que algunos años tienen 53
semanas (2020 y 2025). `gold_epiweekly` tiene una fila por
`(cell_id, epi_year, epi_week)` con `week_start`, `week_end` y `days_observed`, y cada
columna `c` tiene su `c_ndays`. Una suma de precipitación con días faltantes parecería una
semana seca, por eso queda en NaN; la columna `_ndays` lo documenta. La agregación se
valida contra una re-agregación independiente en pandas, tanto en las pruebas como en el
QC del ejemplo.

---

## 6. Latencia de datos y robustez operativa

| Fuente | Preliminar | Final | Estrategia |
|---|---|---|---|
| IMERG V07 | ~2 días (`provisional`) | ~3.5 meses | el estado de cada imagen decide `prelim`/`final`; el día espera las 48 medias horas |
| GOES-East, SMAP | — | 1-3 días | el día espera ≥120 de 144 escaneos (GOES) u 8 de 8 (SMAP) |
| AgERA5, ERA5-Land | — | 7-8 días | no pide fechas que aún no pueden existir |
| MODIS, Sentinel-1/2, Landsat | — | 2-7 días | los días sin pasada quedan como `missing` y se reintentan |
| VIIRS Black Marble | — | ~9 días | las imágenes vacías de los últimos días cuentan como no publicadas |
| CHIRPS v3 (opcional) | ~7 días (`prelim/sat`) | ~3.ª semana del mes siguiente | intenta final → prelim; re-descarga el prelim cuando ya debería existir el final |

* El **planificador** decide por `(variable, fecha)`: `new`, `retry_missing`,
  `prelim_upgrade`, `forced`, `up_to_date` o `not_yet_expected`. Así, una corrida
  programada nunca pide datos que el proveedor no puede tener todavía.
* **Fallos esperados vs inesperados.** Un dato no publicado se registra como `missing` y se
  reintenta en la siguiente corrida, sin fallar. Un error real (credenciales, red, archivo
  corrupto) se aísla por tarea: las demás fechas y fuentes siguen y la CLI sale con
  código 1 al final.
* **Throttling:** espaciado global entre peticiones a CHC y reintentos con espera
  exponencial ante 403/429/5xx. Si CHC bloquea, `mirror: gee` lee CHIRPS v2.0 desde Earth
  Engine en la grilla nativa exacta. La configuración rechaza mezclar versiones.
* **Concurrencia:** AgERA5 envía las peticiones (variable × mes) en paralelo porque CDS
  encola cada una alrededor de un minuto. Las escrituras NetCDF ocurren en el hilo
  principal: HDF5 no es thread-safe y activa la impresión de errores por hilo.
* **Catálogo portable:** un Parquet por fuente con rutas relativas a la raíz del lago, así
  que el lago se puede sincronizar entre máquinas o jobs de CI.

### CI/CD (GitHub Actions)

* **`ci.yml`:** `uv sync --locked`, `ruff check`, `ruff format --check` y `pytest` con
  cobertura. Las pruebas de red corren en un job aparte que no bloquea.
* **`data-pipeline.yml`:** cron diario (07:30 UTC) y `workflow_dispatch` con parámetros.
  1. `plan` descarga el catálogo del bucket y emite la matriz de fuentes con trabajo
     pendiente.
  2. `ingest` (matriz, `fail-fast: false`) hace Bronze → Silver por fuente en paralelo; cada
     job sube solo los archivos de su fuente.
  3. `gold` corre aunque alguna fuente haya fallado: reconstruye solo las fechas cuyo
     Silver cambió, para 1 km y 5 km, en Parquet y DuckDB.
  * El estado vive en un bucket GCS (`vars.DATA_BUCKET`), con autenticación por Workload
    Identity Federation y el secreto `CDSAPI_KEY`. La cabecera del workflow lista todas las
    variables y secretos. Mientras `DATA_BUCKET` no exista, todos los jobs se omiten: el cron
    no falla en forks ni antes de montar la infraestructura.

---

## 7. Hallazgos del ejemplo (Cali, ene-may 2026)

Detalle y cifras en [`outputs/cali_2026-01_2026-05/README.md`](outputs/cali_2026-01_2026-05/README.md):

* **La precipitación de AgERA5 no tiene estructura espacial a escala urbana:** es la de
  ERA5 (~0.25°) replicada en la grilla de 0.1°. Para la lluvia espacial, usar CHIRPS.
* **La temperatura de AgERA5 tiene sesgo frío** frente a la climatología de Cali (~20.5 °C
  vs ~24 °C), un efecto conocido de la orografía suavizada del reanálisis. Las covariables
  de elevación están para corregirlo en el downscaling.
* **Nubosidad y máscara de nubes (NDVI/NDWI):** se midió en GEE, por celda de 1 km y semana
  epidemiológica (enero a mayo de 2024-2026), cuánto deja cada máscara. Sin máscara (código
  original) no hay huecos, pero el NDVI mediano de los píxeles nublados es 0.12 frente a 0.67
  despejado: eran nubes. La máscara MODIS por defecto (`cloud_mask: standard`) quita nube,
  sombra y nube interna y conserva los píxeles solo "adyacentes a nube" (NDVI mediano
  0.57-0.62, casi limpios); `strict` también los quita y deja el 17-28 % de las celdas-semana
  con valor, frente a 46-69 % con `standard`. Sentinel-2 + Cloud Score+ aporta otro 69-76 %
  independiente; juntos llegan al ~78 % en 2026. La semana usa ≥1 día despejado y `*_ndays`
  dice cuántos hubo; lo que sigue sin dato queda en NA, sin imputar.
* **Panel completo:** las tablas Gold tienen una fila por celda del AOI y por día (o
  semana) construido, aunque esa celda no tenga ningún valor: los faltantes son NA, nunca
  filas ausentes.
* **CHC limitó la IP con HTTP 403** tras una ráfaga paralela. El pipeline aisló los fallos,
  siguió con las demás fuentes y se completó con el espejo de Earth Engine.

---

## 8. Credenciales

Ver [`.env.example`](.env.example). CHIRPS (CHC) y OpenStreetMap no requieren credenciales;
todas las fuentes vía Earth Engine (`era5land`, `imerg`, `gsmap`, `modis`, `viirs`, `smap`,
`landsat`, `goes`, `sentinel1`, `gee` y las capas `urban`) usan el mismo `GEE_PROJECT`.

* **AgERA5:** token de https://cds.climate.copernicus.eu y aceptar una vez la licencia del
  dataset. Si `CDSAPI_KEY` no está definido, se usa `~/.cdsapirc`.
* **GEE:** `earthengine authenticate` en local y `GEE_PROJECT`. En CI, una cuenta de
  servicio o Workload Identity (`GEE_SERVICE_ACCOUNT_KEY`).
* **MSWEP:** solicitar acceso en https://www.gloh2o.org/mswep/. Luego OAuth de escritorio
  (`MSWEP_OAUTH_CLIENT`, el token se guarda en `data/auxiliary/tokens/`) o una cuenta de
  servicio con las carpetas compartidas.

---

## 9. Desarrollo

```bash
uv sync                                   # incluye el grupo dev (pytest, ruff, matplotlib, contextily)
uv run pytest                             # offline: fuentes simuladas con NetCDF/GeoTIFF sintéticos
uv run pytest -m network                  # 2 pruebas contra endpoints públicos reales
uv run ruff check . && uv run ruff format --check .
```

* **Agregar una fuente:**
  1. Un módulo en `extractors/` con una subclase de `BaseExtractor` cuyo `fetch()` escriba
     Bronze, decorada con `@register_extractor` e importada en `extractors/__init__.py`.
  2. Sus `VariableSpec` (unidades, rango físico, remuestreo y regla semanal) en
     `domain/variables.py`, dentro de `default_specs()`.
  3. Su latencia en `DEFAULT_SOURCES` (`config.py`) y en `config/pipeline.yaml`.

  Silver, Gold, QC, planificador y CLI la toman sin más cambios. Un producto nuevo de Earth
  Engine es solo un `GeeProduct` en `extractors/gee.py`.
* **Regenerar el ejemplo:** `GEE_PROJECT=<proyecto> bash examples/cali_2026/run_pipeline.sh`
  (descargas, Gold, rásteres, GIFs y evidencias; termina con código 1 si falla un chequeo de QC).
* **Qué se versiona:** el código, la configuración, los AOI de `data/auxiliary/shapefiles/`
  (procedencia en su [README](data/auxiliary/shapefiles/README.md)) y las evidencias de
  `outputs/`. El lago de datos (`data/bronze`, `silver`, `gold`, `state`), `.env` y los
  tokens no se versionan: quedan en `.gitignore`.

---

## 10. Migración desde el código original

Los notebooks y scripts originales se retiraron del árbol; siguen en el historial de Git
(último commit anterior al refactor: `6ae015c`, p. ej.
`git show 6ae015c:notebooks/03_download_gee.ipynb`).

| Original | Reemplazo |
|---|---|
| `notebooks/00-setup.ipynb` | `uv sync` + `DataLake` (`storage/layout.py`) |
| `notebooks/01_download_mswep.ipynb`, `download/mswep.py` | `extractors/mswep.py` (token fuera del repo, carpetas configurables) |
| `notebooks/02_download_cds.ipynb`, `download/copernicus.py` | `extractors/agera5.py` (solo días pendientes, peticiones en paralelo, AgERA5 v2.0) |
| `notebooks/03_download_gee.ipynb`, `download/gee.py` | `extractors/gee.py` (el original no enmascaraba nubes; ahora máscara QA `standard`, Terra+Aqua, Sentinel-2 + Cloud Score+, rangos físicos) |
| `download/process.py` (`recortar_raster`) | `processing/spatial.py` (`all_touched=True`) |
| `requirements.txt` | `pyproject.toml` + `uv.lock` (versiones fijadas) |
| `data/auxiliary/shapefiles/{colombia,valle}` | retirados: solo los usaban los notebooks; cualquier Shapefile, GeoJSON o GPKG sirve como `--aoi-path` |

---

## 11. Latencia operativa: ¿qué dato tengo hoy?

```bash
uv run climate-pipeline availability --aoi-path data/auxiliary/shapefiles/cali/cali.shp \
  --output outputs/cali_operational/reports/availability_$(date +%F).json
uv run python examples/cali_operational/make_latency_report.py \
  outputs/cali_operational/reports/availability_$(date +%F).json
```

`availability` pregunta a cada proveedor (Earth Engine, CHC) cuál es el
último día **completo y con dato sobre el AOI**: un día sub-diario cuenta solo con todas sus
imágenes, y una imagen de VIIRS vacía cuenta como no publicada. AgERA5 y MSWEP usan la latencia
configurada (su consulta en vivo pasa por la cola del CDS o por credenciales de Drive). La página
[`outputs/cali_operational/latencia_operativa.html`](outputs/cali_operational/latencia_operativa.html)
muestra por fuente la resolución original, la latencia y la última semana epidemiológica completa.

Un pronóstico emitido al inicio de la semana epidemiológica *t* solo tiene covariables completas
de semanas anteriores: el JSON de `availability` y la página de latencias dicen cuántas semanas
de rezago usar al entrenar.

## 12. Resoluciones de Gold: las que se pidan

```bash
uv run climate-pipeline run --aoi-path data/auxiliary/shapefiles/cali/cali.shp \
  --config examples/cali_operational/pipeline.yaml \
  --sources imerg,agera5,gee,era5land,modis,viirs,smap,landsat,goes,sentinel1 \
  --start-date 2026-08-30 --end-date 2026-11-08 \
  --target-grid 100m=EPSG:9377@100 --target-grid 500m=EPSG:9377@500 \
  --target-grid 1km=EPSG:9377@1000 --target-grid 5km=EPSG:9377@5000
uv run climate-pipeline build-static --aoi-path data/auxiliary/shapefiles/cali/cali.shp \
  --grid-name 100m --grid-name 500m --grid-name 1km --grid-name 5km --layers terrain,urban,osm --year 2025
```

* Bronze y Silver guardan cada fuente **tal como viene** (su grilla y resolución nativas: de 10 m
  en Sentinel a 0.1° en IMERG y AgERA5). Ninguna resolución está fija en el código: Gold se genera
  en **cada** grilla de `--target-grid`, remuestreando Silver nativo directamente a esa resolución
  (bilineal para campos continuos, promedio por área para flujos, índices y fracciones). Para otra
  resolución basta con agregar otra `--target-grid` y correr `build-gold`, sin volver a descargar.
* Con `--nested`, en cambio, Silver se remuestrea una vez a la grilla más fina y las demás son el
  promedio exacto de sus bloques (`nest()` exige que las resoluciones sean múltiplos y estén
  alineadas). Sirve cuando se quiere que una celda gruesa sea exactamente el promedio de sus hijas.
* Las grillas en un CRS proyectado se alinean a múltiplos de su resolución, así que las escalas se
  cruzan sin operaciones espaciales con `parent_cell_ids(fina, gruesa, cell_ids)`.
* `build-static` calcula terreno, forma urbana y vías en cada grilla (con `--nested`, en la más
  fina y promediadas). Covariables urbanas (`urban_*`, compuestos anuales de `--year`): NDBI, NDVI y
  MNDWI de Sentinel-2; LST mediana de Landsat (islas de calor); probabilidades de Dynamic World;
  fracciones de ESA WorldCover; superficie y altura construida de GHSL; fracción y altura de
  edificios de Open Buildings 2.5D; luces nocturnas VIIRS; densidad de vías, de vías principales y
  de intersecciones de OpenStreetMap (Overpass, una sola consulta para todas las grillas).
* Ejemplo reproducible: `GEE_PROJECT=<proyecto> bash examples/cali_operational/run_pipeline.sh`
  (evidencias en [`outputs/cali_operational/`](outputs/cali_operational/README.md)).

Fuentes revisadas y no incluidas: estaciones CVC/IDEAM (son puntos, no grillas: van en un flujo
de variable objetivo aparte), WorldPop (población, fuera de alcance), Himawari/HRRR (no cubren
Colombia), NOAA CPC, NASA POWER y ENSO/OISST (más gruesos que 0.1°), SPEI/TerraClimate/
PERSIANN-CDR/CHIRTS (mensuales o con meses de rezago; P − ET₀ con IMERG y AgERA5 es el insumo
para un SPEI propio).
