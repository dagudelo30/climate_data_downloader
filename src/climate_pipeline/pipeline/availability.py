"""Operational availability: "if I query today, what is the most recent data I get?".

For every source the latest **complete** day with data over the AOI is measured against
the live provider (``method="probe"``), or taken from the configured latency when the
provider cannot be queried cheaply (``method="config"``: AgERA5 through the CDS queue,
MSWEP behind Google Drive credentials). From it follow the lag in days and the last
complete epidemiological week (Sunday-Saturday) that the source can fill.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from climate_pipeline.config import PipelineConfig, Settings
from climate_pipeline.domain.aoi import AreaOfInterest
from climate_pipeline.domain.dates import EpiWeek
from climate_pipeline.domain.variables import VariableCatalog
from climate_pipeline.extractors.gee import GEE_PRODUCTS, initialize_earth_engine
from climate_pipeline.extractors.gee_datasets import GEE_SOURCES, Cadence, GeeDataset

logger = logging.getLogger(__name__)

CHC_BASE = "https://data.chc.ucsb.edu/products"

#: Native (provider) pixel size: metres of the finest variable, and a readable label.
#: Degree sizes are converted at ~3.4° N (Cali): 0.1° ≈ 11.1 km. No source is coarser than 0.1°.
NATIVE_RESOLUTION: dict[str, tuple[float, str]] = {
    "chirps": (5_550, "5.5 km (0.05°)"),
    "imerg": (11_100, "11 km (0.1°)"),
    "gsmap": (11_100, "11 km (0.1°)"),
    "mswep": (11_100, "11 km (0.1°)"),
    "agera5": (11_100, "11 km (0.1°)"),
    "era5land": (11_100, "11 km (0.1°)"),
    "modis": (1_000, "1 km (LST, nubosidad, vapor de agua y aerosoles MAIAC)"),
    "landsat": (30, "30 m (térmico 100 m, entregado a 30 m)"),
    "gee": (100, "100 m Sentinel-2 (desde 20 m); 500 m MODIS"),
    "smap": (9_000, "9 km"),
    "viirs": (460, "~460 m luces nocturnas; 1 km LST"),
    "goes": (2_000, "2 km (ABI IR y vapor de agua; visible 0.5-1 km)"),
    "sentinel1": (10, "10 m (radar C, entregado a 30 m)"),
}


_GEE_CATALOG = "https://developers.google.com/earth-engine/datasets/catalog/"

#: Official documentation of every source: ``source -> [(label, url)]``.
SOURCE_DOCS: dict[str, list[tuple[str, str]]] = {
    "imerg": [
        ("NASA GPM IMERG", "https://gpm.nasa.gov/data/imerg"),
        ("GEE IMERG V07", _GEE_CATALOG + "NASA_GPM_L3_IMERG_V07"),
    ],
    "agera5": [
        (
            "Copernicus CDS AgERA5",
            "https://cds.climate.copernicus.eu/datasets/sis-agrometeorological-indicators",
        )
    ],
    "era5land": [("GEE ERA5-Land daily", _GEE_CATALOG + "ECMWF_ERA5_LAND_DAILY_AGGR")],
    "goes": [
        ("GEE GOES-19 MCMIP", _GEE_CATALOG + "NOAA_GOES_19_MCMIPF"),
        ("GEE GOES-16 MCMIP", _GEE_CATALOG + "NOAA_GOES_16_MCMIPF"),
    ],
    "modis": [
        ("MOD11A1", _GEE_CATALOG + "MODIS_061_MOD11A1"),
        ("MYD11A1", _GEE_CATALOG + "MODIS_061_MYD11A1"),
        ("MOD09GA", _GEE_CATALOG + "MODIS_061_MOD09GA"),
        ("MCD19A2 MAIAC", _GEE_CATALOG + "MODIS_061_MCD19A2_GRANULES"),
    ],
    "gee": [
        ("MOD09GA", _GEE_CATALOG + "MODIS_061_MOD09GA"),
        ("MYD09GA", _GEE_CATALOG + "MODIS_061_MYD09GA"),
        ("Sentinel-2 SR", _GEE_CATALOG + "COPERNICUS_S2_SR_HARMONIZED"),
        ("Cloud Score+", _GEE_CATALOG + "GOOGLE_CLOUD_SCORE_PLUS_V1_S2_HARMONIZED"),
    ],
    "landsat": [
        ("Landsat 8 C2 L2", _GEE_CATALOG + "LANDSAT_LC08_C02_T1_L2"),
        ("Landsat 9 C2 L2", _GEE_CATALOG + "LANDSAT_LC09_C02_T1_L2"),
    ],
    "sentinel1": [("Sentinel-1 GRD", _GEE_CATALOG + "COPERNICUS_S1_GRD")],
    "smap": [("SMAP L4 SPL4SMGP", _GEE_CATALOG + "NASA_SMAP_SPL4SMGP_008")],
    "viirs": [("VIIRS VNP46A2", _GEE_CATALOG + "NASA_VIIRS_002_VNP46A2")],
    "chirps": [("CHC CHIRPS v3", "https://www.chc.ucsb.edu/data/chirps3")],
    "gsmap": [("GEE GSMaP v8", _GEE_CATALOG + "JAXA_GPM_L3_GSMaP_v8_operational")],
    "mswep": [("GloH2O MSWEP", "https://www.gloh2o.org/mswep/")],
}

#: Documentation of the static covariate products (keyed by the product name used in the docs).
STATIC_DOCS: dict[str, list[tuple[str, str]]] = {
    "Copernicus DEM GLO-90": [
        ("Copernicus DEM (AWS Open Data)", "https://registry.opendata.aws/copernicus-dem/")
    ],
    "Sentinel-2": [("Sentinel-2 SR", _GEE_CATALOG + "COPERNICUS_S2_SR_HARMONIZED")],
    "Landsat 8/9": [("Landsat 9 C2 L2", _GEE_CATALOG + "LANDSAT_LC09_C02_T1_L2")],
    "Dynamic World": [("Dynamic World", _GEE_CATALOG + "GOOGLE_DYNAMICWORLD_V1")],
    "ESA WorldCover 2021": [("ESA WorldCover", _GEE_CATALOG + "ESA_WorldCover_v200")],
    "JRC GHSL": [
        ("GHS-BUILT-S", _GEE_CATALOG + "JRC_GHSL_P2023A_GHS_BUILT_S"),
        ("GHS-BUILT-H", _GEE_CATALOG + "JRC_GHSL_P2023A_GHS_BUILT_H"),
    ],
    "Google Open Buildings 2.5D": [
        ("Open Buildings 2.5D Temporal", _GEE_CATALOG + "GOOGLE_Research_open-buildings-temporal_v1")
    ],
    "VIIRS DNB mensual": [("VIIRS DNB mensual", _GEE_CATALOG + "NOAA_VIIRS_DNB_MONTHLY_V1_VCMSLCFG")],
    "OpenStreetMap": [("Overpass API", "https://wiki.openstreetmap.org/wiki/Overpass_API")],
}


@dataclass
class SourceAvailability:
    source: str
    product: str
    agency: str
    resolution: str
    cadence: str
    variables: list[str]
    latest_day: str | None
    status: str  # final | prelim | mixed | unknown
    method: str  # probe | config
    lag_days: int | None = None
    last_complete_epiweek: str | None = None
    last_complete_epiweek_end: str | None = None
    final_latest_day: str | None = None
    notes: str = ""
    native_resolution_m: float | None = None
    native_resolution: str = ""
    details: dict[str, str] = field(default_factory=dict)
    error: str | None = None


def last_complete_epiweek(latest: date) -> EpiWeek:
    """Last Sunday-Saturday week whose seven days are all ≤ ``latest``."""
    saturday = latest - timedelta(days=(latest.weekday() - 5) % 7)
    return EpiWeek.from_date(saturday)


def _finalize(item: SourceAvailability, today: date) -> SourceAvailability:
    if item.source in NATIVE_RESOLUTION:
        item.native_resolution_m, item.native_resolution = NATIVE_RESOLUTION[item.source]
    if item.latest_day:
        latest = date.fromisoformat(item.latest_day)
        item.lag_days = (today - latest).days
        week = last_complete_epiweek(latest)
        item.last_complete_epiweek = week.label
        item.last_complete_epiweek_end = week.end.isoformat()
    return item


# --- Earth Engine -------------------------------------------------------------------------
def _gee_latest(
    ee: Any, dataset: GeeDataset, aoi: AreaOfInterest, today: date
) -> tuple[date | None, dict[str, Any]]:
    min_x, min_y, max_x, max_y = aoi.bounds
    area = ee.Geometry.Rectangle([min_x, min_y, max_x, max_y], "EPSG:4326", False)
    start = (today - timedelta(days=60)).isoformat()
    collection = ee.ImageCollection(dataset.collections[0])
    for extra in dataset.collections[1:]:
        collection = collection.merge(ee.ImageCollection(extra))
    collection = collection.filterDate(start, (today + timedelta(days=1)).isoformat()).filterBounds(area)
    if dataset.empty_is_missing and dataset.grid_band:
        band = dataset.grid_band

        def with_count(image: Any) -> Any:
            count = image.select(band).reduceRegion(ee.Reducer.count(), area, 500, maxPixels=1e8).get(band)
            return image.set("valid_pixels", count)

        collection = collection.map(with_count).filter(ee.Filter.gt("valid_pixels", 0))
    info: dict[str, Any] = {"times": collection.aggregate_array("system:time_start")}
    if dataset.status_property:
        info["statuses"] = collection.aggregate_array(dataset.status_property[0])
    payload = ee.Dictionary(info).getInfo()
    times = [datetime.fromtimestamp(value / 1000, tz=UTC).replace(tzinfo=None) for value in payload["times"]]
    extra: dict[str, Any] = {}
    if not times:
        return None, extra
    if dataset.cadence is Cadence.SUBDAILY:
        per_day = Counter(moment.date() for moment in times)
        complete = [day for day, count in per_day.items() if count >= dataset.min_images]
        latest = max(complete) if complete else None
    else:
        latest = max(times).date()
    if dataset.status_property and latest is not None:
        statuses = [
            status
            for moment, status in zip(times, payload.get("statuses", []), strict=False)
            if moment.date() == latest
        ]
        final_values = dataset.status_property[1]
        extra["status"] = "final" if statuses and set(statuses) <= final_values else "prelim"
    return latest, extra


def probe_gee_sources(
    settings: Settings,
    aoi: AreaOfInterest,
    today: date,
    sources: list[str],
    ee: Any | None = None,
    selected: set[str] | None = None,
) -> list[SourceAvailability]:
    """``selected``: Gold columns in use; datasets without any of them are not probed."""
    if ee is None:
        key = settings.gee_service_account_key
        ee = initialize_earth_engine(settings.gee_project, str(key) if key else None)
    results = []
    for source in sources:
        datasets = GEE_SOURCES.get(source)
        if datasets is None:
            continue
        if selected is not None:
            datasets = tuple(
                dataset
                for dataset in datasets
                if any(f"{source}_{variable.name}" in selected for variable in dataset.variables)
            )
            if not datasets:
                continue
        details, latest_days, statuses, errors = {}, [], [], []
        for dataset in datasets:
            try:
                latest, extra = _gee_latest(ee, dataset, aoi, today)
            except Exception as exc:
                errors.append(f"{dataset.key}: {exc!r}")
                continue
            details[dataset.key] = latest.isoformat() if latest else "none in 60 days"
            if latest:
                latest_days.append(latest)
            statuses.append(extra.get("status", "final"))
        item = SourceAvailability(
            source=source,
            product=", ".join(collection for dataset in datasets for collection in dataset.collections),
            agency=datasets[0].agency,
            resolution="; ".join(sorted({dataset.resolution_label for dataset in datasets})),
            cadence="daily",
            variables=[
                f"{source}_{variable.name}"
                for dataset in datasets
                for variable in dataset.variables
                if selected is None or f"{source}_{variable.name}" in selected
            ],
            # The source is as recent as its *least* recent dataset (all its columns filled).
            latest_day=min(latest_days).isoformat() if latest_days else None,
            status="prelim" if "prelim" in statuses else "final",
            method="probe",
            details=details,
            error="; ".join(errors) or None,
        )
        results.append(_finalize(item, today))
    return results


def probe_gee_indices(
    settings: Settings, aoi: AreaOfInterest, today: date, products: list[str], ee: Any | None = None
) -> SourceAvailability:
    """The original ``gee`` source (MODIS / Sentinel-2 NDVI-NDWI products)."""
    if ee is None:
        key = settings.gee_service_account_key
        ee = initialize_earth_engine(settings.gee_project, str(key) if key else None)
    details, latest_days = {}, []
    for name in products:
        product = GEE_PRODUCTS[name]
        dataset = GeeDataset(key=name, collections=product.collections, cadence=Cadence.DAILY, variables=())
        latest, _ = _gee_latest(ee, dataset, aoi, today)
        details[name] = latest.isoformat() if latest else "none in 60 days"
        if latest:
            latest_days.append(latest)
    item = SourceAvailability(
        source="gee",
        product=", ".join(collection for name in products for collection in GEE_PRODUCTS[name].collections),
        agency="NASA LP DAAC / ESA Copernicus",
        resolution="500 m (MODIS), 100 m (Sentinel-2)",
        cadence="daily (cloud-masked)",
        variables=["gee_ndvi", "gee_ndwi", "gee_s2_ndvi", "gee_s2_ndwi"],
        latest_day=min(latest_days).isoformat() if latest_days else None,
        status="final",
        method="probe",
        details=details,
        notes="Optical: a published day can still be fully cloudy over the AOI.",
    )
    return _finalize(item, today)


# --- CHC CHIRPS ---------------------------------------------------------------------------
_CHIRPS_FILE = re.compile(r"chirps-v3\.0\.(?:prelim|rnl|sat)\.(\d{4})\.(\d{2})\.(\d{2})\.tif")


def _chc_latest(http: httpx.Client, url: str) -> date | None:
    response = http.get(url)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    days = [date(int(y), int(m), int(d)) for y, m, d in _CHIRPS_FILE.findall(response.text)]
    return max(days) if days else None


def probe_chirps(today: date, http: httpx.Client) -> SourceAvailability:
    latest_prelim = latest_final = None
    for year in (today.year, today.year - 1):
        latest_prelim = latest_prelim or _chc_latest(http, f"{CHC_BASE}/CHIRPS/v3.0/daily/prelim/sat/{year}/")
        latest_final = latest_final or _chc_latest(http, f"{CHC_BASE}/CHIRPS/v3.0/daily/final/rnl/{year}/")
    latest = (
        max(day for day in (latest_prelim, latest_final) if day) if (latest_prelim or latest_final) else None
    )
    item = SourceAvailability(
        source="chirps",
        product="CHIRPS v3.0 daily (prelim/sat → final/rnl)",
        agency="UCSB Climate Hazards Center",
        resolution="0.05° (~5.5 km)",
        cadence="daily",
        variables=["chirps_precip"],
        latest_day=latest.isoformat() if latest else None,
        final_latest_day=latest_final.isoformat() if latest_final else None,
        status="prelim" if latest_prelim and latest_final and latest_prelim > latest_final else "final",
        method="probe",
        details={"prelim": str(latest_prelim), "final": str(latest_final)},
        notes="Preliminary fields are replaced by the final product around the 3rd week of the next month.",
    )
    return _finalize(item, today)


# --- configured latency (not cheaply probeable) ------------------------------------------
_CONFIG_ONLY = {
    "agera5": (
        "AgERA5 v2.0 (sis-agrometeorological-indicators)",
        "Copernicus C3S / ECMWF",
        "0.1° (~11 km)",
        "Published daily by the CDS ~7-8 days after the observation; every CDS request is queued, "
        "so it is not probed live.",
    ),
    "mswep": (
        "MSWEP v2.8 daily (NRT → Past)",
        "GloH2O",
        "0.1° (~11 km)",
        "Needs the Google Drive access granted by GloH2O (MSWEP_OAUTH_CLIENT); NRT within ~2 days.",
    ),
}


def configured_availability(
    source: str, config: PipelineConfig, catalog: VariableCatalog, today: date
) -> SourceAvailability:
    product, agency, resolution, notes = _CONFIG_ONLY[source]
    latency = config.source(source).latency
    latest = today - timedelta(days=latency.earliest_lag_days)
    item = SourceAvailability(
        source=source,
        product=product,
        agency=agency,
        resolution=resolution,
        cadence="daily",
        variables=[spec.gold_column for spec in catalog.for_source(source)],
        latest_day=latest.isoformat(),
        final_latest_day=(today - timedelta(days=latency.final_lag_days)).isoformat(),
        status="prelim" if latency.prelim_lag_days is not None else "final",
        method="config",
        notes=notes,
    )
    return _finalize(item, today)


def availability_report(
    settings: Settings,
    config: PipelineConfig,
    catalog: VariableCatalog,
    aoi: AreaOfInterest,
    sources: list[str],
    today: date | None = None,
) -> dict[str, Any]:
    today = today or datetime.now(UTC).date()
    http = httpx.Client(timeout=120.0, follow_redirects=True, headers={"User-Agent": "climate-pipeline/0.1"})
    items: list[SourceAvailability] = []

    def attempt(source: str, function: Any, *args: Any) -> None:
        try:
            result = function(*args)
            items.extend(result if isinstance(result, list) else [result])
        except Exception as exc:
            logger.exception("availability probe failed for %s", source)
            items.append(
                SourceAvailability(source, "", "", "", "daily", [], None, "unknown", "probe", error=repr(exc))
            )

    gee_sources = [source for source in sources if source in GEE_SOURCES]
    if gee_sources:
        selected = {spec.gold_column for spec in catalog.all() if spec.default}
        attempt("+".join(gee_sources), probe_gee_sources, settings, aoi, today, gee_sources, None, selected)
    if "gee" in sources:
        products = config.source("gee").options.get("products", [])
        attempt("gee", probe_gee_indices, settings, aoi, today, products)
    if "chirps" in sources:
        attempt("chirps", probe_chirps, today, http)
    for source in sources:
        if source in _CONFIG_ONLY:
            attempt(source, configured_availability, source, config, catalog, today)

    order = {source: index for index, source in enumerate(sources)}
    items.sort(key=lambda item: order.get(item.source, len(order)))
    dated = [date.fromisoformat(item.latest_day) for item in items if item.latest_day]
    common = min(dated) if dated else None
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "aoi": aoi.name,
        "aoi_bounds": [round(value, 4) for value in aoi.bounds],
        "all_sources_complete_through": common.isoformat() if common else None,
        "all_sources_last_complete_epiweek": last_complete_epiweek(common).label if common else None,
        "sources": [asdict(item) for item in items],
    }


# --- per-variable latency ------------------------------------------------------------------
def column_product(column: str) -> str | None:
    """Key under ``details`` of the availability report that dates ``column`` (dataset or product)."""
    source, _, name = column.partition("_")
    for dataset in GEE_SOURCES.get(source, ()):
        if any(variable.name == name for variable in dataset.variables):
            return dataset.key
    if source == "gee":
        return "sentinel2_indices" if name.startswith("s2_") else "modis_terra_aqua_indices"
    return None


def variable_latencies(
    report: dict[str, Any], columns: list[str], last_valid: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Publication and effective latency of every Gold column.

    * publication: last day the provider has published (per dataset when a source has several);
    * effective: last day with a valid value over the AOI in Gold (``last_valid``), i.e. what
      the model really gets once clouds and empty placeholders are accounted for.
    """
    today = date.fromisoformat(report["today"])
    by_source = {item["source"]: item for item in report["sources"]}
    rows = []
    for column in columns:
        source = column.split("_", 1)[0]
        item = by_source.get(source, {})
        product = column_product(column)
        published = (item.get("details") or {}).get(product) if product else None
        published = published if published and published[0].isdigit() else item.get("latest_day")
        valid = (last_valid or {}).get(column)
        rows.append(
            {
                "column": column,
                "source": source,
                "product": product or source,
                "method": item.get("method", ""),
                "published_day": published,
                "publication_lag_days": (today - date.fromisoformat(published)).days if published else None,
                "last_valid_day": valid,
                "effective_lag_days": (today - date.fromisoformat(valid)).days if valid else None,
            }
        )
    return rows
