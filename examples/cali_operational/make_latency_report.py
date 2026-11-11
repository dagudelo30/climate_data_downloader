"""Operational latency page: what each source delivers if the model is fed *today*.

Reads the JSON written by ``climate-pipeline availability`` and writes a self-contained HTML
page (no JavaScript, inline SVG) with a per-source timeline, the lag in days, the last
complete epidemiological week and what it implies for a weekly dengue forecast.

    uv run climate-pipeline availability --aoi-path data/auxiliary/shapefiles/cali/cali.shp \
        --output outputs/cali_operational/reports/availability_$(date +%F).json
    uv run python examples/cali_operational/make_latency_report.py \
        outputs/cali_operational/reports/availability_$(date +%F).json \
        --output outputs/cali_operational/latencia_operativa.html
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import date, timedelta
from pathlib import Path

from climate_pipeline.config import load_pipeline_config
from climate_pipeline.domain.dates import EpiWeek
from climate_pipeline.pipeline.availability import NATIVE_RESOLUTION, SOURCE_DOCS, variable_latencies
from climate_pipeline.pipeline.orchestrator import build_variable_catalog

GROUPS = {
    "Precipitación": ["imerg"],
    "Reanálisis agroclimático": ["agera5", "era5land"],
    "Geoestacionario (nubes y vapor)": ["goes"],
    "Superficie desde satélite": ["modis", "gee", "smap", "viirs"],
}
LABELS = {
    "chirps": "CHIRPS v3",
    "imerg": "GPM IMERG V07",
    "gsmap": "GSMaP v8",
    "mswep": "MSWEP v2.8",
    "agera5": "AgERA5",
    "era5land": "ERA5-Land",
    "goes": "GOES-East ABI",
    "modis": "MODIS LST, nubes, MAIAC",
    "gee": "MODIS/Sentinel-2 índices",
    "smap": "SMAP L4",
    "viirs": "VIIRS luces nocturnas",
}
NOTES = {
    "imerg": "Seleccionada para precipitación: la más reciente. Corrida provisional; la final (Final Run) llega a Earth Engine ~13 meses después.",
    "chirps": "No seleccionada: 0.05° pero con 7-8 días de rezago (preliminar).",
    "gsmap": "Empata con IMERG en latencia y resolución; se prefiere IMERG.",
    "mswep": "Empata con IMERG; requiere el acceso a Google Drive que otorga GloH2O (no configurado).",
    "agera5": "El CDS publica cada día con ~7-8 días de rezago; no se consulta en vivo porque cada petición hace cola.",
    "era5land": "Solo aporta lo que AgERA5 no tiene: viento u/v, suelo, flujos, onda larga, escorrentía, LAI y albedo.",
    "goes": "Escaneo cada 10 min; un día está completo con ≥120 de 144 escaneos.",
    "modis": "Día publicado no es día despejado: LST, vapor y aerosoles tienen huecos por nubes.",
    "gee": "Ópticos: un día publicado puede estar totalmente nublado sobre Cali.",
    "smap": "Asimilación del radiómetro SMAP en un modelo de superficie (L4).",
    "viirs": "Las imágenes de los últimos días se publican vacías y se llenan después.",
}
WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def short(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]}"


def resolution(item: dict) -> tuple[float | None, str]:
    """Native resolution from the report, or from the catalog for reports written before it existed."""
    if item.get("native_resolution"):
        return item.get("native_resolution_m"), item["native_resolution"]
    return NATIVE_RESOLUTION.get(item["source"], (None, item.get("resolution", "")))


def compact(metres: float | None) -> str:
    if metres is None:
        return ""
    if metres < 1000:
        return f"{metres:.0f} m"
    return f"{metres / 1000:.1f} km" if metres < 10_000 else f"{metres / 1000:.0f} km"


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def timeline_svg(items: list[dict], today: date, days_back: int) -> str:
    """One row per source: data available up to ``latest_day`` (solid), lag to today (hatched)."""
    label_w, row_h, top, right_pad = 236, 30, 44, 18
    width = 940
    plot_w = width - label_w - right_pad
    start = today - timedelta(days=days_back)
    rows = [
        item for group in GROUPS.values() for source in group for item in items if item["source"] == source
    ]
    height = top + row_h * len(rows) + 30

    def x(day: date) -> float:
        return label_w + plot_w * (day - start).days / (days_back + 1)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Última fecha disponible por fuente" '
        f'class="timeline">',
        '<defs><pattern id="lag" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        '<line x1="0" y1="0" x2="0" y2="6" class="hatch"/></pattern></defs>',
    ]
    # Epidemiological weeks (Sunday-Saturday) as alternating bands with their label.
    sunday = start - timedelta(days=(start.weekday() + 1) % 7)
    band = 0
    while sunday <= today:
        week = EpiWeek.from_date(sunday)
        x0, x1 = max(x(sunday), label_w), min(x(sunday + timedelta(days=7)), label_w + plot_w)
        if x1 > x0:
            klass = "band-a" if band % 2 == 0 else "band-b"
            parts.append(
                f'<rect x="{x0:.1f}" y="{top - 22}" width="{x1 - x0:.1f}" height="{height - top - 8}" class="{klass}"/>'
            )
            if x1 - x0 > 40:
                parts.append(
                    f'<text x="{(x0 + x1) / 2:.1f}" y="{top - 8}" class="week">SE {week.week:02d}</text>'
                )
        sunday += timedelta(days=7)
        band += 1
    for index, item in enumerate(rows):
        y = top + index * row_h
        label = LABELS.get(item["source"], item["source"])
        metres, _ = resolution(item)
        parts.append(
            f'<text x="{label_w - 12}" y="{y + row_h / 2 + 4}" class="row-label">{esc(label)}'
            f'<tspan class="row-res"> · {esc(compact(metres))}</tspan></text>'
        )
        if not item.get("latest_day"):
            parts.append(f'<text x="{label_w + 6}" y="{y + row_h / 2 + 4}" class="muted">sin dato</text>')
            continue
        latest = date.fromisoformat(item["latest_day"])
        bar_end = x(latest + timedelta(days=1))
        klass = "bar-prelim" if item["status"] == "prelim" else "bar-final"
        if item["method"] == "config":
            klass += " bar-config"
        parts.append(
            f'<rect x="{label_w}" y="{y + 7}" width="{max(bar_end - label_w, 0):.1f}" height="{row_h - 14}" rx="2" class="{klass}"/>'
        )
        final_day = item.get("final_latest_day")
        if final_day and date.fromisoformat(final_day) >= start:
            fx = x(date.fromisoformat(final_day) + timedelta(days=1))
            parts.append(
                f'<line x1="{fx:.1f}" x2="{fx:.1f}" y1="{y + 4}" y2="{y + row_h - 4}" class="final-tick"/>'
            )
        parts.append(
            f'<rect x="{bar_end:.1f}" y="{y + 7}" width="{max(x(today + timedelta(days=1)) - bar_end, 0):.1f}" height="{row_h - 14}" fill="url(#lag)" class="lag"/>'
        )
        parts.append(
            f'<text x="{bar_end + 6:.1f}" y="{y + row_h / 2 + 4}" class="lag-label">{item["lag_days"]} d</text>'
        )
    tx = x(today + timedelta(days=1))
    parts.append(f'<line x1="{tx:.1f}" x2="{tx:.1f}" y1="{top - 22}" y2="{height - 8}" class="today"/>')
    parts.append(f'<text x="{tx - 4:.1f}" y="{height - 12}" class="today-label">hoy</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def load_last_valid(inventory: Path, grid: str = "1km") -> dict[str, str]:
    """Last day with a valid value per Gold column (from summarize_gold.py)."""
    if not inventory.exists():
        return {}
    import csv

    with inventory.open() as handle:
        return {
            row["column"]: row["last_date"][:10]
            for row in csv.DictReader(handle)
            if row["grid"] == grid and row["last_date"]
        }


def when_text(lag: int | None) -> str:
    return "—" if lag is None else f"el día D se tiene el D+{lag}"


def selected_variables_section(config_path: Path, report: dict, inventory: Path) -> str:
    config = load_pipeline_config(config_path)
    catalog = build_variable_catalog(config)
    sources = [item["source"] for item in report["sources"]]
    columns = [spec.gold_column for spec in catalog.all() if spec.default and spec.source in sources]
    rows = variable_latencies(report, columns, load_last_valid(inventory))
    by_source: dict[str, list[dict]] = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row)
    has_effective = any(row["last_valid_day"] for row in rows)
    body = []
    for group, group_sources in GROUPS.items():
        present = [source for source in group_sources if source in by_source]
        if not present:
            continue
        body.append(f'<tr class="group"><th colspan="6" scope="colgroup">{esc(group)}</th></tr>')
        for source in present:
            items = by_source[source]
            lags = [row["publication_lag_days"] for row in items if row["publication_lag_days"] is not None]
            body.append(
                f'<tr class="subgroup"><th colspan="6" scope="rowgroup">{esc(LABELS.get(source, source))} · '
                f"{len(items)} {'variable' if len(items) == 1 else 'variables'}"
                + (
                    f" · publica con {min(lags)}-{max(lags)} d"
                    if lags and min(lags) != max(lags)
                    else f" · publica con {lags[0]} d"
                    if lags
                    else ""
                )
                + "</th></tr>"
            )
            for row in items:
                effective = row["effective_lag_days"]
                body.append(
                    f"<tr><td><code>{esc(row['column'])}</code></td>"
                    f'<td class="num">{esc(short(date.fromisoformat(row["published_day"])) if row["published_day"] else "—")}</td>'
                    f'<td class="num">{esc(row["publication_lag_days"])} d</td>'
                    f'<td class="num">{esc(short(date.fromisoformat(row["last_valid_day"])) if row["last_valid_day"] else "—")}</td>'
                    f'<td class="num"><span class="lag-pill lag-{min((effective or 0) // 4, 3)}">'
                    f"{esc(effective if effective is not None else '—')}{' d' if effective is not None else ''}</span></td>"
                    f"<td>{esc(when_text(row['publication_lag_days']))}</td></tr>"
                )
    note = (
        "La latencia efectiva es el último día con un valor válido sobre Cali en Gold de 1 km: en las fuentes ópticas"
        " (MODIS, Sentinel-2) es mayor que la de publicación porque hay días nublados o sin pasada."
        if has_effective
        else "La latencia efectiva aparece cuando existe la corrida del ejercicio (tables/column_inventory.csv)."
    )
    return f"""
  <section>
    <h2>{len(columns)} variables diarias seleccionadas</h2>
    <p>Una columna por magnitud física: entre fuentes que miden lo mismo se eligió la más reciente, luego la de mejor
    resolución. {esc(note)}</p>
    <div class="scroll">
      <table>
        <thead><tr><th scope="col">Columna Gold</th><th scope="col">Publicado hasta</th><th scope="col">Latencia de publicación</th>
        <th scope="col">Último dato válido</th><th scope="col">Latencia efectiva</th><th scope="col">Cuándo sale</th></tr></thead>
        <tbody>{"".join(body)}</tbody>
      </table>
    </div>
  </section>
