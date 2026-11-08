# Control de calidad — Cali, enero a mayo de 2026

**71 / 71 chequeos superados.**

| Estado | Alcance | Chequeo | Detalle |
|---|---|---|---|
| ✅ | 1km | days covered | 151 of 151 days |
| ✅ | 1km | same cells every day | 160..160 cells/day, 160 AOI cells |
| ✅ | 1km | unique (cell_id, date) | 24160 rows |
| ✅ | 1km | unique (cell_id, epi_year, epi_week) | 3680 rows |
| ✅ | 1km | same cells every epi week (NA when no value, never a missing row) | 160..160 cells/week over 23 weeks, 160 AOI cells |
| ✅ | 1km | MODIS vs Sentinel-2 NDVI: same spatial pattern on common days (median r > 0.6) | median spatial r = 0.81 over 11 days (1105 cell-days); S2 - MODIS bias = -0.087 (separate columns, do not mix without calibration) |
| ✅ | 1km | physical range agera5_dewpoint_mean | [14.222, 19.179] ⊂ [-90, 40] degC; 0.0% null |
| ✅ | 1km | physical range agera5_precip | [0.010, 63.170] ⊂ [0, 2000] mm d-1; 0.0% null |
| ✅ | 1km | physical range agera5_rh_06h | [86.377, 92.533] ⊂ [0, 100] %; 0.0% null |
| ✅ | 1km | physical range agera5_rh_15h | [68.330, 90.217] ⊂ [0, 100] %; 0.0% null |
| ✅ | 1km | physical range agera5_solar_radiation | [6.314, 23.161] ⊂ [0, 45] MJ m-2 d-1; 0.0% null |
| ✅ | 1km | physical range agera5_temp_max | [20.123, 26.983] ⊂ [-90, 60] degC; 0.0% null |
| ✅ | 1km | physical range agera5_temp_mean | [17.717, 22.881] ⊂ [-90, 60] degC; 0.0% null |
| ✅ | 1km | physical range agera5_temp_min | [14.993, 18.862] ⊂ [-90, 60] degC; 0.0% null |
| ✅ | 1km | physical range agera5_vapour_pressure | [16.290, 22.319] ⊂ [0, 80] hPa; 0.0% null |
| ✅ | 1km | physical range agera5_wind_speed | [0.835, 1.184] ⊂ [0, 75] m s-1; 0.0% null |
| ✅ | 1km | physical range chirps_precip | [0.000, 64.154] ⊂ [0, 2000] mm d-1; 0.0% null |
| ✅ | 1km | physical range gee_ndvi | [0.060, 0.999] ⊂ [-1, 1] 1; 86.3% null |
| ✅ | 1km | physical range gee_ndwi | [-0.320, 0.472] ⊂ [-1, 1] 1; 88.0% null |
| ✅ | 1km | physical range gee_s2_ndvi | [-0.121, 0.872] ⊂ [-1, 1] 1; 89.1% null |
| ✅ | 1km | physical range gee_s2_ndwi | [-0.155, 0.391] ⊂ [-1, 1] 1; 89.1% null |
| ✅ | 1km | gee_ndvi within [-1, 1] | min=0.060 max=0.999 n=3321 |
| ✅ | 1km | gee_ndwi within [-1, 1] | min=-0.320 max=0.472 n=2911 |
| ✅ | 1km | gee_s2_ndvi within [-1, 1] | min=-0.121 max=0.872 n=2633 |
| ✅ | 1km | gee_s2_ndwi within [-1, 1] | min=-0.155 max=0.391 n=2633 |
| ✅ | 1km | agera5_temp_min <= agera5_temp_mean | 0 violations in 24160 rows |
| ✅ | 1km | agera5_temp_mean <= agera5_temp_max | 0 violations in 24160 rows |
| ✅ | 1km | agera5_dewpoint_mean <= agera5_temp_mean | 0 violations in 24160 rows |
| ✅ | 1km | temperatures plausible for Cali (5-40 °C) | Tmin≥15.0, Tmax≤27.0 |
| ✅ | 1km | weekly table == independent re-aggregation | all catalog columns, max |Δ|=1.53e-05 (float32) |
| ✅ | 1km | Parquet == DuckDB (gold_daily) | 24160 rows |
| ✅ | 1km | make_raster round trip | 160 cells, NaN outside AOI |
| ✅ | 1km | elevation plausible (Cali ~900-1500 m) | 948-1302 m |
| ✅ | 5km | days covered | 151 of 151 days |
| ✅ | 5km | same cells every day | 13..13 cells/day, 13 AOI cells |
| ✅ | 5km | unique (cell_id, date) | 1963 rows |
| ✅ | 5km | unique (cell_id, epi_year, epi_week) | 299 rows |
| ✅ | 5km | same cells every epi week (NA when no value, never a missing row) | 13..13 cells/week over 23 weeks, 13 AOI cells |
| ✅ | 5km | MODIS vs Sentinel-2 NDVI: same spatial pattern on common days (median r > 0.6) | median spatial r = 0.76 over 11 days (121 cell-days); S2 - MODIS bias = -0.101 (separate columns, do not mix without calibration) |
| ✅ | 5km | physical range agera5_dewpoint_mean | [14.153, 19.181] ⊂ [-90, 40] degC; 0.0% null |
| ✅ | 5km | physical range agera5_precip | [0.010, 63.170] ⊂ [0, 2000] mm d-1; 0.0% null |
| ✅ | 5km | physical range agera5_rh_06h | [86.140, 92.533] ⊂ [0, 100] %; 0.0% null |
| ✅ | 5km | physical range agera5_rh_15h | [68.330, 90.683] ⊂ [0, 100] %; 0.0% null |
| ✅ | 5km | physical range agera5_solar_radiation | [6.304, 23.161] ⊂ [0, 45] MJ m-2 d-1; 0.0% null |
| ✅ | 5km | physical range agera5_temp_max | [19.854, 26.982] ⊂ [-90, 60] degC; 0.0% null |
| ✅ | 5km | physical range agera5_temp_mean | [17.709, 22.887] ⊂ [-90, 60] degC; 0.0% null |
| ✅ | 5km | physical range agera5_temp_min | [15.123, 18.877] ⊂ [-90, 60] degC; 0.0% null |
| ✅ | 5km | physical range agera5_vapour_pressure | [16.249, 22.325] ⊂ [0, 80] hPa; 0.0% null |
| ✅ | 5km | physical range agera5_wind_speed | [0.835, 1.171] ⊂ [0, 75] m s-1; 0.0% null |
| ✅ | 5km | physical range chirps_precip | [0.000, 64.077] ⊂ [0, 2000] mm d-1; 0.0% null |
| ✅ | 5km | physical range gee_ndvi | [0.136, 0.805] ⊂ [-1, 1] 1; 78.6% null |
| ✅ | 5km | physical range gee_ndwi | [-0.151, 0.313] ⊂ [-1, 1] 1; 80.5% null |
| ✅ | 5km | physical range gee_s2_ndvi | [0.052, 0.656] ⊂ [-1, 1] 1; 88.1% null |
| ✅ | 5km | physical range gee_s2_ndwi | [-0.127, 0.379] ⊂ [-1, 1] 1; 88.1% null |
| ✅ | 5km | gee_ndvi within [-1, 1] | min=0.136 max=0.805 n=421 |
| ✅ | 5km | gee_ndwi within [-1, 1] | min=-0.151 max=0.313 n=383 |
| ✅ | 5km | gee_s2_ndvi within [-1, 1] | min=0.052 max=0.656 n=233 |
| ✅ | 5km | gee_s2_ndwi within [-1, 1] | min=-0.127 max=0.379 n=233 |
| ✅ | 5km | agera5_temp_min <= agera5_temp_mean | 0 violations in 1963 rows |
| ✅ | 5km | agera5_temp_mean <= agera5_temp_max | 0 violations in 1963 rows |
| ✅ | 5km | agera5_dewpoint_mean <= agera5_temp_mean | 0 violations in 1963 rows |
| ✅ | 5km | temperatures plausible for Cali (5-40 °C) | Tmin≥15.1, Tmax≤27.0 |
| ✅ | 5km | weekly table == independent re-aggregation | all catalog columns, max |Δ|=1.53e-05 (float32) |
| ✅ | 5km | Parquet == DuckDB (gold_daily) | 1963 rows |
| ✅ | 5km | make_raster round trip | 13 cells, NaN outside AOI |
| ✅ | 5km | elevation plausible (Cali ~900-1500 m) | 950-1362 m |
| ✅ | agera5 | K → °C reproducible from Bronze | raw units=K, max |(K-273.15) - silver|=5.72e-06 |
| ✅ | both | 1 km vs 5 km AOI-mean T (°C) | max daily |Δ|=0.117 °C |
| ✅ | both | 1 km vs 5 km AOI-mean CHIRPS correlation | r=0.9992 |
| ✅ | both | CHIRPS vs AgERA5 daily correlation peaks at lag 0 | lag -3: r=0.02, lag -2: r=0.02, lag -1: r=0.04, lag +0: r=0.27, lag +1: r=0.00, lag +2: r=-0.06, lag +3: r=-0.04 |
| ✅ | agera5 | Bronze file date == time coordinate (AgERA5) | 151 files, 0 mismatches |
