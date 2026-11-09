# Evidencias — ejercicio en tiempo real de Cali (fuentes diarias seleccionadas, cuatro resoluciones)

Periodo: **2026-01-01 → 2026-11-08** (de `run.start_date` a hoy). Generado por `examples/cali_operational/run_pipeline.sh` (o `.ps1`). Las fuentes se descargan en su grilla nativa y Gold se genera en cada resolución pedida (**100 m, 500 m, 1 km y 5 km**) directamente desde esos datos. Cada partición diaria y semanal trae **todas** las variables seleccionadas: NA donde la fuente aún no publicó.

## Tablas Gold por grilla

| grid | resolution_m | shape (h×w) | cells_in_aoi | daily_rows | epiweekly_rows | first_day | last_day | daily_columns | static_columns |
|---|---|---|---|---|---|---|---|---|---|
| 100m | 100.0 | 189×145 | 12535 | 3910920 | 576610 | 2026-01-01 | 2026-11-08 | 73 | 24 |
| 500m | 500.0 | 39×29 | 570 | 177840 | 26220 | 2026-01-01 | 2026-11-08 | 73 | 24 |
| 1km | 1000.0 | 20×15 | 160 | 49920 | 7360 | 2026-01-01 | 2026-11-08 | 73 | 24 |
| 5km | 5000.0 | 5×4 | 13 | 4056 | 598 | 2026-01-01 | 2026-11-08 | 73 | 24 |

Rutas: `data/gold/<grilla>/parquet/{daily,epiweekly,static}/` (y las mismas tablas en PostgreSQL con `climate-pipeline db import-lake` o `sync`).

## Consistencia entre escalas

Cada grilla se calcula directamente desde los datos nativos a su resolución. Para comprobar que las escalas coinciden se compara cada celda gruesa con el promedio de sus celdas de la grilla más fina (`parent_cell_ids`):

| fine | grid | date | column | cells | mean_abs_diff | correlation |
|---|---|---|---|---|---|---|
| 100m | 500m | 2026-01-01 | imerg_precip | 570 | 0.0243 | 0.9717 |
| 100m | 500m | 2026-01-01 | agera5_temp_max | 570 | 0.0043 | 0.9999 |
| 100m | 500m | 2026-01-01 | goes_cold_cloud_frac_235 | 570 | 0.0001 | 0.9994 |
| 100m | 500m | 2026-01-01 | modis_cloud_frac | 375 | 0.0 |  |
| 100m | 1km | 2026-01-01 | imerg_precip | 160 | 0.0597 | 0.9567 |
| 100m | 1km | 2026-01-01 | agera5_temp_max | 160 | 0.0136 | 0.9993 |
| 100m | 1km | 2026-01-01 | goes_cold_cloud_frac_235 | 160 | 0.0002 | 0.9994 |
| 100m | 1km | 2026-01-01 | modis_cloud_frac | 105 | 0.0 |  |
| 100m | 5km | 2026-01-01 | imerg_precip | 13 | 0.5424 | 0.7509 |
| 100m | 5km | 2026-01-01 | agera5_temp_max | 13 | 0.1447 | 0.9835 |
| 100m | 5km | 2026-01-01 | goes_cold_cloud_frac_235 | 13 | 0.002 | 0.9913 |
| 100m | 5km | 2026-01-01 | modis_cloud_frac | 10 | 0.0 |  |

## Cobertura por fuente (grilla 100m)

`mean_coverage_pct` = porcentaje de valores diarios no nulos (las fuentes ópticas bajan por nubes; los días aún no publicados quedan como NA).

| source | columns | mean_coverage_pct | last_date |
|---|---|---|---|
| agera5 | 21 | 97.8 | 2026-11-01 |
| era5land | 13 | 97.8 | 2026-11-01 |
| gee | 10 | 11.7 | 2026-11-05 |
| goes | 14 | 96.8 | 2026-11-08 |
| imerg | 1 | 98.4 | 2026-11-05 |
| landsat | 1 | 4.7 | 2026-11-01 |
| modis | 7 | 15.2 | 2026-11-06 |
| sentinel1 | 3 | 26.0 | 2026-11-06 |
| smap | 2 | 98.7 | 2026-11-04 |
| viirs | 1 | 89.1 | 2026-10-30 |

