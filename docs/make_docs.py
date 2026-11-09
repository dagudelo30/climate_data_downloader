"""Builds the HTML documentation from the live variable catalog.

    uv run python docs/make_docs.py

Writes ``docs/diccionario_variables.html`` (every Gold column: what it explains, source,
native resolution, latency, sampling, weekly rule, which target it helps to model),
``docs/flujo_proceso.html`` (the process, end to end) and ``docs/modelo_relacional.html``
(Gold tables, keys and how the nested grids join). The dictionary reads the catalog and the
redundancy rule from the code, so it never drifts from what the pipeline produces.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from climate_pipeline.config import load_pipeline_config
from climate_pipeline.pipeline.availability import SOURCE_DOCS, STATIC_DOCS, variable_latencies
from climate_pipeline.pipeline.orchestrator import build_variable_catalog

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "examples/cali_operational/pipeline.yaml"
SOURCES = ["imerg", "agera5", "era5land", "goes", "modis", "gee", "landsat", "sentinel1", "smap", "viirs"]

# Objetivos: P precipitación, T temperatura (media/máx/mín), H humedad relativa, V viento, R radiación solar.
SOURCE_INFO = {
    "imerg": ("GPM IMERG V07", "NASA", "0.1° (~11 km), 30 min", "diario"),
    "agera5": ("AgERA5 v2.0", "Copernicus C3S / ECMWF", "0.1° (~11 km)", "diario"),
    "era5land": ("ERA5-Land", "ECMWF", "0.1° (~11 km)", "diario"),
    "goes": ("GOES-East ABI (GOES-16 → GOES-19)", "NOAA", "2 km, 10 min", "diario (144 escaneos)"),
    "modis": ("MODIS Terra/Aqua (MOD11A1, MYD11A1, MOD09GA, MCD19A2)", "NASA", "1 km", "diario (2 pasadas)"),
    "gee": ("MODIS MOD09GA/MYD09GA y Sentinel-2 L2A + Cloud Score+", "NASA / ESA", "", "diario / ~5 días"),
    "landsat": ("Landsat 8/9 Collection 2 L2", "USGS / NASA", "30 m (térmico 100 m)", "~8 días"),
    "sentinel1": ("Sentinel-1 GRD IW", "ESA Copernicus", "10 m (entregado a 30 m)", "~6 días"),
    "smap": ("SMAP L4 (SPL4SMGP)", "NASA", "9 km, 3 h", "diario"),
    "viirs": ("VIIRS Black Marble VNP46A2", "NASA", "15″ (~460 m)", "diario"),
}

PURPOSE: dict[str, tuple[str, str]] = {
    "imerg_precip": (
        "Lluvia diaria estimada por satélite (microondas + IR). Precipitación de referencia del pipeline.",
        "P",
    ),
    "agera5_temp_mean": ("Temperatura media del aire a 2 m en el día solar local.", "T"),
    "agera5_temp_max": ("Temperatura máxima del día; base para Tmáx y estrés térmico.", "T"),
    "agera5_temp_min": ("Temperatura mínima del día; base para Tmín.", "T"),
    "agera5_temp_day_max": ("Máxima de las horas de luz (06-18 h).", "T"),
    "agera5_temp_day_mean": (
        "Media de las horas de luz: calentamiento diurno que dispara la convección.",
        "T P",
    ),
    "agera5_temp_night_mean": ("Media nocturna: enfriamiento radiativo e isla de calor nocturna.", "T"),
    "agera5_temp_night_min": ("Mínima nocturna.", "T"),
    "agera5_dewpoint_mean": ("Punto de rocío: contenido absoluto de humedad del aire.", "H T"),
    "agera5_vapour_pressure": ("Presión de vapor: humedad del aire en hPa.", "H P"),
    "agera5_vpd_tmax": (
        "Déficit de presión de vapor a la hora de Tmáx: sequedad del aire y demanda evaporativa.",
        "H T",
    ),
    "agera5_rh_06h": ("HR a las 06 h (cerca de Tmín, máxima del día).", "H"),
    "agera5_rh_09h": ("HR a las 09 h.", "H"),
    "agera5_rh_12h": ("HR al mediodía.", "H"),
    "agera5_rh_15h": ("HR a las 15 h (cerca de Tmáx, mínima del día).", "H"),
    "agera5_rh_18h": ("HR a las 18 h.", "H"),
    "agera5_rh_max": ("HR máxima diaria, calculada de T y Td horarias.", "H"),
    "agera5_rh_min": ("HR mínima diaria, calculada de T y Td horarias.", "H"),
    "agera5_wind_speed": ("Velocidad media del viento a 10 m.", "V"),
    "agera5_solar_radiation": ("Radiación solar global diaria en superficie.", "R T"),
    "agera5_et0": ("Evapotranspiración de referencia FAO-56: demanda atmosférica de agua.", "H R"),
    "agera5_precip_hours_frac": (
        "Fracción de horas del día con lluvia (persistencia frente a aguacero).",
        "P",
    ),
    "era5land_skin_temp": (
        "Temperatura de piel (superficie) con cielo de cualquier tipo, sin huecos por nubes.",
        "T",
    ),
    "era5land_surface_pressure": ("Presión en superficie (depende de la altura; sinóptica).", "V P"),
    "era5land_evaporation": ("Evaporación real del suelo y la vegetación.", "H P"),
    "era5land_u_wind_10m": ("Componente este-oeste del viento: dirección del flujo.", "V P"),
    "era5land_v_wind_10m": ("Componente norte-sur del viento: dirección del flujo.", "V P"),
    "era5land_soil_temp_l1": ("Temperatura del suelo 0-7 cm.", "T"),
    "era5land_sensible_heat_flux": ("Calor sensible: energía que calienta el aire.", "T V"),
    "era5land_net_thermal_radiation": (
        "Balance de onda larga: pérdida nocturna de calor (cielo despejado = más pérdida).",
        "T R",
    ),
    "era5land_thermal_radiation_down": ("Onda larga descendente: señal de nubes y vapor.", "R H"),
    "era5land_runoff": ("Escorrentía: agua que no se infiltra (encharcamientos).", "P"),
    "era5land_lai_high": ("Índice de área foliar de vegetación alta.", "T H"),
    "era5land_lai_low": ("Índice de área foliar de vegetación baja.", "T H"),
    "era5land_albedo": ("Albedo de superficie: fracción de radiación reflejada.", "R T"),
    "goes_bt_c08": ("Brillo a 6.2 µm: vapor de agua en niveles altos.", "P H"),
    "goes_bt_c09": ("Brillo a 6.9 µm: vapor en niveles medios.", "P H"),
    "goes_bt_c10": ("Brillo a 7.3 µm: vapor en niveles bajos.", "P H"),
    "goes_bt_c11": ("Brillo a 8.4 µm: fase del tope de nube (hielo o agua).", "P"),
    "goes_bt_c13": ("Brillo IR limpio a 10.3 µm: altura y temperatura de topes de nube.", "P R"),
    "goes_bt_c14": ("Brillo IR a 11.2 µm.", "P R"),
    "goes_bt_c15": ("Brillo IR sucio a 12.3 µm (absorción por vapor).", "P H"),
    "goes_bt_c13_min": ("Tope de nube más frío del día: intensidad máxima de la convección.", "P"),
    "goes_btd_c13_c15": ("Ventana partida 10.3−12.3 µm: humedad en capas bajas y cirros delgados.", "P H"),
    "goes_btd_c08_c13": (
        "Vapor − ventana 6.2−10.3 µm: cerca de 0 o positivo indica convección profunda.",
        "P",
    ),
    "goes_btd_c11_c14": ("8.4−11.2 µm: distingue topes de hielo y de agua.", "P"),
    "goes_cold_cloud_frac_235": (
        "Duración de nube fría (<235 K): el predictor de lluvia de CHIRPS y TAMSAT.",
        "P R",
    ),
    "goes_cold_cloud_frac_220": ("Duración de nube muy fría (<220 K): tormentas profundas.", "P"),
    "goes_refl_c02_day": (
        "Reflectancia visible diurna: albedo de nubes; controla la radiación solar.",
        "R P T",
    ),
    "modis_lst_day_terra": ("Temperatura de superficie a las ~10:30 (cielo despejado).", "T"),
    "modis_lst_night_terra": ("Temperatura de superficie a las ~22:30.", "T"),
    "modis_lst_day_aqua": (
        "Temperatura de superficie a las ~13:30: cerca del pico diario, explica Tmáx.",
        "T P",
    ),
    "modis_lst_night_aqua": ("Temperatura de superficie a las ~01:30: explica Tmín.", "T"),
    "modis_cloud_frac": ("Fracción nubosa de las pasadas de Terra y Aqua.", "P R"),
    "modis_column_wv": ("Agua precipitable de la columna (MAIAC, cielo despejado).", "P H"),
    "modis_aod_055": ("Espesor óptico de aerosoles: atenuación de la radiación solar.", "R"),
    "gee_ndvi": ("NDVI diario de MODIS (500 m): vigor de la vegetación.", "T H P"),
    "gee_ndwi": ("NDWI de Gao: agua en la vegetación.", "H P"),
    "gee_evi": ("EVI: vegetación densa sin saturar.", "T H"),
    "gee_mndwi": ("MNDWI: agua abierta y suelo húmedo.", "P H"),
    "gee_s2_ndvi": ("NDVI de Sentinel-2 (10 m nativo, entregado a 100 m).", "T H"),
    "gee_s2_ndwi": ("NDWI de Sentinel-2 (−NDBI: agua frente a superficie construida).", "H T"),
    "gee_s2_evi": ("EVI de Sentinel-2.", "T H"),
    "gee_s2_mndwi": ("MNDWI de Sentinel-2: cuerpos de agua y encharcamientos.", "P H"),
    "gee_s2_ndre": ("NDRE (borde rojo): estrés de la vegetación.", "H T"),
    "gee_s2_bsi": ("Índice de suelo desnudo.", "T"),
    "landsat_lst": ("Temperatura de superficie a 30 m (~10:15): islas de calor urbanas.", "T"),
    "sentinel1_vv_db": (
        "Radar VV: humedad del suelo, agua abierta y construcciones. Ve a través de nubes.",
        "P H",
    ),
    "sentinel1_vh_db": ("Radar VH: volumen de vegetación.", "H"),
    "sentinel1_vh_vv_db": ("Razón VH−VV: vegetación frente a estructura urbana.", "T"),
    "smap_sm_surface": ("Humedad del suelo 0-5 cm: memoria de la lluvia reciente.", "P H T"),
    "smap_sm_rootzone": ("Humedad del suelo 0-100 cm.", "P H"),
    "viirs_ntl": ("Luces nocturnas diarias: intensidad urbana.", "T"),
}

STATIC = [
    ("static_elevation_m", "Elevación media de la celda", "Copernicus DEM GLO-90", "90 m", "fija", "T P H V"),
    ("static_slope_deg", "Pendiente media", "Copernicus DEM GLO-90", "90 m", "fija", "P V R"),
    (
        "static_northness",
        "Orientación norte-sur (cos aspecto)",
        "Copernicus DEM GLO-90",
        "90 m",
        "fija",
        "R T",
    ),
    (
        "static_eastness",
        "Orientación este-oeste (sen aspecto)",
        "Copernicus DEM GLO-90",
        "90 m",
        "fija",
        "R T",
    ),
    (
        "static_tpi_500m",
        "Posición topográfica a 500 m (cresta +, valle −)",
        "Copernicus DEM GLO-90",
        "90 m",
        "fija",
        "T V",
    ),
    ("static_tpi_2km", "Posición topográfica a 2 km", "Copernicus DEM GLO-90", "90 m", "fija", "T V P"),
    ("static_tri_m", "Rugosidad del terreno", "Copernicus DEM GLO-90", "90 m", "fija", "V"),
    ("urban_s2_ndbi", "Índice de construcción (mediana anual)", "Sentinel-2", "10-20 m", "anual", "T"),
    ("urban_s2_ndvi", "NDVI mediana anual", "Sentinel-2", "10 m", "anual", "T H"),
    ("urban_s2_mndwi", "Agua abierta (mediana anual)", "Sentinel-2", "10-20 m", "anual", "H"),
    ("urban_landsat_lst_median", "LST mediana anual: islas de calor", "Landsat 8/9", "30 m", "anual", "T"),
    ("urban_dw_built", "Probabilidad media de construido", "Dynamic World", "10 m", "anual", "T V"),
    ("urban_dw_trees", "Probabilidad media de árboles", "Dynamic World", "10 m", "anual", "T H"),
    ("urban_dw_water", "Probabilidad media de agua", "Dynamic World", "10 m", "anual", "H"),
    ("urban_worldcover_built_frac", "Fracción construida", "ESA WorldCover 2021", "10 m", "2021", "T V"),
    ("urban_worldcover_tree_frac", "Fracción de árboles", "ESA WorldCover 2021", "10 m", "2021", "T H"),
    ("urban_ghsl_built_frac", "Superficie construida", "JRC GHSL", "100 m", "2020", "T V"),
    ("urban_ghsl_built_height_m", "Altura media de edificios", "JRC GHSL", "100 m", "2018", "V T"),
    ("urban_buildings_frac", "Huella de edificios", "Google Open Buildings 2.5D", "~4 m", "≤2023", "T V"),
    ("urban_buildings_height_m", "Altura de edificios", "Google Open Buildings 2.5D", "~4 m", "≤2023", "V T"),
    ("urban_viirs_ntl_median", "Luces nocturnas mediana anual", "VIIRS DNB mensual", "~460 m", "anual", "T"),
    (
        "urban_osm_road_density_km_km2",
        "Densidad de vías (km/km²)",
        "OpenStreetMap",
        "vectorial",
        "actual",
        "T V",
    ),
    (
        "urban_osm_major_road_density_km_km2",
        "Densidad de vías principales",
        "OpenStreetMap",
        "vectorial",
        "actual",
        "T V",
    ),
    (
        "urban_osm_intersection_density_km2",
        "Intersecciones por km²",
        "OpenStreetMap",
        "vectorial",
        "actual",
        "V",
    ),
]

TARGETS = {
    "P": "Precipitación",
    "T": "Temperatura",
    "H": "Humedad relativa",
    "V": "Viento",
    "R": "Radiación solar",
}

CSS = """
:root { --bg:#f5f6f3; --surface:#fff; --fg:#1c2320; --muted:#5b6761; --rule:#d8ddd6; --band:#edf0ea;
  --accent:#1f6f5c; --accent-soft:#d5eae3; --warn:#9a5b12; --warn-soft:#f4e5cf;
  --font-display:"Archivo Narrow","Arial Narrow",Arial,sans-serif; --font-body:"Archivo","Helvetica Neue",Arial,sans-serif;
  --font-data:"IBM Plex Mono",ui-monospace,Menlo,monospace; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#131816; --surface:#1a201d; --fg:#e4eae6;
  --muted:#9aa69f; --rule:#2d3732; --band:#1b221f; --accent:#5fbfa3; --accent-soft:#1f3a32; --warn:#e2a84b;
  --warn-soft:#3b2f1a; color-scheme: dark; } }
:root[data-theme="dark"] { --bg:#131816; --surface:#1a201d; --fg:#e4eae6; --muted:#9aa69f; --rule:#2d3732; --band:#1b221f;
  --accent:#5fbfa3; --accent-soft:#1f3a32; --warn:#e2a84b; --warn-soft:#3b2f1a; color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 var(--font-body); }
main { max-width:1180px; margin:0 auto; padding-block:36px 56px; padding-left:max(16px, env(safe-area-inset-left, 0px));
  padding-right:max(16px, env(safe-area-inset-right, 0px)); display:grid; gap:28px; }
nav { display:flex; flex-wrap:wrap; gap:6px 16px; font:500 13px var(--font-data); }
nav a { color:var(--accent); }
.eyebrow { font:600 12px/1 var(--font-data); letter-spacing:.08em; text-transform:uppercase; color:var(--muted); }
h1 { font:600 clamp(28px,4.2vw,42px)/1.08 var(--font-display); margin:0; text-wrap:balance; }
h2 { font:600 22px/1.2 var(--font-display); margin:0; }
h3 { font:600 17px/1.25 var(--font-display); margin:0; }
p, li { max-width:76ch; }
p { margin:0; }
section { display:grid; gap:12px; min-width:0; }
.lede { color:var(--muted); }
.scroll { overflow-x:auto; background:var(--surface); border:1px solid var(--rule); }
table { border-collapse:collapse; width:100%; font-size:13.5px; }
th, td { text-align:left; vertical-align:top; padding:9px 11px; border-bottom:1px solid var(--rule); }
thead th { font:500 11.5px/1.3 var(--font-data); text-transform:uppercase; letter-spacing:.06em; color:var(--muted);
  position:sticky; top:0; background:var(--surface); }
tr.group th { font:600 15px var(--font-display); background:var(--band); padding-top:14px; }
code, .mono { font-family:var(--font-data); font-size:12.5px; }
.muted { color:var(--muted); }
.tag { display:inline-block; padding:1px 7px; margin:1px 2px 1px 0; border-radius:999px; font:600 11.5px var(--font-data);
  background:var(--accent-soft); color:var(--accent); }
.tag.off { background:var(--warn-soft); color:var(--warn); }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:1px; background:var(--rule);
  border:1px solid var(--rule); }
.cards > div { background:var(--surface); padding:16px 18px; display:grid; gap:4px; align-content:start; }
.cards .k { font:500 12px var(--font-data); text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
.cards .v { font:600 28px/1.1 var(--font-display); font-variant-numeric:tabular-nums; }
.diagram { background:var(--surface); border:1px solid var(--rule); padding:16px; overflow-x:auto; }
ol, ul { margin:0; padding-left:1.2em; display:grid; gap:6px; }
footer { color:var(--muted); font-size:12.5px; border-top:1px solid var(--rule); padding-top:12px; }
"""

NAV = (
    '<nav><a href="resolucion_fuentes.html">Fuentes y resolución</a>'
    '<a href="diccionario_variables.html">Diccionario de variables</a>'
    '<a href="predictores_por_variable.html">Papers por variable</a>'
    '<a href="flujo_proceso.html">Flujo del proceso</a><a href="modelo_relacional.html">Modelo relacional</a>'
    '<a href="../outputs/cali_operational/latencia_operativa.html">Latencia operativa</a></nav>'
)
FONTS = (
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600'
    '&family=Archivo+Narrow:wght@600&family=IBM+Plex+Mono:wght@400;500&display=swap">'
)
MERMAID = (
    '<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>'
    "<script>mermaid.initialize({startOnLoad:true, theme: matchMedia('(prefers-color-scheme: dark)').matches ? "
    "'dark' : 'neutral', securityLevel:'strict', flowchart:{useMaxWidth:true}});</script>"
)


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def page(title: str, body: str, extra_head: str = "") -> str:
    return (
        '<!doctype html>\n<html lang="es">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f'<title>{esc(title)}</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n{FONTS}\n'
        f"<style>{CSS}</style>\n{extra_head}\n</head>\n<body>\n<main>\n{NAV}\n{body}\n</main>\n</body>\n</html>\n"
    )


def doc_links(links: list[tuple[str, str]]) -> str:
    return " · ".join(
        f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(label)}</a>' for label, url in links
    )


def static_doc_links(source: str) -> str:
    links = [link for name, items in STATIC_DOCS.items() if name in source for link in items]
    return doc_links(links)


def tags(codes: str) -> str:
    return "".join(f'<span class="tag" title="{esc(TARGETS[c])}">{c}</span>' for c in codes.split())


def resolution_of(column: str, source: str) -> str:
    if column.startswith("gee_s2_"):
        return "10-20 m (entregado a 100 m)"
    if column.startswith("gee_"):
        return "500 m"
    if column in {"goes_refl_c02_day"}:
        return "0.5-1 km (entregado a 2 km)"
    if column == "viirs_ntl":
        return "15″ (~460 m)"
    return SOURCE_INFO[source][2]


def latest_availability() -> dict:
    files = sorted((ROOT / "outputs/cali_operational/reports").glob("availability_*.json"))
    return json.loads(files[-1].read_text()) if files else {"today": "", "sources": []}


def last_valid_days(grid: str = "100m") -> dict[str, str]:
    inventory = ROOT / "outputs/cali_operational/tables/column_inventory.csv"
    if not inventory.exists():
        return {}
    import csv

    with inventory.open() as handle:
        return {
            r["column"]: r["last_date"][:10]
            for r in csv.DictReader(handle)
            if r["grid"] == grid and r["last_date"]
        }


def dictionary_page() -> str:
    config = load_pipeline_config(CONFIG)
    catalog = build_variable_catalog(config)
    report = latest_availability()
    kept = [spec for spec in catalog.all() if spec.default and spec.source in SOURCES]
    latency = (
        {
            row["column"]: row
            for row in variable_latencies(report, [s.gold_column for s in kept], last_valid_days())
        }
        if report["today"]
        else {}
    )
    rows, summary = [], []
    for source in SOURCES:
        product, agency, _, sampling = SOURCE_INFO[source]
        specs = [spec for spec in kept if spec.source == source]
        publication = sorted(
            {
                latency[s.gold_column]["publication_lag_days"]
                for s in specs
                if s.gold_column in latency and latency[s.gold_column]["publication_lag_days"] is not None
            }
        )
        effective = sorted(
            {
                latency[s.gold_column]["effective_lag_days"]
                for s in specs
                if s.gold_column in latency and latency[s.gold_column]["effective_lag_days"] is not None
            }
        )
        configured = config.source(source).latency.earliest_lag_days

        def span(values: list[int]) -> str:
            return (
                "—"
                if not values
                else f"{values[0]} d"
                if values[0] == values[-1]
                else f"{values[0]}-{values[-1]} d"
            )

        summary.append(
            f'<tr><td>{esc(product)}</td><td class="mono">{len(specs)}</td><td>{esc("500 m (MODIS) / 10-20 m (Sentinel-2)" if source == "gee" else resolution_of(specs[0].gold_column, source) if specs else "")}</td>'
            f'<td class="mono">{span(publication) if publication else f"~{configured} d (configurada)"}</td><td class="mono">{span(effective)}</td>'
            f"<td>{esc(agency)}</td><td>{doc_links(SOURCE_DOCS.get(source, []))}</td></tr>"
        )
        rows.append(
            f'<tr class="group"><th colspan="9" scope="colgroup">{esc(product)} '
            f'<span class="muted mono">· {esc(agency)} · {len(specs)} {"variable" if len(specs) == 1 else "variables"}</span>'
            f' <span class="mono" style="font-weight:400">· {doc_links(SOURCE_DOCS.get(source, []))}</span></th></tr>'
        )
        for spec in specs:
            text, targets = PURPOSE.get(spec.gold_column, (spec.description, ""))
            weekly = f"{spec.weekly_aggregation.value} (≥{spec.min_valid_days} d)"
            info = latency.get(spec.gold_column, {})
            pub = info.get("publication_lag_days")
            eff = info.get("effective_lag_days")
            rows.append(
                f"<tr><td><code>{esc(spec.gold_column)}</code></td><td>{esc(text)}</td><td>{tags(targets)}</td>"
                f'<td class="mono">{esc(spec.units)}</td><td>{esc(resolution_of(spec.gold_column, source))}</td>'
                f"<td>{esc(sampling)}</td>"
                f'<td class="mono">{f"{pub} d" if pub is not None else f"~{configured} d"}</td>'
                f'<td class="mono">{f"{eff} d" if eff is not None else "—"}</td>'
                f'<td class="mono">{esc(weekly)}</td></tr>'
            )
    static_rows = "".join(
        f"<tr><td><code>{esc(column)}</code></td><td>{esc(text)}</td><td>{tags(targets)}</td>"
        f'<td>{esc(source)}<span class="mono" style="display:block">{static_doc_links(source)}</span></td>'
        f"<td>{esc(resolution)}</td><td>{esc(period)}</td></tr>"
        for column, text, source, resolution, period, targets in STATIC
    )
    counts = {
        code: sum(code in PURPOSE.get(spec.gold_column, ("", ""))[1].split() for spec in kept)
        for code in TARGETS
    }
    cards = (
        f'<div><span class="k">Variables seleccionadas</span><span class="v">{len(kept)}</span><span class="muted">diarias, de {len({s.source for s in kept})} fuentes · + {len(STATIC)} estáticas</span></div>'
        + "".join(
            f'<div><span class="k">{esc(name)}</span><span class="v">{counts[code]}</span>'
            f'<span class="muted">columnas útiles · <span class="tag">{code}</span></span></div>'
            for code, name in TARGETS.items()
        )
    )
    measured = f"medida el {report['today']}" if report["today"] else "configurada"
    body = f"""
<header style="display:grid;gap:10px">
  <div class="eyebrow">Dengue.IA Cali · climate-pipeline · Gold</div>
  <h1>Diccionario de variables</h1>
  <p class="lede">Solo las variables seleccionadas: {len(kept)} columnas diarias y {len(STATIC)} covariables estáticas, en cada
  grilla (100 m, 1 km y 5 km). Para cada una: qué explica, fuente, resolución original, latencia ({measured}) y para qué
  variable objetivo sirve (<span class="tag">P</span> precipitación, <span class="tag">T</span> temperatura,
  <span class="tag">H</span> humedad relativa, <span class="tag">V</span> viento, <span class="tag">R</span> radiación solar).
  Ninguna fuente es más gruesa que 0.1°, y donde dos fuentes medían lo mismo quedó la más reciente.</p>
</header>
<div class="cards">{cards}</div>
<section>
  <h2>Resumen por fuente</h2>
  <p class="muted"><strong>Latencia de publicación</strong>: días entre la fecha del dato y su publicación (hoy).
  <strong>Latencia efectiva</strong>: días hasta el último valor válido sobre Cali en Gold; en ópticas y radar incluye
  nubes y días sin pasada.</p>
  <div class="scroll"><table>
    <thead><tr><th>Fuente</th><th>Variables</th><th>Resolución original</th><th>Latencia de publicación</th>
    <th>Latencia efectiva</th><th>Organismo</th><th>Documentación</th></tr></thead><tbody>{"".join(summary)}</tbody></table></div>
</section>
<section>
  <h2>Columnas diarias</h2>
  <p class="muted">Cada fila de <code>gold_daily</code> es una celda y un día. En <code>gold_epiweekly</code> cada columna
  se agrega con la regla indicada y trae <code>&lt;columna&gt;_ndays</code> con los días válidos.</p>
  <div class="scroll"><table>
    <thead><tr><th>Columna Gold</th><th>Qué explica</th><th>Sirve para</th><th>Unidad</th><th>Resolución original</th>
    <th>Muestreo</th><th>Latencia publicación</th><th>Latencia efectiva</th><th>Semana epi.</th></tr></thead>
    <tbody>{"".join(rows)}</tbody></table></div>
</section>
<section>
  <h2>Covariables estáticas</h2>
  <p class="muted">Tabla <code>static_covariates</code> (una fila por celda), calculada en 100 m y promediada por bloques a
  1 km y 5 km: <code>climate-pipeline build-static --layers terrain,urban,osm</code>.</p>
  <div class="scroll"><table>
    <thead><tr><th>Columna</th><th>Qué explica</th><th>Sirve para</th><th>Fuente</th><th>Resolución original</th>
    <th>Periodo</th></tr></thead><tbody>{static_rows}</tbody></table></div>
</section>
<footer>Generado por <code>docs/make_docs.py</code> desde el catálogo de variables, el último
<code>climate-pipeline availability</code> y el inventario de Gold del ejercicio. Predictores por objetivo y bibliografía:
<a href="predictores_por_variable.md">predictores_por_variable.md</a>.</footer>
"""
    return page("Diccionario de variables", body)


FLOW = """
flowchart LR
  subgraph P[Proveedores]
    CDS[Copernicus CDS<br/>AgERA5]
    GEE[Google Earth Engine<br/>IMERG, ERA5-Land, GOES, MODIS,<br/>Sentinel-1/2, Landsat, SMAP, VIIRS]
    CHC[CHC / GloH2O<br/>CHIRPS, MSWEP opcionales]
    OSM[OpenStreetMap<br/>Overpass]
  end
  CRON([cron diario<br/>db-sync.yml])
  PLAN{{plan<br/>¿qué falta entre start y hoy?<br/>latencia por fuente}}
  B[(Bronze<br/>NetCDF crudo<br/>grilla nativa)]
  S[(Silver<br/>unidades, QC físico,<br/>recorte all_touched)]
  T{{Grillas pedidas<br/>run.target_grids}}
  subgraph PG[PostgreSQL / Parquet]
    CAT[(ingest_manifest<br/>catálogo)]
    G[(gold_daily · gold_epiweekly<br/>100 m · 500 m · 1 km · 5 km)]
    ST[(static_covariates<br/>terreno + urbano + OSM)]
  end
  COV[coverage<br/>huecos y días de atraso]
  AV[availability<br/>latencia operativa]
  CRON --> PLAN
  CAT -.-> PLAN
  CDS & GEE & CHC --> PLAN --> B --> S --> T --> G
  B -.-> CAT
  S -.-> CAT
  GEE & OSM --> ST
  G --> COV
  CAT -.-> COV
  GEE & CDS & CHC -.-> AV
"""

ER = """
erDiagram
  GRID ||--|{ CELL : "grid_name"
  CELL ||--o{ GOLD_DAILY : "grid_name, cell_id"
  CELL ||--o{ GOLD_EPIWEEKLY : "grid_name, cell_id"
  CELL ||--o| STATIC_COVARIATES : "grid_name, cell_id"
  VARIABLE ||--o{ GOLD_DAILY : "una columna por variable"
  INGEST_MANIFEST }o--o{ GOLD_DAILY : "date (rebuild si Silver cambió)"
  GRID {
    text grid_name PK "100m, 500m, 1km, 5km"
    text crs "EPSG:9377"
    float8_array transform "a,b,c,d,e,f"
    int width
    int height
    text fingerprint
  }
  CELL {
    text grid_name PK
    bigint cell_id PK "fila*ancho + col"
    int row_index
    int col_index
    float8 latitude
    float8 longitude
    geometry geom "polígono EPSG:4326 (PostGIS)"
  }
  GOLD_DAILY {
    text grid_name PK "partición LIST"
    date date PK
    bigint cell_id PK
    real imerg_precip
    real agera5_temp_max "y 71 columnas más"
    timestamptz updated_at
  }
  GOLD_EPIWEEKLY {
    text grid_name PK
    smallint epi_year PK
    smallint epi_week PK
    bigint cell_id PK
    date week_start
    date week_end
    smallint days_observed
    real imerg_precip "suma, exige 7 días"
    smallint imerg_precip_ndays
  }
  STATIC_COVARIATES {
    text grid_name PK
    bigint cell_id PK
    real static_elevation_m
    real urban_buildings_frac "y 22 columnas más"
  }
  VARIABLE {
    text gold_column PK
    text source
    text units
    float8 valid_min
    float8 valid_max
    text weekly_aggregation
    bool selected
  }
  INGEST_MANIFEST {
    text source PK
    text layer PK "bronze|silver|gold"
    text variable PK
    date date PK
    text status "final|prelim|missing"
    text path
    bigint updated_ns
  }
"""


def flow_page() -> str:
    body = f"""
<header style="display:grid;gap:10px">
  <div class="eyebrow">Dengue.IA Cali · climate-pipeline</div>
  <h1>Flujo del proceso</h1>
  <p class="lede">Del proveedor a la tabla del modelo: cada fuente diaria se descarga solo cuando ya debe existir, tal
  como viene (grilla y resolución nativas), y Gold se genera a las resoluciones que se pidan (por ejemplo 100 m, 500 m,
  1 km y 5 km), cada una calculada directamente desde los datos nativos. Todo es idempotente: volver a correr solo pide
  lo que falta o lo preliminar. En operación, un cron diario (<code>.github/workflows/db-sync.yml</code>) corre
  <code>sync</code>: le pregunta a PostgreSQL qué falta entre <code>run.start_date</code> y hoy, baja solo eso y deja la
  base al día.</p>
</header>
<div class="diagram"><pre class="mermaid">{esc(FLOW)}</pre></div>
<section>
  <h2>Pasos</h2>
  <ol>
    <li><strong>Disponibilidad</strong>. <code>climate-pipeline availability</code> consulta a cada proveedor el último
    día completo sobre el AOI y la última semana epidemiológica completa.</li>
    <li><strong>Plan</strong>. Por cada (variable, día) decide <code>new</code>, <code>retry_missing</code>,
    <code>prelim_upgrade</code>, <code>missing_given_up</code> o <code>not_yet_expected</code>, según la latencia
    configurada y el catálogo: la tabla <code>ingest_manifest</code> de PostgreSQL o los manifiestos Parquet de
    <code>state/manifest</code>.</li>
    <li><strong>Bronze</strong>. Descarga en paralelo, en la grilla nativa del proveedor y con margen alrededor del AOI.
    Lo sub-diario (IMERG cada 30 min, GOES cada 10 min, SMAP cada 3 h) se agrega al día civil de Colombia (UTC−5) y el
    día queda <code>missing</code> hasta estar completo.</li>
    <li><strong>Silver</strong>. Por fuente y día: nombres canónicos, unidades destino, QC de rangos físicos y recorte
    <code>all_touched</code> (se conserva todo píxel que toque el AOI).</li>
    <li><strong>Gold</strong> (<code>--target-grid</code>, repetible). Para cada grilla pedida, Silver nativo de cada fuente
    se remuestrea directamente a esa resolución (bilineal para campos continuos, promedio por área para flujos, índices y
    fracciones). Cada grilla tiene QC de consistencia (Tmín ≤ Tmedia ≤ Tmáx) y claves únicas. Opcional:
    <code>--nested</code> remuestrea una vez a la grilla más fina y deriva las demás por promedio exacto de bloques.</li>
    <li><strong>Semana epidemiológica</strong>. Domingo a sábado; las sumas exigen los 7 días y las medias un mínimo de
    días; cada columna trae <code>_ndays</code>.</li>
    <li><strong>Estáticas</strong> (<code>build-static --layers terrain,urban,osm</code>). Terreno del DEM, forma urbana
    desde Earth Engine (compuestos anuales) y vías de OpenStreetMap, calculadas en cada grilla pedida.</li>
    <li><strong>PostgreSQL</strong> (<code>sync</code>, <code>--output-format postgres</code>). Gold se escribe por
    columnas (<code>INSERT … ON CONFLICT DO UPDATE</code> solo de las columnas que trae la corrida), así que un runner
    efímero que solo tiene el Silver de hoy nunca borra lo que otras fuentes escribieron antes.</li>
    <li><strong>Cobertura</strong> (<code>coverage</code>). Por fuente y grilla: días con dato, días sin píxel válido,
    días no publicados, huecos y cuántos días va atrás respecto de lo que ya podría estar publicado.</li>
    <li><strong>Selección sin redundancia</strong>. Antes de descargar, el catálogo deja una columna por magnitud física:
    más reciente, luego mejor resolución, luego más sentido.</li>
  </ol>
</section>
<section>
  <h2>Comandos</h2>
  <div class="scroll"><table>
    <thead><tr><th>Comando</th><th>Qué hace</th></tr></thead><tbody>
    <tr><td><code>availability</code></td><td>Último día disponible por fuente hoy, rezago y semana completa.</td></tr>
    <tr><td><code>plan</code></td><td>Qué (variable, día) se pediría; <code>--github-matrix</code> para CI.</td></tr>
    <tr><td><code>run</code></td><td>Bronze → Silver → Gold en las grillas pedidas con <code>--target-grid</code>.</td></tr>
    <tr><td><code>build-gold</code></td><td>Reconstruye Gold de los días cuyo Silver cambió.</td></tr>
    <tr><td><code>build-static</code></td><td>Covariables estáticas en una o varias grillas anidadas.</td></tr>
    <tr><td><code>make-raster</code></td><td>Tabla Gold → GeoTIFF/COG (día, semana o estática).</td></tr>
    <tr><td><code>variables</code></td><td>Catálogo y decisiones de redundancia.</td></tr>
    <tr><td><code>catalog</code></td><td>Estado de la ingesta por fuente, capa y estado.</td></tr>
    <tr><td><code>coverage</code></td><td>Huecos y días de atraso por fuente y grilla entre dos fechas.</td></tr>
    <tr><td><code>sync</code></td><td>Ciclo completo contra PostgreSQL: plan desde la base → descarga → Gold → semanas →
      estáticas → cobertura.</td></tr>
    <tr><td><code>db init | status | import-lake</code></td><td>Migraciones, estado de la base y carga de un lago local.</td></tr>
    </tbody></table></div>
  <p class="muted">Todo se declara en <code>run:</code> de <code>examples/cali_operational/pipeline.yaml</code> (AOI,
  fuentes, fecha inicial, grillas, estáticas): <code>climate-pipeline sync --config examples/cali_operational/pipeline.yaml</code>.
  Ejemplo completo local: <code>examples/cali_operational/run_pipeline.sh</code>.</p>
</section>
<footer>Generado por <code>docs/make_docs.py</code>. El diagrama se dibuja con Mermaid (se carga desde jsDelivr).</footer>
"""
    return page("Flujo del proceso", body, MERMAID)


def relational_page() -> str:
    body = f"""
<header style="display:grid;gap:10px">
  <div class="eyebrow">Dengue.IA Cali · climate-pipeline · Gold</div>
  <h1>Modelo relacional</h1>
  <p class="lede">En PostgreSQL (esquema <code>climate</code>): <code>grid</code> y <code>cell</code> (con el polígono de
  cada celda si hay PostGIS), las tres tablas Gold particionadas por grilla (100 m, 500 m, 1 km, 5 km o las que se
  pidan), el diccionario <code>variable</code> y el catálogo <code>ingest_manifest</code>. En Parquet/DuckDB son las mismas
  tablas por grilla, con latitud y longitud en cada fila. Las grillas están alineadas a múltiplos de su resolución, así
  que las escalas se cruzan por aritmética de índices, sin tablas de cruce.</p>
</header>
<div class="diagram"><pre class="mermaid">{esc(ER)}</pre></div>
<section>
  <h2>Tablas</h2>
  <div class="scroll"><table>
    <thead><tr><th>Tabla</th><th>Clave</th><th>PostgreSQL</th><th>Parquet</th><th>Contenido</th></tr></thead><tbody>
    <tr><td><code>grid</code></td><td class="mono">grid_name</td><td class="mono">climate.grid</td>
      <td class="mono">gold/&lt;grilla&gt;/grid.json</td>
      <td>CRS, transformada afín y tamaño: define <code>cell_id = fila × ancho + columna</code>.</td></tr>
    <tr><td><code>cell</code></td><td class="mono">(grid_name, cell_id)</td><td class="mono">climate.cell</td>
      <td>latitude/longitude en cada tabla</td>
      <td>Celdas del AOI (<code>all_touched</code>), centroide y polígono <code>geom</code> (PostGIS) para QGIS.</td></tr>
    <tr><td><code>gold_daily</code></td><td class="mono">(grid_name, date, cell_id)</td>
      <td class="mono">climate.gold_daily__&lt;grilla&gt;</td>
      <td class="mono">gold/&lt;grilla&gt;/parquet/daily/year=/month=/date=.parquet</td>
      <td>Una fila por celda del AOI y día, con todas las columnas diarias. Panel completo: los faltantes son NA.</td></tr>
    <tr><td><code>gold_epiweekly</code></td><td class="mono">(grid_name, epi_year, epi_week, cell_id)</td>
      <td class="mono">climate.gold_epiweekly__&lt;grilla&gt;</td>
      <td class="mono">gold/&lt;grilla&gt;/parquet/epiweekly/epi_year=/epi_week=.parquet</td>
      <td>Agregado domingo-sábado con <code>week_start</code>, <code>week_end</code>, <code>days_observed</code> y
      <code>&lt;columna&gt;_ndays</code>.</td></tr>
    <tr><td><code>static_covariates</code></td><td class="mono">(grid_name, cell_id)</td>
      <td class="mono">climate.static_covariates__&lt;grilla&gt;</td>
      <td class="mono">gold/&lt;grilla&gt;/parquet/static/covariates.parquet</td>
      <td>Terreno, forma urbana y OSM. Se une con las tablas diaria y semanal por <code>cell_id</code>.</td></tr>
    <tr><td><code>variable</code></td><td class="mono">gold_column</td><td class="mono">climate.variable</td><td>—</td>
      <td>Diccionario desde el catálogo del código: unidades, rango físico, remuestreo, regla semanal, seleccionada.</td></tr>
    <tr><td><code>ingest_manifest</code></td><td class="mono">(source, layer, variable, date)</td>
      <td class="mono">climate.ingest_manifest</td><td class="mono">state/manifest/&lt;fuente&gt;.parquet</td>
      <td>Estado de cada descarga (final, prelim, missing) y marca de tiempo: decide qué volver a pedir y qué días de
      Gold reconstruir. Vistas <code>ingest_status</code> y <code>source_freshness</code> para monitoreo.</td></tr>
    </tbody></table></div>
  <p class="muted">Las mismas tablas existen en DuckDB (<code>gold/&lt;grilla&gt;/climate.duckdb</code>) con
  <code>--output-format duckdb</code>. Migraciones versionadas en <code>src/climate_pipeline/storage/migrations/</code>.</p>
</section>
<section>
  <h2>Cómo se cruzan las escalas</h2>
  <p>Con una grilla fina de ancho <code>W₁₀₀</code> (por ejemplo 100 m) y una gruesa de factor <code>k</code> (5 para 500 m,
  10 para 1 km, 50 para 5 km), desplazamiento <code>(r₀, c₀)</code> entre sus esquinas y ancho <code>W_k</code>:</p>
  <div class="diagram"><code>fila = cell_id_100 // W₁₀₀ ; col = cell_id_100 % W₁₀₀<br/>
  cell_id_k = ((fila − r₀) // k) × W_k + (col − c₀) // k</code></div>
  <p>La función <code>parent_cell_ids(fine, coarse, cell_ids)</code> de <code>climate_pipeline.domain.grid</code> lo
  calcula. Cada grilla se calcula desde los datos nativos a su propia resolución; con <code>--nested</code>, además, el
  valor de una celda gruesa es exactamente el promedio de sus celdas finas.</p>
  <div class="diagram"><code>-- PostgreSQL: semanas de 2026 en 1 km con covariables estáticas y polígono de la celda<br/>
  SELECT c.geom, w.*, s.static_elevation_m, s.urban_buildings_frac<br/>
  FROM climate.gold_epiweekly w<br/>
  JOIN climate.cell c USING (grid_name, cell_id)<br/>
  JOIN climate.static_covariates s USING (grid_name, cell_id)<br/>
  WHERE w.grid_name = '1km' AND w.epi_year = 2026;<br/><br/>
  -- DuckDB sobre Parquet: lo mismo desde el lago local<br/>
  SELECT w.*, s.* EXCLUDE (cell_id, latitude, longitude)<br/>
  FROM read_parquet('data/gold/1km/parquet/epiweekly/**/*.parquet') w<br/>
  JOIN read_parquet('data/gold/1km/parquet/static/covariates.parquet') s USING (cell_id)<br/>
  WHERE w.epi_year = 2026;</code></div>
</section>
<footer>Generado por <code>docs/make_docs.py</code>. El diagrama se dibuja con Mermaid (se carga desde jsDelivr).</footer>
"""
    return page("Modelo relacional", body, MERMAID)


#: (fuente, producto, organismo, resolución espacial nativa en m, etiqueta, resolución temporal nativa, uso)
RESOLUTION_ROWS = [
    ("sentinel1", "Sentinel-1 GRD IW", "ESA", 10, "10 m", "~6 días (revisita S1C+S1D)", "diaria"),
    ("gee", "Sentinel-2 L2A (índices s2_*)", "ESA", 10, "10-20 m", "~5 días (revisita)", "diaria"),
    (
        "landsat",
        "Landsat 8/9 C2 L2",
        "USGS / NASA",
        30,
        "30 m (térmico 100 m)",
        "~8 días (revisita)",
        "diaria",
    ),
    ("viirs", "VIIRS Black Marble VNP46A2", "NASA", 460, "~460 m (15″)", "diaria", "diaria"),
    ("gee", "MODIS MOD09GA/MYD09GA (índices)", "NASA", 500, "500 m", "diaria (2 pasadas)", "diaria"),
    ("modis", "MODIS LST, nubes y MAIAC", "NASA", 1000, "1 km", "diaria (2 pasadas)", "diaria"),
    ("goes", "GOES-East ABI", "NOAA", 2000, "2 km", "10 min", "diaria (144 escaneos)"),
    ("smap", "SMAP L4", "NASA", 9000, "9 km", "3 h", "diaria"),
    ("imerg", "GPM IMERG V07", "NASA", 11100, "0.1° (~11 km)", "30 min", "diaria"),
    ("agera5", "AgERA5 v2.0", "Copernicus C3S", 11100, "0.1° (~11 km)", "diaria (de horaria)", "diaria"),
    ("era5land", "ERA5-Land", "ECMWF", 11100, "0.1° (~11 km)", "diaria (de horaria)", "diaria"),
]
STATIC_RESOLUTION_ROWS = [
    ("Google Open Buildings 2.5D", "~4 m", "anual (≤2023)"),
    ("ESA WorldCover 2021 / Dynamic World / Sentinel-2", "10 m", "2021 / anual"),
    ("Landsat 8/9 (LST anual)", "30 m", "anual"),
    ("Copernicus DEM GLO-90", "90 m", "fija"),
    ("JRC GHSL", "100 m", "2018 / 2020"),
    ("VIIRS DNB mensual", "~460 m", "anual"),
    ("OpenStreetMap", "vectorial (exacto)", "actual"),
]


def resolution_page() -> str:
    def row_docs(source: str, product: str) -> str:
        links = SOURCE_DOCS.get(source, [])
        if source == "gee":  # the gee source mixes MODIS and Sentinel-2 indices
            keep = ("Sentinel-2", "Cloud") if "Sentinel-2" in product else ("MOD09GA", "MYD09GA")
            links = [link for link in links if link[0].startswith(keep)]
        return doc_links(links)

    rows = "".join(
        f'<tr><td>{esc(product)}</td><td class="mono">{esc(source)}</td><td>{esc(label)}</td>'
        f"<td>{esc(temporal)}</td><td>{esc(agency)}</td><td>{row_docs(source, product)}</td></tr>"
        for source, product, agency, _, label, temporal, _ in sorted(RESOLUTION_ROWS, key=lambda row: row[3])
    )
    static_rows = "".join(
        f"<tr><td>{esc(source)}</td><td>{esc(spatial)}</td><td>{esc(period)}</td><td>{static_doc_links(source)}</td></tr>"
        for source, spatial, period in STATIC_RESOLUTION_ROWS
    )
    body = f"""
<header style="display:grid;gap:10px">
  <div class="eyebrow">Dengue.IA Cali · climate-pipeline</div>
  <h1>Fuentes y resolución</h1>
  <p class="lede">Resolución <strong>original</strong> de cada fuente seleccionada, de la más fina a la más gruesa. La
  más fina es de 10 m (Sentinel-1 y Sentinel-2) y la más gruesa de 0.1° (~11 km: IMERG, AgERA5, ERA5-Land); en tiempo,
  la más fina es de 10 min (GOES) y la más gruesa la revisita de ~8 días de Landsat. Todo se descarga así y Gold se
  genera a la resolución que se pida (por ejemplo 100 m, 500 m, 1 km y 5 km); en Gold todo es diario.</p>
</header>
<section>
  <h2>Variables diarias</h2>
  <div class="scroll"><table>
    <thead><tr><th>Fuente</th><th>--sources</th><th>Resolución espacial</th><th>Resolución temporal</th><th>Organismo</th><th>Documentación</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
</section>
<section>
  <h2>Covariables estáticas</h2>
  <div class="scroll"><table>
    <thead><tr><th>Fuente</th><th>Resolución espacial</th><th>Periodo</th><th>Documentación</th></tr></thead>
    <tbody>{static_rows}</tbody></table></div>
</section>
<footer>Generado por <code>docs/make_docs.py</code>.</footer>
"""
    return page("Fuentes y resolución", body)


def _inline(text: str) -> str:
    """Markdown inline subset: links, bold, italics, code."""
    import re

    text = esc(text)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', text
    )
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"(?<![*\w])\*([^*]+)\*(?![*\w])", r"<em>\1</em>", text)


def markdown_to_html(markdown: str) -> str:
    """Enough Markdown for docs/predictores_por_variable.md: headings, paragraphs, tables, lists."""
    out, paragraph, table, items = [], [], [], []

    def flush() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()
        if table:
            header, *body = [row for row in table if not set(row.replace("|", "").strip()) <= {"-", ":", " "}]
            cells = lambda row: [cell.strip() for cell in row.strip().strip("|").split("|")]  # noqa: E731
            head = "".join(f"<th>{_inline(cell)}</th>" for cell in cells(header))
            rows = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells(row)) + "</tr>" for row in body
            )
            out.append(
                f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>'
            )
            table.clear()
        if items:
            tag = "ol" if items[0][0] == "ol" else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(text)}</li>" for _, text in items) + f"</{tag}>")
            items.clear()

    import re

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("# "):
            flush()
        elif stripped.startswith("## "):
            flush()
            out.append(f"</section><section><h2>{_inline(stripped[3:])}</h2>")
        elif stripped.startswith("|"):
            table.append(stripped)
        elif stripped.startswith("- "):
            items.append(("ul", stripped[2:]))
        elif re.match(r"^\d+\. ", stripped):
            items.append(("ol", stripped.split(". ", 1)[1]))
        elif items and line.startswith("   "):
            items[-1] = (items[-1][0], items[-1][1] + " " + stripped)
        else:
            paragraph.append(stripped)
    flush()
    return "<section>" + "".join(out) + "</section>"


def predictors_page() -> str:
    markdown = (ROOT / "docs/predictores_por_variable.md").read_text()
    intro, _, rest = markdown.partition("\n## ")
    intro_text = " ".join(line for line in intro.splitlines()[1:] if line.strip())
    body = f"""
<header style="display:grid;gap:10px">
  <div class="eyebrow">Dengue.IA Cali · climate-pipeline</div>
  <h1>Papers por variable</h1>
  <p class="lede">{_inline(intro_text)}</p>
</header>
{markdown_to_html("## " + rest)}
<footer>Generado por <code>docs/make_docs.py</code> desde <code>docs/predictores_por_variable.md</code>.</footer>
"""
    return page("Papers por variable", body.replace("<section></section>", ""))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "resolucion_fuentes.html": resolution_page(),
        "predictores_por_variable.html": predictors_page(),
        "diccionario_variables.html": dictionary_page(),
        "flujo_proceso.html": flow_page(),
        "modelo_relacional.html": relational_page(),
    }
    for name, content in outputs.items():
        (args.output_dir / name).write_text(content)
        print(args.output_dir / name)


if __name__ == "__main__":
    main()
