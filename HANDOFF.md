# Handoff — climate-pipeline (Dengue.IA Cali)

_Actualizado: 2026-11-09 · todo en `main` (PR #4 backend Postgres y ejercicio en tiempo real · PR #5
optimización y limpieza · PR #6 licencia, atribución y procedencia · PR #7 este handoff)_

## Objetivo

Backend de covariables **diarias y al día de hoy** para los modelos de downscaling (precipitación,
temperatura, HR, viento, radiación) y de dengue en Cali. Mantiene Gold (grillas y semana epidemiológica)
en **PostgreSQL** y/o **Parquet**. Un cron diario puede consultar qué falta entre `run.start_date` y hoy,
bajar solo eso y dejar Gold al día. El front lo construye Andrés; el backend entrega datos listos.

## Estado actual

| Pieza | Estado |
|---|---|
| Ejercicio único en tiempo real (`examples/cali_operational`: 2026-01-01 → hoy, 10 fuentes, 100 m / 500 m / 1 km / 5 km, estáticas 2026) | ✅ corrido localmente; lago en `data/` |
| Backend PostgreSQL (migraciones 0001–0003, Gold particionado por grilla, `ingest_manifest`, PostGIS, vistas de frescura y estado de producto) | ✅ con pruebas de integración |
| `sync`, `coverage`, `snapshot`, `db init/status/import-lake` | ✅ |
| Configuración declarativa (`run:` en el YAML) | ✅ todos los comandos la leen |
| Optimización: fuentes en paralelo, Silver leído una vez para todas las grillas, `rasterio.warp` directo, COPY vía Arrow | ✅ Gold ~2× más rápido; la descarga dura lo de la fuente más lenta |
| Licencia (MIT), `CITATION.cff`, `ATTRIBUTION.md` | ✅ **falta que Diego confirme la MIT** |
| Estado provisional en Gold (`product_status*`) y snapshots fechados | ✅ primer snapshot `data/snapshots/2026-11-09/` |
| CI (`ci.yml`: lint, pruebas con PostGIS, imagen a GHCR) y cron (`db-sync.yml`) | ⚠️ escritos pero **sin correr**: GitHub Actions está desactivado en el repo |
| Base local (`docker compose up -d db`, puerto 5433) | ✅ cargada con 2026-01-01 → 2026-11-08, 4 grillas (1.8 GB) |
| Calidad | ruff limpio, `uv.lock` al día, **179 pruebas** (79.6 % de cobertura) incluidas las de PostGIS; imagen Docker construida y probada |

## Resultados del ejercicio (corrida del 2026-11-08/09)

Cali, **2026-01-01 → 2026-11-08**, 10 fuentes, 73 variables diarias + 24 estáticas. Lago en `data/`
(Bronze 839 MB, Silver 220 MB, Gold 1.1 GB) y lo mismo en PostgreSQL local.

| Grilla | Celdas del AOI | Filas diarias | Filas semanales | Estáticas |
|---|---|---|---|---|
| 100 m | 12 535 | 3 910 920 | 576 610 | 12 535 |
| 500 m | 570 | 177 840 | 26 220 | 570 |
| 1 km | 160 | 49 920 | 7 360 | 160 |
| 5 km | 13 | 4 056 | 598 | 13 |

* **Cobertura** (igual en las 4 grillas): **0 huecos, ninguna fuente atrasada**. Los días sin dato son días
  que el proveedor no publicó (Landsat 269, Sentinel-1 229, VIIRS 29, GOES 10) o sin píxel válido por nubes
  (índices GEE 171, MODIS 6).
* **Disponibilidad** (`availability`): todas las fuentes completas hasta **2026-10-30** (semana
  epidemiológica **2026-W42**). GOES va a 1 día; IMERG, GEE y Sentinel-1 a 3; MODIS y SMAP a 4; AgERA5,
  ERA5-Land y Landsat a 8; VIIRS a 10.
* **Estado de producto:** IMERG tiene 307 días `prelim` y 0 `final` en 2026 (Late Run); todas las demás
  fuentes son finales.
* **Tiempos medidos** (antes de paralelizar fuentes): descarga de lo que faltaba ~37 min; Gold de 245 días
  en 4 grillas ~7 min; estáticas 4 min; `db import-lake` del año completo 3 min 40 s; `snapshot` 4 s.
* **Plan desde la base:** solo pide días recientes que aún pueden aparecer. 2 300 `missing` viejos (días
  sin pasada) ya no se re-consultan.
* **Esquema completo:** cada partición diaria y semanal trae las 73 variables, con NA donde la fuente no
  publicó.
* **`cell_id`** = `fila × ancho + columna` del rectángulo de la grilla (no es un contador): el primero de
  100 m es 107. Sirve para reconstruir el ráster exacto y cruzar escalas con `parent_cell_ids`.

## Cómo operar

```bash
# Ejercicio declarado en examples/cali_operational/pipeline.yaml (run:)
docker compose up -d db                                   # PostGIS en localhost:5433
export DATABASE_URL=postgresql://climate:climate@localhost:5433/climate
uv run climate-pipeline sync --config examples/cali_operational/pipeline.yaml      # llena la base hasta hoy
uv run climate-pipeline coverage --config examples/cali_operational/pipeline.yaml --output-format postgres
uv run climate-pipeline db status

# Lago local (Parquet), evidencias y snapshot
GEE_PROJECT=252843856929 bash examples/cali_operational/run_pipeline.sh            # Windows: run_pipeline.ps1
uv run climate-pipeline db import-lake                     # lago → base, sin volver a descargar
uv run climate-pipeline snapshot --config examples/cali_operational/pipeline.yaml --tag 2026-11-09 --archive

# Pruebas (sin TEST_DATABASE_URL, las de Postgres se omiten)
TEST_DATABASE_URL=postgresql://climate:climate@localhost:5433/climate uv run pytest
```

**Para entrenar y publicar**, usar siempre un snapshot (`data/snapshots/<tag>/snapshot.json` trae commit,
configuración, filas y SHA-256) y filtrar o ponderar los días `prelim` con `gold/product_status*.parquet`
(o con las vistas `climate.product_status*`). Al publicar datos, incluir `ATTRIBUTION.md`.

## Decisiones vigentes

* **Fuente de verdad sin bucket.** El catálogo vive con Gold: con Postgres, el plan sale de
  `ingest_manifest`. Gold se escribe **por columnas** en Postgres (`ON CONFLICT DO UPDATE` solo de las
  columnas de la corrida), así que un runner efímero nunca borra columnas de otras fuentes. Gold queda
  pendiente **por fuente** y día.
* **IMERG `final_lag_days = 420`:** el Final Run llega a Earth Engine ~13 meses después (último día
  `permanent` al 2026-11-09: 2025-09-30). Con 120 días, cada corrida habría re-bajado ~190 días provisionales.
* **`sync` no reintenta `missing` más viejos que `lookback_days`**; `run` sí, salvo `--no-retry-old-missing`.
* **psycopg 3 + COPY vía pyarrow**, sin ORM; migraciones SQL propias (sin Alembic) bajo advisory lock.
* **Un solo ejercicio y un solo lago (`data/`).** Se retiraron el ejemplo enero–mayo, el workflow de GCS,
  `config/pipeline.yaml`, los datos de CHIRPS del lago y las figuras redundantes. El lago viejo de
  enero–mayo quedó en el scratchpad de la sesión (temporal, se puede borrar).
* **Licencia:** MIT para el código; datos derivados bajo las condiciones de cada fuente (`ATTRIBUTION.md`),
  sugerido CC BY 4.0, salvo `urban_osm_*` (ODbL, share-alike). DOI verificados contra Crossref, doi.org y
  DataCite.

## Próxima sesión: prioridades

1. **Lo que depende de Andrés o del equipo:**
   * que Diego confirme la licencia MIT;
   * **reactivar GitHub Actions** (Settings → Actions) y revisar la primera corrida del CI;
   * para AWS: una cuenta con permisos y una **cuenta de servicio de Earth Engine** (llave JSON) registrada
     como no comercial.
2. **Sumar CHIRPS v3 a la precipitación** (no reemplazar IMERG). Es 0.05°, corregida con estaciones de
   Colombia, con preliminar a ~7 días y final a ~7 semanas: mejor referencia para entrenar y hacer
   downscaling de lluvia. Se activa con `sources.chirps.variables: [precipitation_flux]` y `chirps` en
   `run.sources`. Bajar 2026 desde CHC con 2 peticiones a la vez (responde 403 a clientes agresivos).
3. **Despliegue en AWS** (acordado, no iniciado): solo **Gold en Parquet**, grillas **1 km y 5 km**;
   Bronze y Silver viven solo en el runner.

   | Componente | Servicio | US$/mes (aprox.; verificar en AWS Pricing Calculator) |
   |---|---|---|
   | Runner diario (`sync`, ~15 min) | EventBridge Scheduler + ECS Fargate (2 vCPU, 4 GB) + ECR | ~0.8 |
   | Gold Parquet + catálogo (~0.7 GB a 10 años) | S3 | ~0.05 |
   | Descargas | CloudFront (1 TB/mes gratis) | ~0 |
   | API (opcional) | API Gateway HTTP + Lambda (DuckDB sobre S3) | 0–2 con uso de proyecto |
   | Secretos, logs y alertas | SSM Parameter Store, CloudWatch, SNS | ~0.3 |
   | **Total** | | **≈ 1.5–5** (sin NAT Gateway; us-east-1) |

   Orden de trabajo:
   1. Modo Parquet + S3 del runner: upsert por columnas también en Parquet (hoy reescribe la partición
      completa) y bajar/subir el catálogo y las particiones afectadas; `run.target_grids` → `{1km, 5km}`.
   2. Productos por **barrio (335) y comuna (22)**, calculados desde **Silver nativo** (los barrios son más
      pequeños que una celda de 1 km); diarios y semanales con `_ndays`.
   3. Exportadores a Parquet, NetCDF CF-1.8 y CSV por nivel y año; `catalog.json` para la página de
      descargas del front; guía de uso con ejemplos en Python y R.
   4. Terraform (S3, ECR, Fargate, Scheduler, IAM mínimo, Parameter Store, alarma con SNS) y CI con OIDC.
   5. API Lambda: `/variables`, `/catalog`, `/coverage`, `/series`, `/map`, `/download` y `/features`
      (rezagos de 1 a N semanas).
   6. Back-fill de 10 años (~1 h por año de datos; antes de 2018 GOES y Sentinel-2 quedan en NA).
4. **Mejoras del repo aún no hechas** (de la revisión del 2026-11-09):
   * Dependabot o Renovate (versiones fijadas exactas), `pre-commit` con ruff y pyright o mypy en el CI.
   * Separar `cli.py` (~830 líneas, 46 % de cobertura) por comando y `gee_datasets.py` (~1 200 líneas)
     por fuente; es reorganización sin cambio de comportamiento.
   * Una sola semántica de ventana: con IMERG a 420 días, `run --incremental` ya no puede actualizar los
     provisionales; conviene dejar solo la de `sync` (`start_date` + reintentos acotados).
   * Alertas por valores anómalos (cambio de unidades o datos raros del proveedor), no solo por huecos.
   * Logs en JSON para CloudWatch.
   * Estáticas urbanas: hoy usan el compuesto 2026 (año en curso); para entrenar, fijar 2025 y recalcular
     al cerrar el año.
   * Historial de Git pesado por binarios de ejemplos viejos: limpiarlo exige reescribir el historial
     (`git filter-repo`), así que solo si el clon molesta.

## Riesgos conocidos

* `postgis/postgis` no tiene imagen arm64: en Apple Silicon corre emulada (más lenta, funciona).
* Si se sube la grilla de 100 m a una base en la nube, ocupa ~2 GB por año: un plan gratuito de 0.5 GB no
  alcanza (1 km + 5 km: ~0.4 GB a 10 años).
* Earth Engine limita cuotas: un back-fill largo se parte por fuentes (`--sources`) o por rangos de años.
* MSWEP sigue sin credenciales de GloH2O (opcional, no seleccionada).

## Mapa del código

| Archivo | Qué hace |
|---|---|
| `src/climate_pipeline/config.py` | `Settings` (`.env`), fuentes y latencias (`DEFAULT_SOURCES`), `RunDefaults` (`run:`), `parallel_sources` |
| `src/climate_pipeline/cli.py` | `Exercise.resolve` (CLI + YAML) y todos los comandos, incluidos `sync`, `coverage`, `snapshot` y `db …` |
| `src/climate_pipeline/pipeline/orchestrator.py` | plan → Bronze → Silver (fuentes en paralelo) → Gold (días como bucle externo, Silver leído una vez) |
| `src/climate_pipeline/pipeline/sync.py` | `sync` contra Postgres e `import_lake` |
| `src/climate_pipeline/pipeline/coverage.py` | clasificación de días, huecos y atraso por fuente |
| `src/climate_pipeline/pipeline/provenance.py` | `product_status`, `weekly_status`, `create_snapshot` |
| `src/climate_pipeline/storage/postgres.py` | `Database` (migraciones, DDL bajo lock), `PostgresCatalog`, `PostgresGoldStore` (upsert por columnas, COPY) |
| `src/climate_pipeline/storage/migrations/*.sql` | 0001 esquema, 0002 PostGIS opcional, 0003 vistas de estado de producto |
| `src/climate_pipeline/storage/catalog.py` | contrato `Catalog` + `ParquetCatalog`; Gold pendiente por fuente |
| `src/climate_pipeline/processing/resample.py` | remuestreo con `rasterio.warp.reproject` directo |
| `examples/cali_operational/` | `pipeline.yaml`, `run_pipeline.sh` / `.ps1`, figuras, inventario y página de latencia |
| `LICENSE`, `CITATION.cff`, `ATTRIBUTION.md` | licencia del código, cómo citar y condiciones de cada fuente de datos |
| `.github/workflows/{ci,db-sync}.yml`, `Dockerfile`, `docker-compose.yml` | CI/CD e infraestructura local |
| `tests/` | 179 pruebas: offline con datos sintéticos + integración con PostGIS (`TEST_DATABASE_URL`) |
