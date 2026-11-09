# Atribución de datos

El **código** está bajo licencia MIT ([`LICENSE`](LICENSE)). Los **datos** que produce el pipeline
(Bronze, Silver, Gold, snapshots, descargas) son productos **derivados** de las fuentes de abajo y
heredan sus condiciones. Quien publique o redistribuya Gold debe:

1. incluir este archivo (o las líneas de atribución de cada fuente usada) junto con los datos;
2. citar el software ([`CITATION.cff`](CITATION.cff)) y los datasets de la tabla;
3. respetar las condiciones particulares señaladas (OpenStreetMap, Copernicus, Earth Engine).

**Licencia sugerida para los datos derivados:** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/),
salvo las columnas `urban_osm_*`, que se derivan de OpenStreetMap (ver abajo).

## Líneas de atribución obligatorias

Copiar en la publicación o en la página de descargas las que correspondan a las columnas usadas:

* **Copernicus Climate Change Service (AgERA5, ERA5-Land):** "Contains modified Copernicus Climate Change
  Service information [2026]. Neither the European Commission nor ECMWF is responsible for any use that
  may be made of the Copernicus information or data it contains."
* **Copernicus Sentinel (Sentinel-1, Sentinel-2):** "Contains modified Copernicus Sentinel data [2026]."
* **Copernicus DEM:** "Produced using Copernicus WorldDEM-90 © DLR e.V. 2010-2014 and © Airbus Defence and
  Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved."
* **OpenStreetMap (`urban_osm_*`):** "© OpenStreetMap contributors", datos bajo
  [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/). Una base de datos derivada que se haga
  pública debe ofrecerse también bajo ODbL; las demás columnas no quedan afectadas si se distribuyen por
  separado.
* **NASA (IMERG, MODIS, SMAP, VIIRS Black Marble) y NOAA (GOES):** datos abiertos sin restricciones de uso;
  NASA y NOAA piden citar el dataset (DOI de la tabla) y reconocer al proveedor.