Detalle por columna: [`tables/column_inventory.csv`](tables/column_inventory.csv).

## Figuras

Generadas con `examples/cali_operational/make_figures.py`.

**Variables clave de la última semana completa en 100 m, 500 m, 1 km y 5 km**

![Variables clave de la última semana completa en 100 m, 500 m, 1 km y 5 km](maps/resoluciones_semana.png)

**Terreno y forma urbana en las cuatro resoluciones**

![Terreno y forma urbana en las cuatro resoluciones](maps/estaticas_resoluciones.png)

**Precipitación IMERG y Tmáx AgERA5 semana a semana, cuatro resoluciones**

![Precipitación IMERG y Tmáx AgERA5 semana a semana, cuatro resoluciones](animations/semanal_resoluciones.gif)

**Una variable por familia en la última semana completa, 100 m**

![Una variable por familia en la última semana completa, 100 m](maps/variables_100m.png)


## Cobertura del periodo 2026-01-01 → 2026-11-09 (grilla 100m)

`climate-pipeline coverage`: días con dato (`in_gold`), días sin píxel válido (nubes / sin pasada), días que el proveedor no publicó, huecos (`not_built`, `not_requested`) y cuántos días va atrás cada fuente respecto de lo que ya podría estar publicado (`behind_days`).

| source | expected_until | days | in_gold | no_valid_pixels | upstream_missing | not_built | not_requested | last_valid | behind_days | stale |
|---|---|---|---|---|---|---|---|---|---|---|
| imerg | 2026-11-07 | 311 | 307 | 0 | 4 | 0 | 0 | 2026-11-05 | 0 | False |
| agera5 | 2026-11-01 | 305 | 305 | 0 | 0 | 0 | 0 | 2026-11-01 | 0 | False |
| gee | 2026-11-05 | 309 | 138 | 171 | 0 | 0 | 0 | 2026-11-05 | 0 | False |
| era5land | 2026-11-02 | 306 | 305 | 0 | 1 | 0 | 0 | 2026-11-01 | 0 | False |
| modis | 2026-11-06 | 310 | 304 | 6 | 0 | 0 | 0 | 2026-11-06 | 0 | False |
| viirs | 2026-11-03 | 307 | 278 | 0 | 29 | 0 | 0 | 2026-10-30 | 0 | False |
| smap | 2026-11-06 | 310 | 308 | 0 | 2 | 0 | 0 | 2026-11-04 | 0 | False |
| landsat | 2026-11-02 | 306 | 21 | 16 | 269 | 0 | 0 | 2026-11-01 | 0 | False |
| goes | 2026-11-08 | 312 | 302 | 0 | 10 | 0 | 0 | 2026-11-08 | 0 | False |
| sentinel1 | 2026-11-06 | 310 | 81 | 0 | 229 | 0 | 0 | 2026-11-06 | 0 | False |

## Disponibilidad operativa al 2026-11-09

| source | latest_day | lag_days | last_complete_epiweek | status | method |
|---|---|---|---|---|---|
| imerg | 2026-11-06 | 3 | 2026-W43 | prelim | probe |
| agera5 | 2026-11-01 | 8 | 2026-W43 | final | config |
| gee | 2026-11-06 | 3 | 2026-W43 | final | probe |
| era5land | 2026-11-01 | 8 | 2026-W43 | final | probe |
| modis | 2026-11-05 | 4 | 2026-W43 | final | probe |
| viirs | 2026-10-30 | 10 | 2026-W42 | final | probe |
| smap | 2026-11-05 | 4 | 2026-W43 | final | probe |
| landsat | 2026-11-01 | 8 | 2026-W43 | final | probe |
| goes | 2026-11-08 | 1 | 2026-W44 | final | probe |
| sentinel1 | 2026-11-06 | 3 | 2026-W43 | final | probe |

Todas las fuentes completas hasta **2026-10-30** (semana epidemiológica **2026-W42**).
