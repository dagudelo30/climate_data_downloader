# Handoff — climate-pipeline (Dengue.IA Cali)

_Actualizado: 2026-11-09 · todo en `main` (PR #4 backend Postgres + ejercicio en tiempo real; PR #5 optimización y limpieza; PR #6 licencia, atribución y procedencia)_

## Objetivo

Backend de covariables **diarias y al día de hoy** para los modelos de downscaling (precipitación,
temperatura, HR, viento, radiación) y de dengue en Cali. Fuente de verdad: **PostgreSQL**. Un cron diario
consulta la base, baja lo que falte entre `run.start_date` y hoy, y deja Gold al día en 100 m, 500 m,
1 km y 5 km.

## Estado actual

| Pieza | Estado |
|---|---|
| Ejercicio único en tiempo real (`examples/cali_operational`, 2026-01-01 → hoy, 10 fuentes, 4 grillas, estáticas 2026) | ✅ corrido localmente, lago en `data/` (ver cifras abajo) |
| Backend PostgreSQL (migraciones, Gold particionado por grilla, catálogo `ingest_manifest`, PostGIS) | ✅ con pruebas de integración |
| `sync` (plan desde la base → descarga → Gold → semanas → estáticas → cobertura) | ✅ probado de punta a punta con fuentes simuladas |
| `coverage` (huecos y días de atraso por fuente y grilla) | ✅ |
| Configuración declarativa (`run:` en el YAML) | ✅ todos los comandos la leen |
| CI (`ci.yml`): lint + pruebas con servicio PostGIS + imagen Docker a GHCR | ✅ escrito, **no ejecutado aún en GitHub** (falta push) |
| CD (`db-sync.yml`): cron diario contra la base | ✅ escrito; **inactivo** hasta configurar secretos y `vars.DB_SYNC_ENABLED` |
| Base local (`docker compose up -d db`, puerto 5433) cargada con el ejercicio | ✅ 2026-01-01 → 2026-11-08, 4 grillas |

## Resultados del ejercicio (corrida del 2026-11-08/09)

Cali, **2026-01-01 → 2026-11-08**, 10 fuentes, 73 variables diarias + 24 estáticas, lago en `data/`
(Bronze 839 MB, Silver 220 MB, Gold 1.1 GB) y la misma información en PostgreSQL local (1.8 GB).

| Grilla | Celdas del AOI | Filas diarias | Filas semanales | Estáticas |
|---|---|---|---|---|
| 100 m | 12 535 | 3 910 920 | 576 610 | 12 535 |
| 500 m | 570 | 177 840 | 26 220 | 570 |
| 1 km | 160 | 49 920 | 7 360 | 160 |
| 5 km | 13 | 4 056 | 598 | 13 |

* **Cobertura** (`coverage`, igual en las 4 grillas): **0 huecos, ninguna fuente atrasada**. Los días sin
  dato son días que el proveedor no publicó (Landsat 269, Sentinel-1 229, VIIRS 29, GOES 10) o sin píxel
  válido por nubes (índices GEE 171, MODIS 6). Detalle en `outputs/cali_operational/README.md`.
* **Disponibilidad hoy** (`availability`): todas las fuentes completas hasta **2026-10-30** (semana
  epidemiológica **2026-W42**); GOES va a 1 día, IMERG/GEE/Sentinel-1 a 3, MODIS/SMAP a 4, AgERA5/ERA5-Land/Landsat
  a 8 y VIIRS a 10.
* **Tiempos**: descarga de lo que faltaba (enero a agosto + días recientes) ~37 min; Gold de 245 días en 4 grillas ~7 min;
  estáticas 4 min; carga del lago completo a Postgres (`db import-lake`) 3 min 40 s.
* **El plan desde la base** (`plan --output-format postgres`) hoy solo pide los días recientes que pueden
  aparecer todavía (reintentos dentro de `lookback_days`); 2 300 `missing` viejos ya no se re-consultan.
* **Esquema completo en cada partición**: cada Parquet diario/semanal trae las 73 variables (NA donde la
  fuente aún no publicó; antes un día con solo GOES tenía solo columnas de GOES). En Postgres, `db init`/`sync`
  crean todas las columnas desde el inicio. El lago existente se completó sin re-descargar.
* **IMERG**: el Final Run (`permanent`) en Earth Engine llega con ~13 meses de rezago (último día permanente:
  2025-09-30). `final_lag_days` pasó de 120 a 420; con 120, cada corrida habría re-bajado ~190 días
  provisionales sin poder mejorarlos.
* **`cell_id`** es el índice ráster `fila × ancho + columna` del rectángulo de la grilla (no un contador): por
  eso el primero de 100 m es 107. Permite reconstruir el ráster exacto y cruzar escalas con `parent_cell_ids`.

## Cómo operar

```bash
# Local, todo declarado en el YAML
docker compose up -d db                                   # PostGIS en localhost:5433
export DATABASE_URL=postgresql://climate:climate@localhost:5433/climate
uv run climate-pipeline sync --config examples/cali_operational/pipeline.yaml
uv run climate-pipeline coverage --config examples/cali_operational/pipeline.yaml --output-format postgres
uv run climate-pipeline db status

# Lago local (Parquet) → base, sin volver a descargar
uv run climate-pipeline db import-lake

# Ejercicio completo con evidencias (outputs/cali_operational)
GEE_PROJECT=252843856929 bash examples/cali_operational/run_pipeline.sh
```

Pruebas: `TEST_DATABASE_URL=postgresql://climate:climate@localhost:5433/climate uv run pytest`
(sin la variable, las de Postgres se omiten).

## Para activar el cron en GitHub

1. Una base PostgreSQL accesible desde GitHub (Cloud SQL, Supabase, Neon, RDS…), idealmente con PostGIS.
   **Tamaño:** la grilla de 100 m ocupa ~5 MB por día (≈1.5–2 GB al año); 500 m, 1 km y 5 km son
   despreciables. Un plan gratuito de 0.5 GB no alcanza con 100 m: usar un plan de ≥5 GB o quitar `100m`
   de `run.target_grids` en la base.
2. Secrets: `DATABASE_URL` (con `?sslmode=require`), `CDSAPI_KEY`, `GEE_SERVICE_ACCOUNT_JSON` (cuenta de
   servicio registrada en Earth Engine). Variables: `GEE_PROJECT`, `DB_SYNC_ENABLED=true`. Opcional:
   `GCP_WORKLOAD_IDENTITY` + `GCP_SERVICE_ACCOUNT` en lugar de la llave JSON.
3. Cargar la historia sin re-descargar: `DATABASE_URL=<nube> uv run climate-pipeline db import-lake`
   desde la máquina que tiene el lago local. Si no, la primera corrida del cron hace el back-fill
   completo (varias horas; el timeout del job es de 350 min).
4. Lanzar `DB sync` a mano (workflow_dispatch) una vez y revisar el resumen: plan, cobertura y `db status`.

## Decisiones tomadas en esta sesión

* **La base es la fuente de verdad, sin bucket.** El catálogo vive con Gold: con `--output-format
  postgres` (y siempre en `sync`) el plan sale de `ingest_manifest`. Gold se escribe **por columnas**
  (`ON CONFLICT DO UPDATE` solo de las columnas que trae la corrida), así que un runner efímero que solo
  tiene el Silver de hoy nunca borra columnas de otras fuentes.
* **Gold pendiente por fuente.** Antes había una sola marca de Gold por día; ahora el catálogo guarda qué
  fuentes se escribieron. Sin esto, un runner con el Silver de una fuente marcaba el día como hecho para
  todas. Las marcas viejas (`variable = "*"`) siguen valiendo para todas las fuentes.
* **`missing` viejos no se reintentan en `sync`** (más allá de `lookback_days` de cada fuente): evita
  re-consultar cada noche cientos de días sin pasada de Landsat/Sentinel-1. `run` sigue reintentándolos
  todos salvo `--no-retry-old-missing`.
* **Remuestreo directo con `rasterio.warp.reproject`** (sin el envoltorio de rioxarray): mismo resultado
  (diferencia máxima 2e-6 en float32) y 1.7× más rápido.
* **psycopg 3 + COPY vía pyarrow** para cargar y leer; sin ORM. Migraciones SQL versionadas propias
  (sin Alembic): son pocas y se aplican bajo advisory lock.
* **Limpieza del repo:** se retiraron los datos de CHIRPS del lago (no es fuente seleccionada), el ejemplo enero–mayo (`examples/cali_2026`,
  `outputs/cali_2026-01_2026-05`), el workflow de GCS (`data-pipeline.yml`, nunca configurado),
  `config/pipeline.yaml` (repetía los defaults del código), los mapas semana a semana (los cubre el GIF),
  las series diarias y los mapas de variables en 500 m/1 km/5 km (redundantes con los de 100 m).
* **Lago local único en `data/`** (antes `data/operational/` + un lago viejo en `data/`). El lago viejo
  de enero–mayo se movió fuera del repo (scratchpad de la sesión), no se borró a ciegas.
* **Literatura:** `docs/predictores_por_variable.md` ganó la sección de dengue (rezagos, no linealidad,
  dos estudios de Cali) y referencias de fuentes; todos los DOI se verificaron contra Crossref.

## Cierre de la sesión (PR #5): optimización y limpieza

* **Fuentes en paralelo** (`parallel_sources`, por defecto 4): la descarga dura aproximadamente lo de la fuente
  más lenta (la cola del CDS de AgERA5), no la suma. Earth Engine se inicializa una sola vez por proceso
  bajo un lock; el catálogo Postgres serializa el acceso a su conexión.
* **Silver leído una vez por día para todas las grillas** (días como bucle externo): 1.28× en Gold, que
  se suma al 1.7× del remuestreo directo con `rasterio.warp`; cada grilla sigue fallando por separado.
* **Código sin uso eliminado:** `static_covariates_frame` duplicaba el remuestreo del terreno; ahora
  hay una sola función (`terrain_to_grid`) y la prueba usa el camino de producción. Vulture no encuentra
  otro código muerto (los demás avisos son comandos y extractores registrados por decorador).
* **Documentación:** README con instalación en macOS/Linux y Windows y una sección de rendimiento;
  corregido el enlace roto del DOI de Lowe et al. (2021) en `docs/predictores_por_variable.html`.

## Licencia, atribución y procedencia (PR #6)

* **`LICENSE` (MIT)** con titulares Diego Fernando Agudelo y Andrés Felipe Palomino Montezuma: el repo
  original (`dagudelo30/climate_data_downloader`) no tenía licencia y Diego tiene commits en el historial.
  **Pendiente: que Diego confirme la licencia MIT.** También `CITATION.cff` y metadatos en `pyproject.toml`
  (versión 0.2.0).
* **`ATTRIBUTION.md`**: licencia, líneas de atribución obligatorias y DOI de cada fuente. Los DOI se
  verificaron contra doi.org y DataCite; donde el DOI no correspondía a la versión usada (SMAP v7, DEM
  TanDEM-X, GOES L1b) se dejó el enlace oficial. Se sugiere CC BY 4.0 para los datos derivados, salvo
  `urban_osm_*` (ODbL, share-alike).
* **Estado de producto:** `gold/product_status*.parquet` y vistas Postgres `product_status` y
  `product_status_epiweekly` (migración `0003`). Hoy IMERG tiene 307 días `prelim` y 0 `final` en 2026;
  todas las demás fuentes son finales.
* **Snapshots:** `climate-pipeline snapshot --tag <fecha>` congela Gold con hard links, SHA-256, commit y
  configuración. Primer snapshot: `data/snapshots/2026-11-09/` (commit `c89f0d9`, 2026-01-01 → 2026-11-08).
  Usar el tag del snapshot en los papers y en el entrenamiento.

## Recomendación sobre la precipitación (no implementada)

No reemplazar IMERG: su Late Run (2 días) es el estándar operativo, y lo que llega tarde (~13 meses) es solo
la versión final. Para entrenar y hacer downscaling de lluvia conviene **sumar CHIRPS v3** (0.05°, corregida
con estaciones de Colombia, preliminar ~7 días y final ~7 semanas). Hoy la regla de redundancia la descarta
por latencia; se activa con `sources.chirps.variables: [precipitation_flux]` y `chirps` en `run.sources`. Hay
que bajar 2026 desde CHC con 2 peticiones a la vez (CHC responde HTTP 403 a clientes agresivos).

## Próximo paso acordado: despliegue en AWS (no iniciado)

Decisión de la conversación del 2026-11-09: guardar solo **Gold en Parquet**, con grillas **1 km y
5 km**. Bronze y Silver viven solo en el runner. El front lo hace Andrés; el backend debe dejar productos
listos para descargar y graficar.

| Componente | Servicio | US$/mes (aprox., verificar en AWS Pricing Calculator) |
|---|---|---|
| Runner diario (`sync`, ~15 min) | EventBridge Scheduler + ECS Fargate (2 vCPU, 4 GB) + ECR | ~0.8 |
| Gold Parquet + catálogo (~0.7 GB a 10 años) | S3 | ~0.05 |
| Descargas | CloudFront (1 TB/mes gratis) | ~0 |
| API (opcional) | API Gateway HTTP + Lambda (DuckDB sobre S3) | 0–2 con uso de proyecto |
| Secretos, logs y alertas | SSM Parameter Store, CloudWatch, SNS | ~0.3 |
| **Total** | | **≈ 1.5–5** (sin NAT Gateway; región us-east-1) |

Trabajo pendiente, en orden:
1. **Modo Parquet + S3 del runner:** upsert por columnas también en Parquet (hoy reescribe la partición
   completa con todas las columnas), y bajar/subir el catálogo y las particiones afectadas.
   `run.target_grids` pasa a `{1km, 5km}`.
2. **Productos por barrio (335) y comuna (22):** calculados desde **Silver nativo**, no desde 1 km (los
   barrios son más pequeños que una celda), diarios y semanales con `_ndays`.
3. **Exportadores** a Parquet, NetCDF CF-1.8 (tiempo × y × x; tiempo × unidad) y CSV por nivel y año,
   más un `catalog.json` (archivo, nivel, formato, periodo, tamaño, checksum) y una guía de uso con
   ejemplos en Python y R.
4. **Terraform:** S3, ECR, Fargate, Scheduler, IAM mínimo, Parameter Store y alarma con SNS; CI con OIDC
   hacia AWS (sin llaves guardadas).
5. **API Lambda:** `/variables`, `/catalog`, `/coverage`, `/series`, `/map`, `/download` y `/features`
   (rezagos de 1 a N semanas).
6. Back-fill de 10 años (~1 h por año de datos; antes de 2018 GOES y Sentinel-2 quedan en NA).

Se necesita del equipo: una cuenta AWS con permisos, una **cuenta de servicio de Earth Engine** (llave
JSON) registrada como no comercial y **reactivar GitHub Actions** en el repo (hoy está desactivado, por eso
el CI no corrió en el PR #4).

## Pendientes y riesgos

* **Publicar los Parquet de Gold 2026** como Release si se quieren compartir (la Release
  `cali-operativo-2026-11-08` del README anterior tenía solo 30 ago – 7 nov).
* **GitHub Actions está desactivado** en el repo: ni el CI ni el cron corren. Al reactivarlo, revisar la
  primera ejecución (servicio PostGIS y publicación de la imagen en GHCR). Todo se verificó localmente
  (ruff, formato, lockfile, 175 pruebas con PostGIS).
* `postgis/postgis` no tiene imagen arm64: en Apple Silicon corre emulada (más lenta, funciona).
* GEE limita cuotas: el back-fill de un año completo en CI puede tardar; `sync` se puede partir por
  fuentes (`--sources`) con `workflow_dispatch`.
* MSWEP sigue sin credenciales de GloH2O (fuente opcional, no seleccionada).
* Las estáticas urbanas usan compuestos de 2026 (año en curso, enero → hoy): al cerrar el año conviene
  recalcularlas (`build-static --year 2026` o borrar `static_covariates` y dejar que `sync` las rehaga).

## Mapa del código (lo nuevo)

| Archivo | Qué hace |
|---|---|
| `src/climate_pipeline/storage/postgres.py` | `Database` (conexión, migraciones, DDL bajo lock, diccionario), `PostgresCatalog`, `PostgresGoldStore` (upsert por columnas, lectura vía COPY, `stale_weeks`) |
| `src/climate_pipeline/storage/migrations/*.sql` | esquema `climate` (tablas, particiones, vistas) y PostGIS opcional |
| `src/climate_pipeline/storage/catalog.py` | contrato `Catalog` + `ParquetCatalog`; Gold pendiente por fuente |
| `src/climate_pipeline/pipeline/sync.py` | `sync` e `import_lake` |
| `src/climate_pipeline/pipeline/coverage.py` | clasificación de días, huecos y atraso |
| `src/climate_pipeline/config.py` | `RunDefaults` (`run:`), `DATABASE_URL` / `DATABASE_SCHEMA` |
| `src/climate_pipeline/cli.py` | `Exercise.resolve` (CLI + YAML), comandos `sync`, `coverage`, `db …` |
| `.github/workflows/{ci,db-sync}.yml`, `Dockerfile`, `docker-compose.yml` | CI/CD e infraestructura local |
| `tests/test_postgres.py`, `test_pipeline_postgres.py`, `test_coverage_and_catalog.py` | unitarias + integración |
