# Diagnóstico de máscara de nubes para NDVI/NDWI (GEE, Cali, enero a mayo)

Generado por `examples/cali_2026/gee_cloud_mask_diagnostic.py` sobre las 160 celdas de 1 km.

## Celdas-semana epidemiológica con al menos 1 día despejado

| Opción | 2024 | 2025 | 2026 |
|---|---|---|---|
| MODIS sin máscara (código original) | 100.0% | 100.0% | 100.0% |
| MODIS `strict` (nube, sombra, nube interna, adyacente) | 28.2% | 17.1% | 27.5% |
| MODIS `standard` (nube, sombra, nube interna) — por defecto | 69.2% | 46.0% | 56.7% |
| MODIS solo estado de nube MOD35 | 85.0% | 70.1% | 76.4% |
| Sentinel-2 + Cloud Score+ (cs_cdf ≥ 0.6) | 76.3% | 70.0% | 68.7% |
| MODIS `standard` + Sentinel-2 (cualquiera de los dos) | 88.9% | 76.1% | 77.8% |

Detalle por celda-día y con ≥2 días: `gee_cloud_mask_coverage.csv`.

## NDVI de los píxeles-día MODIS que agrega cada nivel de máscara

Las nubes acercan el NDVI a 0: un nivel que agrega muchos valores bajos está contaminado.

| Año | Clase | Píxeles-día | NDVI mediano | NDVI < 0.2 |
|---|---|---|---|---|
| 2024 | despejado (pasa `strict`) | 15,818 | 0.67 | 0.4% |
| 2024 | solo marcado adyacente a nube | 25,650 | 0.57 | 2.4% |
| 2024 | sombra o nube interna | 29,078 | 0.37 | 15.5% |
| 2024 | nublado (MOD35) | 228,358 | 0.12 | 62.5% |
| 2025 | despejado (pasa `strict`) | 7,073 | 0.67 | 0.1% |
| 2025 | solo marcado adyacente a nube | 12,837 | 0.62 | 2.0% |
| 2025 | sombra o nube interna | 15,735 | 0.42 | 14.9% |
| 2025 | nublado (MOD35) | 265,613 | 0.07 | 71.9% |
| 2026 | despejado (pasa `strict`) | 10,517 | 0.67 | 0.9% |
| 2026 | solo marcado adyacente a nube | 12,376 | 0.62 | 2.4% |
| 2026 | sombra o nube interna | 16,351 | 0.42 | 12.4% |
| 2026 | nublado (MOD35) | 264,811 | 0.12 | 71.7% |

**Decisión:** `cloud_mask: standard` por defecto (se conservan los píxeles solo adyacentes a nube, casi limpios; se quitan sombra y nube interna, contaminados) y Sentinel-2 como segundo producto. El código original no enmascaraba: sin huecos, pero la mayoría de sus valores eran nubes.