* **Google Earth Engine:** los datasets de la tabla se leen a través de Earth Engine bajo sus
  [términos](https://earthengine.google.com/terms/) (uso no comercial o licencia comercial); cada dataset
  conserva su propia licencia.

## Fuentes, condiciones y citas

| Columnas | Dataset | Proveedor | Condiciones | Cita / DOI |
|---|---|---|---|---|
| `imerg_*` | GPM IMERG Late Run V07 (Earth Engine `NASA/GPM_L3/IMERG_V07`) | NASA GES DISC | abierto, sin restricciones | [10.5067/GPM/IMERG/3B-HH-L/07](https://doi.org/10.5067/GPM/IMERG/3B-HH-L/07); Final Run [10.5067/GPM/IMERG/3B-HH/07](https://doi.org/10.5067/GPM/IMERG/3B-HH/07) |
| `agera5_*` | AgERA5 v2.0 (`sis-agrometeorological-indicators`) | Copernicus C3S / ECMWF | [licencia del dataset en el CDS](https://cds.climate.copernicus.eu/datasets/sis-agrometeorological-indicators) | [10.24381/cds.6c68c9bb](https://doi.org/10.24381/cds.6c68c9bb) |
| `era5land_*` | ERA5-Land (agregados diarios de Earth Engine) | Copernicus C3S / ECMWF | licencia del dataset en el CDS | [10.24381/cds.e2161bac](https://doi.org/10.24381/cds.e2161bac); [Muñoz-Sabater et al. 2021](https://doi.org/10.5194/essd-13-4349-2021) |
| `goes_*` | GOES-16/19 ABI L2 Cloud and Moisture Imagery (MCMIP) | NOAA | abierto, sin restricciones | [catálogo de Earth Engine](https://developers.google.com/earth-engine/datasets/catalog/NOAA_GOES_19_MCMIPF); [Schmit et al. 2017](https://doi.org/10.1175/BAMS-D-15-00230.1) |
| `modis_lst_*` | MOD11A1 / MYD11A1 v061 | NASA LP DAAC | abierto, sin restricciones | [10.5067/MODIS/MOD11A1.061](https://doi.org/10.5067/MODIS/MOD11A1.061), [10.5067/MODIS/MYD11A1.061](https://doi.org/10.5067/MODIS/MYD11A1.061) |
| `modis_cloud_frac`, `gee_*` (MODIS) | MOD09GA / MYD09GA v061 | NASA LP DAAC | abierto, sin restricciones | [10.5067/MODIS/MOD09GA.061](https://doi.org/10.5067/MODIS/MOD09GA.061), [10.5067/MODIS/MYD09GA.061](https://doi.org/10.5067/MODIS/MYD09GA.061) |
| `modis_aod_055`, `modis_column_wv` | MCD19A2 v061 (MAIAC) | NASA LP DAAC | abierto, sin restricciones | [10.5067/MODIS/MCD19A2.061](https://doi.org/10.5067/MODIS/MCD19A2.061) |
| `gee_s2_*`, `urban_s2_*` | Sentinel-2 L2A harmonized + Cloud Score+ | ESA Copernicus; Google | [términos de datos Sentinel](https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice) | [catálogo de Earth Engine](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED) |
| `sentinel1_*` | Sentinel-1 GRD | ESA Copernicus | términos de datos Sentinel | [catálogo de Earth Engine](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S1_GRD) |
| `landsat_lst`, `urban_landsat_*` | Landsat 8-9 OLI/TIRS Collection 2 Level-2 | USGS | dominio público (cortesía del USGS) | [10.5066/P9OGBGM6](https://doi.org/10.5066/P9OGBGM6) |
| `smap_*` | SMAP L4 SPL4SMGP v008 | NASA NSIDC DAAC | abierto, sin restricciones | [catálogo de Earth Engine](https://developers.google.com/earth-engine/datasets/catalog/NASA_SMAP_SPL4SMGP_008); [Reichle et al. 2019](https://doi.org/10.1029/2019MS001729) |
| `viirs_*` | VIIRS Black Marble VNP46A2 v002 | NASA LAADS DAAC | abierto, sin restricciones | [10.5067/VIIRS/VNP46A2.002](https://doi.org/10.5067/VIIRS/VNP46A2.002) |
| `urban_viirs_ntl_median` | VIIRS DNB mensual (VCMSLCFG) | Earth Observation Group, Colorado School of Mines | CC BY 4.0 | [Elvidge et al. 2017](https://doi.org/10.1080/01431161.2017.1342050) |
| `static_*` (terreno) | Copernicus DEM GLO-90 | ESA / DLR / Airbus | licencia Copernicus DEM (uso libre con atribución) | [Copernicus DEM](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM) |
| `urban_worldcover_*` | ESA WorldCover 10 m 2021 v200 | ESA | CC BY 4.0 | [10.5281/zenodo.7254221](https://doi.org/10.5281/zenodo.7254221) |
| `urban_dw_*` | Dynamic World V1 | Google / World Resources Institute | CC BY 4.0 | [Brown et al. 2022](https://doi.org/10.1038/s41597-022-01307-4) |
| `urban_ghsl_*` | GHSL GHS-BUILT-S / GHS-BUILT-H (P2023A) | Comisión Europea, JRC | CC BY 4.0 | [GHSL Data Package 2023](https://ghsl.jrc.ec.europa.eu/download.php) |
| `urban_buildings_*` | Open Buildings 2.5D Temporal | Google Research | CC BY 4.0 y ODbL 1.0 | [Open Buildings](https://sites.research.google/gr/open-buildings/) |
| `urban_osm_*` | OpenStreetMap (Overpass API) | OpenStreetMap contributors | ODbL 1.0 | © OpenStreetMap contributors |
| (opcional) `chirps_*` | CHIRPS v3 | Climate Hazards Center, UCSB | ver [términos del CHC](https://www.chc.ucsb.edu/data/chirps3) | [Funk et al. 2015](https://doi.org/10.1038/sdata.2015.66) |
| AOI | Barrios de Cali (`idesc:mc_barrios`) | IDESC, Alcaldía de Santiago de Cali | ver condiciones de IDESC | [`data/auxiliary/shapefiles/README.md`](data/auxiliary/shapefiles/README.md) |

Las condiciones resumidas aquí son orientativas: la licencia vigente es la de la página de cada proveedor.
