# Predictores por variable objetivo

Qué covariables usa la literatura para modelar o hacer downscaling de cada variable, y en qué
columnas de Gold están. Todas las columnas dinámicas son **diarias** y vienen de fuentes de
**0.1° (≈11 km) o más finas**, operativas hasta hoy (ver `climate-pipeline availability`). Cuando
dos fuentes miden lo mismo queda una sola (ver [regla de redundancia](#regla-de-redundancia)).

## Precipitación

| Predictor en la literatura | Por qué | Columnas Gold |
|---|---|---|
| Precipitación satelital de referencia | Base de todos los esquemas de downscaling y fusión con pluviómetros | `chirps_precip` (5.5 km) |
| Temperatura de brillo IR y duración de nube fría | Topes fríos ⇔ convección profunda; es el principio de CHIRPS/TAMSAT y PERSIANN-CCS | `goes_bt_c13`, `goes_bt_c13_min`, `goes_cold_cloud_frac_235`, `goes_cold_cloud_frac_220` |
| Canales de vapor de agua y diferencias de canales (ABI 8, 10, 11, 14, 15) | Mejores predictores de lluvia convectiva con ML en GOES-16 | `goes_bt_c08`, `goes_bt_c09`, `goes_bt_c10`, `goes_bt_c11`, `goes_bt_c14`, `goes_bt_c15`, `goes_btd_c08_c13`, `goes_btd_c13_c15`, `goes_btd_c11_c14` |
| Agua precipitable | Humedad disponible para precipitar | `modis_column_wv` |
| Temperatura de superficie (LST) | La variable más importante en varios estudios de downscaling con RF | `modis_lst_day_terra`, `modis_lst_night_terra`, `modis_lst_day_aqua`, `modis_lst_night_aqua`, `landsat_lst` |
| NDVI / EVI | Respuesta de la vegetación a la lluvia acumulada | `gee_ndvi`, `gee_evi`, `gee_s2_ndvi`, `gee_s2_evi` |
| Humedad del suelo | Restricción física de la lluvia reciente; entre los predictores más influyentes | `smap_sm_surface`, `smap_sm_rootzone`, `sentinel1_vv_db` |
| Elevación, pendiente, orientación, TPI | Efecto orográfico | `static_elevation_m`, `static_slope_deg`, `static_northness`, `static_eastness`, `static_tpi_500m`, `static_tpi_2km` |
| Nubosidad y horas con lluvia | Ocurrencia de lluvia | `modis_cloud_frac`, `agera5_precip_hours_frac`, `goes_refl_c02_day` |
| Viento y presión | Advección de humedad y convergencia | `agera5_wind_speed`, `era5land_u_wind_10m`, `era5land_v_wind_10m`, `era5land_surface_pressure` |
| Autocorrelación espacial (vecinos, lat/lon) | El factor más influyente en la fusión con RF espacial | `latitude`, `longitude` en Gold; vecinos con `cell_id` y `parent_cell_ids` entre escalas |

**Papers:**

- [Jing et al. (2016), downscaling de TRMM con NDVI, DEM y LST mediante RF, *Remote Sensing*](https://doi.org/10.3390/rs8080655)
- [Revisión del downscaling espacial de precipitación satelital, *Environmental Earth Sciences* (2023)](https://link.springer.com/article/10.1007/s12665-023-11115-7)
- [Downscaling de GPM con ML en montaña, Chongqing, *Atmospheric Research* (2024)](https://www.sciencedirect.com/science/article/abs/pii/S0169809524004800)
- [Downscaling con múltiples productos y ML explicable, Turquía, *STOTEN* (2025)](https://www.sciencedirect.com/science/article/abs/pii/S0048969725021801)
- [ML de alta resolución con restricción de humedad del suelo (2026)](https://link.springer.com/article/10.1007/s41748-026-01138-y)
- [Precipitación con bandas 8, 10, 11, 14 y 15 de GOES-16 ABI y diferencias de brillo](https://par.nsf.gov/servlets/purl/10515825)
- [Texturas y diferencias de brillo como predictores de precipitación (NOAA)](https://repository.library.noaa.gov/view/noaa/45096/noaa_45096_DS1.pdf)
- [Canal de vapor de agua de 6.2 µm con IR de 11.2 µm para clasificar precipitación](https://essopenarchive.org/doi/full/10.1002/essoar.10505957.1)
- [U-Net con GOES-16, radar y pluviómetros en los Andes colombianos, *Ingeniería y Competitividad*](https://revistaingenieria.univalle.edu.co/index.php/ingenieria_y_competitividad/article/download/15228/19304/77721)
- [RF-MEP: fusión de CHIRPS, ERA5-Land e IMERG con pluviómetros, *Remote Sensing* (2023)](https://doi.org/10.3390/rs15174230)
- [Fusión espacial con RF de IMERG, GSMaP, CHIRPS y PERSIANN con pluviómetros](https://aminer.org/pub/65e6dcdb13fb2c6cf63b5462/downscaling-and-merging-multiple-satellite-precipitation-products-and-gauge-observations-using-random)


## Temperatura media, máxima y mínima

| Predictor | Por qué | Columnas Gold |
|---|---|---|
| Temperatura de referencia de reanálisis | Campo base | `agera5_temp_mean`, `agera5_temp_max`, `agera5_temp_min`, `agera5_temp_day_max`, `agera5_temp_day_mean`, `agera5_temp_night_mean`, `agera5_temp_night_min` |
| LST diurna y nocturna (Terra 10:30/22:30, Aqua 13:30/01:30, Landsat 30 m); LST del día anterior | La LST diurna explica Tmáx y la nocturna Tmín; la LST del día anterior también ayuda | `modis_lst_*`, `landsat_lst`, `era5land_skin_temp` |
| NDVI, superficie construida | Enfriamiento por vegetación e isla de calor urbana | `gee_*ndvi`, `urban_*` |
| Elevación, pendiente, orientación, TPI | Gradiente térmico vertical y acumulación de aire frío en valles | `static_*` |
| Radiación solar, nubosidad | Balance de energía | `agera5_solar_radiation`, `modis_cloud_frac`, `goes_refl_c02_day`, `era5land_thermal_radiation_down`, `era5land_net_thermal_radiation` |
| Flujos de superficie | Partición entre calor sensible y evaporación | `era5land_sensible_heat_flux`, `era5land_evaporation`, `era5land_albedo` |

**Papers:**

- [Yoo et al. (2018), Tmáx y Tmín urbanas con LST de MODIS y RF, *ISPRS J. Photogramm.*](https://scholarworks.unist.ac.kr/handle/201301/23828)
- [Tmáx y Tmín con LST de Aqua y Terra, NDVI y radiación (Argentina)](https://revistas.unc.edu.ar/index.php/agris/article/download/33225/41049/153605)
- [Uscamayta-Ferrano et al. (2025), downscaling de CHIRTS con LST, NDVI y terreno, *Atmosphere*](https://www.mdpi.com/2073-4433/16/10/1188)
- [Crosson et al. (2020), Tmáx de NLDAS-2 con LST de MODIS, *PLOS ONE*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6964900/)
- [XGBoost frente a aprendizaje profundo para downscaling de temperatura, *Remote Sensing* (2024)](https://www.mdpi.com/2072-4292/16/8/1314)


## Humedad relativa

| Predictor | Columnas Gold |
|---|---|
| HR de referencia a distintas horas, máxima y mínima | `agera5_rh_06h`, `agera5_rh_09h`, `agera5_rh_12h`, `agera5_rh_15h`, `agera5_rh_18h`, `agera5_rh_max`, `agera5_rh_min` |
| Punto de rocío, presión de vapor, déficit de presión de vapor | `agera5_dewpoint_mean`, `agera5_vapour_pressure`, `agera5_vpd_tmax` |
| Agua precipitable y vapor en niveles medios y altos | `modis_column_wv`, `goes_bt_c08`, `goes_bt_c09`, `goes_bt_c10` |
| Humedad del suelo, evaporación y vegetación | `smap_sm_*`, `era5land_evaporation`, `agera5_et0`, `gee_*ndvi`, `gee_*ndwi` |
| Terreno (barlovento y sotavento) | `static_*` |

**Papers:**

- [CHELSA v2.0: HR por barlovento/sotavento y radiación corregida por terreno](https://www.chelsa-climate.org/models/chelsa_v2.0)
- [CHELSA-W5E5: forzantes diarios a 1 km (HR, viento, radiación)](https://www.isimip.org/documents/755/ISIMIP_Karger.pdf)
- [XGBoost frente a aprendizaje profundo para downscaling de temperatura, *Remote Sensing* (2024)](https://www.mdpi.com/2072-4292/16/8/1314)


## Viento

| Predictor | Columnas Gold |
|---|---|
| Velocidad y componentes de referencia | `agera5_wind_speed`, `era5land_u_wind_10m`, `era5land_v_wind_10m` |
| TPI a dos radios y rugosidad del terreno (enfoque del downscaling de ERA5 del DLR) | `static_tpi_500m`, `static_tpi_2km`, `static_tri_m` |
| Rugosidad urbana: altura y densidad de edificios, vías | `urban_buildings_height_m`, `urban_ghsl_built_height_m`, `urban_buildings_frac`, `urban_osm_*` |
| Presión y gradiente térmico | `era5land_surface_pressure`, `era5land_sensible_heat_flux` |

**Papers:**

- [Downscaling del viento de ERA5 con TPI a 5 y 75 km (DLR)](https://zenodo.org/records/8100208)
- [CHELSA-W5E5: forzantes diarios a 1 km (HR, viento, radiación)](https://www.isimip.org/documents/755/ISIMIP_Karger.pdf)
- [XGBoost frente a aprendizaje profundo para downscaling de temperatura, *Remote Sensing* (2024)](https://www.mdpi.com/2072-4292/16/8/1314)


## Radiación solar

| Predictor | Columnas Gold |
|---|---|
| Radiación de referencia | `agera5_solar_radiation` |
| Reflectancia visible geoestacionaria (albedo de nubes) e IR | `goes_refl_c02_day`, `goes_bt_c13`, `goes_cold_cloud_frac_235` |
| Nubosidad, aerosoles y vapor | `modis_cloud_frac`, `modis_aod_055`, `modis_column_wv` |
| Onda larga y albedo de superficie | `era5land_thermal_radiation_down`, `era5land_net_thermal_radiation`, `era5land_albedo` |
| Exposición y orientación del terreno | `static_slope_deg`, `static_northness`, `static_eastness` |

**Papers:**

- [Chen, He y Liang (2022), radiación neta diaria desde GOES-16 ABI con RF, *IEEE TGRS*](https://vufind.lboro.ac.uk/PrimoRecord/cdi_proquest_journals_2637441323)
- [Radiación solar con GOES-R y XGBoost (U. Delaware, 2024)](https://udspace.udel.edu/handle/19716/35116)
- [CHELSA v2.0: HR por barlovento/sotavento y radiación corregida por terreno](https://www.chelsa-climate.org/models/chelsa_v2.0)


## Regla de redundancia

Si dos fuentes miden la misma magnitud con el mismo muestreo temporal, queda **una**:

1. la **más reciente** (menor latencia; 1 día de diferencia se considera empate);
2. si empatan, la de **mejor resolución original**;
3. si también empatan, la de **más sentido** (por ejemplo, AgERA5 agrega por día solar local y
   ERA5-Land por día UTC, que en Colombia va de 19:00 a 19:00).

Productos con muestreo distinto se conservan porque se complementan (MODIS diario a 500 m y
Sentinel-2 a 10-20 m cada ~5 días; LST de MODIS diaria a 1 km y de Landsat a 30 m cada ~8 días).
También se descartan columnas que se calculan exactamente de otras ya conservadas (P − ET₀).
`uv run climate-pipeline variables` imprime cada decisión; `deduplicate: false` las conserva todas.

Por esta regla la precipitación es **IMERG** (2 días) y quedan sin columnas propias CHIRPS
(7-8 días), GSMaP y MSWEP (empatan con IMERG en 0.1° y latencia). Se pueden volver a pedir con
`sources.<fuente>.variables`.