"""


def render(
    report: dict,
    config_path: Path = Path("examples/cali_operational/pipeline.yaml"),
    inventory: Path = Path("outputs/cali_operational/tables/column_inventory.csv"),
) -> str:
    today = date.fromisoformat(report["today"])
    items = report["sources"]
    by_source = {item["source"]: item for item in items}
    current = EpiWeek.from_date(today)
    common_day = date.fromisoformat(report["all_sources_complete_through"])
    common_week = report["all_sources_last_complete_epiweek"]
    probed = [item for item in items if item["method"] == "probe" and item.get("latest_day")]
    slowest = max(probed, key=lambda item: item["lag_days"]) if probed else None
    without_slowest = [item for item in items if slowest is None or item["source"] != slowest["source"]]
    others_day = min(
        date.fromisoformat(item["latest_day"]) for item in without_slowest if item.get("latest_day")
    )
    others_week = EpiWeek.from_date(others_day - timedelta(days=(others_day.weekday() - 5) % 7))
    weeks_behind = (
        current.week - int(common_week.split("W")[1]) if common_week.startswith(str(current.year)) else None
    )
    days_back = 45

    group_rows = []
    for group, sources in GROUPS.items():
        group_items = [by_source[source] for source in sources if source in by_source]
        if not group_items:
            continue
        group_rows.append(f'<tr class="group"><th colspan="8" scope="colgroup">{esc(group)}</th></tr>')
        for item in group_items:
            status = "preliminar" if item["status"] == "prelim" else "final"
            method = "medido hoy" if item["method"] == "probe" else "latencia configurada"
            notes = NOTES.get(item["source"], item.get("notes") or "")
            if item.get("final_latest_day") and item["status"] == "prelim":
                notes = f"Final hasta {short(date.fromisoformat(item['final_latest_day']))}. {notes}"
            details = item.get("details") or {}
            if len(details) > 1:
                pairs = "; ".join(
                    f"{key.replace(item['source'] + '_', '')}: {value}" for key, value in details.items()
                )
                notes = f"{notes} Por componente: {pairs}.".strip()
            _, label = resolution(item)
            latest = esc(short(date.fromisoformat(item["latest_day"])) if item.get("latest_day") else "—")
            group_rows.append(
                "<tr>"
                f'<td><span class="name">{esc(LABELS.get(item["source"], item["source"]))}</span>'
                f'<span class="sub">{esc(item["agency"])}</span>'
                + "".join(
                    f'<a class="doc" href="{esc(url)}" target="_blank" rel="noopener">{esc(label)}</a>'
                    for label, url in SOURCE_DOCS.get(item["source"], [])
                )
                + "</td>"
                f'<td><span class="res">{esc(label)}</span></td>'
                f'<td class="num"><span class="lag-pill lag-{min((item["lag_days"] or 0) // 4, 3)}">'
                f"{esc(item['lag_days'])} días</span></td>"
                f'<td class="num">{latest}</td>'
                f'<td class="num">{esc(item.get("last_complete_epiweek") or "—")}</td>'
                f'<td><span class="chip chip-{item["status"]}">{status}</span></td>'
                f'<td class="method">{method}</td>'
                f'<td class="notes">{esc(notes)}<span class="vars">{esc(len(item["variables"]))} {"columna" if len(item["variables"]) == 1 else "columnas"}: '
                f"{esc(', '.join(item['variables']))}</span></td>"
                "</tr>"
            )

    weekday = WEEKDAYS[today.weekday()]
    return f"""<title>Latencia de fuentes climáticas</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=Archivo+Narrow:wght@500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: an operations bulletin. Answer first (what is complete today), then the per-source
   timeline against epidemiological weeks, then the reference table. */
