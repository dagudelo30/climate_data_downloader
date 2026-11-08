# Áreas de interés (AOI)

Cualquier Shapefile, GeoJSON o GPKG con CRS sirve como `--aoi-path`. Se incluye el de Cali:

| Carpeta | Contenido | Entidades | CRS | Fuente |
|---|---|---|---|---|
| `cali/` | Barrios del perímetro urbano de Santiago de Cali (`id_barrio`, `barrio`, `comuna`) | 335 | EPSG:6249 (MAGNA-SIRGAS / Cali urban grid) | IDESC, Alcaldía de Cali: capa WFS `idesc:mc_barrios` de `ws-idesc.cali.gov.co/geoserver` |

`cali.dbf` está codificado en ISO-8859-1 (declarado en `cali.cpg`). El pipeline reproyecta el
AOI a la grilla de cada fuente y no depende del CRS de origen.