:root {{
  --bg: #f6f7f4; --surface: #ffffff; --fg: #1d2421; --muted: #5c6862; --rule: #d9ddd7;
  --band-a: #eef0eb; --band-b: #f6f7f4;
  --accent: #1f6f5c; --accent-soft: #d6ebe4; --prelim: #b7791f; --prelim-soft: #f6e7cc;
  --today: #b4372c; --hatch: #c7ccc6;
  --font-display: "Archivo Narrow", "Arial Narrow", "Helvetica Neue", Arial, sans-serif;
  --font-body: "Archivo", "Helvetica Neue", Arial, sans-serif;
  --font-data: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #141917; --surface: #1b211e; --fg: #e5ebe7; --muted: #9aa69f; --rule: #2e3833;
  --band-a: #1b221f; --band-b: #141917; --accent: #5fbfa3; --accent-soft: #1f3a32;
  --prelim: #e2a84b; --prelim-soft: #3b2f1a; --today: #f07a6c; --hatch: #3a4540; color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #141917; --surface: #1b211e; --fg: #e5ebe7; --muted: #9aa69f; --rule: #2e3833;
  --band-a: #1b221f; --band-b: #141917; --accent: #5fbfa3; --accent-soft: #1f3a32;
  --prelim: #e2a84b; --prelim-soft: #3b2f1a; --today: #f07a6c; --hatch: #3a4540; color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.55 var(--font-body); }}
.page {{ max-width: 1080px; margin: 0 auto; padding-block: 36px 56px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 32px; }}
header {{ display: grid; gap: 10px; }}
.eyebrow {{ font: 600 12px/1 var(--font-data); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }}
h1 {{ font: 600 clamp(28px, 4.4vw, 44px)/1.08 var(--font-display); margin: 0; text-wrap: balance; letter-spacing: -.01em; }}
h2 {{ font: 600 22px/1.2 var(--font-display); margin: 0; text-wrap: balance; }}
p {{ margin: 0; max-width: 68ch; }}
.lede {{ color: var(--muted); }}
.answer {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 1px;
  background: var(--rule); border: 1px solid var(--rule); }}
.answer > div {{ background: var(--surface); padding: 18px 20px; display: grid; gap: 4px; align-content: start; }}
.answer .k {{ font: 500 12px/1.3 var(--font-data); color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
.answer .v {{ font: 600 30px/1.1 var(--font-display); font-variant-numeric: tabular-nums; }}
.answer .v.accent {{ color: var(--accent); }}
.answer .d {{ color: var(--muted); font-size: 13.5px; }}
section {{ display: grid; gap: 14px; min-width: 0; }}
.scroll {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--rule); }}
svg.timeline {{ display: block; min-width: 760px; width: 100%; height: auto; font-family: var(--font-data); }}
.band-a {{ fill: var(--band-a); }} .band-b {{ fill: var(--band-b); }}
.week {{ font-size: 11px; fill: var(--muted); text-anchor: middle; letter-spacing: .04em; }}
.row-label {{ font: 500 12.5px var(--font-body); fill: var(--fg); text-anchor: end; }}
.bar-final {{ fill: var(--accent); }} .bar-prelim {{ fill: var(--prelim); }}
.bar-config {{ opacity: .55; }}
.hatch {{ stroke: var(--hatch); stroke-width: 3; }}
.lag {{ stroke: none; }}
.lag-label {{ font-size: 11.5px; fill: var(--muted); }}
.final-tick {{ stroke: var(--fg); stroke-width: 2; }}
.today {{ stroke: var(--today); stroke-width: 1.5; stroke-dasharray: 3 3; }}
.today-label {{ font-size: 11px; fill: var(--today); text-anchor: end; }}
.muted {{ fill: var(--muted); font-size: 12px; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 8px 18px; font-size: 13px; color: var(--muted); }}
.legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
.sw {{ width: 18px; height: 10px; display: inline-block; border-radius: 2px; }}
.sw.final {{ background: var(--accent); }} .sw.prelim {{ background: var(--prelim); }}
.sw.config {{ background: var(--accent); opacity: .55; }}
.sw.lagsw {{ background: repeating-linear-gradient(45deg, var(--hatch) 0 2px, transparent 2px 5px); }}
.sw.tick {{ width: 2px; height: 14px; background: var(--fg); }}
table {{ border-collapse: collapse; width: 100%; min-width: 860px; font-size: 13.5px; }}
th, td {{ text-align: left; vertical-align: top; padding: 10px 12px; border-bottom: 1px solid var(--rule); }}
thead th {{ font: 500 11.5px/1.3 var(--font-data); text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }}
tr.subgroup th {{ font: 600 13px/1.3 var(--font-data); color: var(--muted); padding-top: 12px; }}
tr.group th {{ font: 600 15px/1.2 var(--font-display); padding-top: 18px; color: var(--fg); background: var(--band-a); }}
.name {{ display: block; font-weight: 600; }}
.sub {{ display: block; color: var(--muted); font-size: 12.5px; }}
.num {{ font-family: var(--font-data); font-variant-numeric: tabular-nums; white-space: nowrap; }}
.method {{ color: var(--muted); white-space: nowrap; }}
.notes {{ color: var(--muted); max-width: 38ch; }}
.vars {{ display: block; margin-top: 4px; font: 11.5px/1.45 var(--font-data); color: var(--muted); word-break: break-word; }}
.chip {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
.chip-final {{ background: var(--accent-soft); color: var(--accent); }}
.chip-prelim {{ background: var(--prelim-soft); color: var(--prelim); }}
.lag-pill {{ display: inline-block; min-width: 4.6em; }}
a.doc {{ display: inline-block; margin: 3px 8px 0 0; font: 500 11.5px var(--font-data); color: var(--accent); }}
.res {{ display: block; font: 500 13px/1.4 var(--font-data); font-variant-numeric: tabular-nums; max-width: 24ch; }}
.row-res {{ fill: var(--muted); font: 400 11.5px var(--font-data); }}
.lag-2, .lag-3 {{ color: var(--today); font-weight: 600; }}
ol {{ margin: 0; padding-left: 1.2em; display: grid; gap: 8px; max-width: 76ch; }}
footer {{ color: var(--muted); font-size: 12.5px; border-top: 1px solid var(--rule); padding-top: 14px; }}
code {{ font-family: var(--font-data); font-size: .92em; }}
</style>
<main class="page">
  <header>
    <div class="eyebrow">Dengue.IA Cali · disponibilidad de covariables · {esc(report["aoi"])}</div>
    <h1>Si hoy, {weekday} {short(today)} de {today.year}, consulto todas las fuentes, ¿hasta qué día tengo datos?</h1>
    <p class="lede">Medido contra cada proveedor el {esc(report["generated_at"][:16].replace("T", " "))} UTC. Hoy empieza la
    semana epidemiológica <strong>{current.label}</strong> ({short(current.start)} – {short(current.end)}). Una semana sirve
    al modelo cuando sus siete días (domingo a sábado) están publicados.</p>
  </header>

  <div class="answer">
    <div><span class="k">Todas las fuentes completas hasta</span><span class="v accent">{short(common_day)}</span>
      <span class="d">Última semana completa con todo: <strong>{esc(common_week)}</strong>{f", {weeks_behind} semanas antes de la actual" if weeks_behind is not None else ""}.</span></div>
    <div><span class="k">Sin {esc(LABELS.get(slowest["source"], slowest["source"])) if slowest else "la más lenta"}</span><span class="v">{short(others_day)}</span>
      <span class="d">Semana completa: <strong>{others_week.label}</strong>. {esc(LABELS.get(slowest["source"], "")) if slowest else ""} es la fuente que más retrasa ({slowest["lag_days"] if slowest else "—"} días).</span></div>
    <div><span class="k">Más rápidas</span><span class="v">{min(item["lag_days"] for item in probed)} días</span>
      <span class="d">{esc(", ".join(LABELS.get(item["source"], item["source"]) for item in probed if item["lag_days"] == min(i["lag_days"] for i in probed)))}.</span></div>
  </div>

  <section>
    <h2>Última fecha disponible por fuente</h2>
    <div class="legend">
      <span><i class="sw final"></i>dato final</span><span><i class="sw prelim"></i>dato preliminar (se reemplaza)</span>
      <span><i class="sw config"></i>latencia configurada, no medida</span><span><i class="sw lagsw"></i>rezago hasta hoy</span>
      <span><i class="sw tick"></i>fin del dato final (CHIRPS)</span>
    </div>
    <div class="scroll">{timeline_svg(items, today, days_back)}</div>
  </section>

  <section>
    <h2>Qué implica para el modelo semanal</h2>
    <ol>
      <li>Un pronóstico emitido hoy, al inicio de {current.label}, solo puede usar covariables completas hasta
      <strong>{esc(common_week)}</strong> si exige todas las fuentes, o hasta <strong>{others_week.label}</strong> sin
      {esc(LABELS.get(slowest["source"], "")) if slowest else ""}. Entrenar con rezagos de al menos
      {weeks_behind if weeks_behind is not None else 2} semanas evita usar en el entrenamiento información que en operación no existe.</li>
      <li>IMERG entrega primero un dato <strong>provisional</strong> (y CHIRPS, si se usa, uno preliminar). El pipeline lo marca como
      <code>prelim</code> y lo vuelve a descargar cuando sale el final. Para reentrenar conviene usar solo semanas finales;
      para pronosticar se usa el preliminar.</li>
      <li>Las fuentes ópticas (MODIS, Sentinel-2) publican a diario, pero un día publicado puede estar cubierto de nubes
      sobre Cali. Su latencia efectiva es mayor: hay que usar promedios semanales con el conteo <code>_ndays</code>.</li>
      <li>La corrida diaria (<code>run --incremental</code>) pide a cada fuente solo los días que ya pueden existir según su
      latencia y vuelve a intentar los faltantes. Este reporte se regenera con <code>climate-pipeline availability</code>.</li>
    </ol>
  </section>

  <section>
    <h2>Detalle por fuente</h2>
    <div class="scroll">
      <table>
        <thead><tr><th scope="col">Fuente</th><th scope="col">Resolución original</th>
        <th scope="col">Latencia hoy</th><th scope="col">Último día</th>
        <th scope="col">Semana completa</th><th scope="col">Estado</th><th scope="col">Cómo se obtuvo</th><th scope="col">Notas y columnas Gold</th></tr></thead>
        <tbody>
        {"".join(group_rows)}
        </tbody>
      </table>
    </div>
  </section>

  {selected_variables_section(config_path, report, inventory)}

  <footer>Generado por <code>examples/cali_operational/make_latency_report.py</code> a partir de
  <code>climate-pipeline availability</code>. "Medido hoy" consulta el catálogo del proveedor (Earth Engine y CHC); AgERA5 y MSWEP usan la latencia configurada porque su consulta en vivo requiere la cola del CDS o
  credenciales de Google Drive. El rezago se cuenta en días desde el último día completo sobre el AOI.</footer>
</main>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("availability", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("outputs/cali_operational/latencia_operativa.html")
    )
    parser.add_argument("--config", type=Path, default=Path("examples/cali_operational/pipeline.yaml"))
    args = parser.parse_args()
    report = json.loads(args.availability.read_text())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    body = render(report, args.config)
    document = (
        '<!doctype html>\n<html lang="es">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f"{body.split('<main', 1)[0]}</head>\n<body>\n<main{body.split('<main', 1)[1]}</body>\n</html>\n"
    )
    args.output.write_text(document)
    print(args.output)


if __name__ == "__main__":
    main()
