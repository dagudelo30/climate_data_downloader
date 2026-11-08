"""How much NDVI/NDWI each optical option leaves over Cali, and how clean it is.

Measured in Earth Engine on the 1 km Gold cells (``data/gold/1km``), January-May of each year:

* coverage: share of (cell, day) and of (cell, epidemiological week) with at least one clear
  pixel, for MODIS Terra+Aqua under several ``state_1km`` masks and for Sentinel-2 + Cloud Score+;
* quality: NDVI distribution of the MODIS pixel-days each mask level adds (clouds pull NDVI
  towards 0), which is what justifies keeping or dropping each QA flag.

Writes ``qc/gee_cloud_mask_diagnostic.{md,csv}`` under ``--output-dir``.

Usage: ``uv run python examples/cali_2026/gee_cloud_mask_diagnostic.py --output-dir outputs/...``
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box

from climate_pipeline.config import Settings
from climate_pipeline.domain.grid import GridSpec
from climate_pipeline.extractors.gee import CLOUD_SCORE_PLUS_THRESHOLD, initialize_earth_engine
from climate_pipeline.storage.gold import GoldTable, OutputFormat, open_gold_store
from climate_pipeline.storage.layout import DataLake

MODIS = ("MODIS/061/MOD09GA", "MODIS/061/MYD09GA")
OPTIONS = {
    "modis_none": "MODIS sin máscara (código original)",
    "modis_strict": "MODIS `strict` (nube, sombra, nube interna, adyacente)",
    "modis_standard": "MODIS `standard` (nube, sombra, nube interna) — por defecto",
    "modis_cloud_state_only": "MODIS solo estado de nube MOD35",
    "s2_cloud_score_plus": f"Sentinel-2 + Cloud Score+ (cs_cdf ≥ {CLOUD_SCORE_PLUS_THRESHOLD})",
}
QUALITY_CLASSES = {
    "clear_strict": "despejado (pasa `strict`)",
    "adjacent_only": "solo marcado adyacente a nube",
    "shadow_or_internal_cloud": "sombra o nube interna",
    "cloudy_mod35": "nublado (MOD35)",
}


def cells_feature_collection(ee, lake: DataLake):
    grid = GridSpec.load(lake.gold_layout("1km").grid_path)
    store = open_gold_store(lake.gold_layout("1km"), OutputFormat.PARQUET)
    cell_ids = np.sort(store.query(f"SELECT DISTINCT cell_id FROM {GoldTable.STATIC}")["cell_id"].to_numpy())
    a, _, c, _, e, f = grid.transform
    rows, cols = np.divmod(cell_ids, grid.width)
    cells = gpd.GeoDataFrame(
        {"cell_id": cell_ids},
        geometry=[
            box(c + col * a, f + (row + 1) * e, c + (col + 1) * a, f + row * e)
            for row, col in zip(rows, cols, strict=True)
        ],
        crs=grid.crs,
    ).to_crs("EPSG:4326")
    return ee.FeatureCollection(json.loads(cells.to_json()))


def clear_collection(ee, option: str, start: str, end: str, region):
    """Per-image 'clear' masks (1 where usable), keeping the acquisition time."""

    def keep_time(mask, image):
        return (
            mask.rename("clear").selfMask().toByte().set("system:time_start", image.get("system:time_start"))
        )

    if option == "s2_cloud_score_plus":
        s2 = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterDate(start, end).filterBounds(region)
        scores = ee.ImageCollection("GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED")
        linked = s2.linkCollection(scores, ["cs_cdf"])
        return linked.map(lambda i: keep_time(i.select("cs_cdf").gte(CLOUD_SCORE_PLUS_THRESHOLD), i)), 20

    def modis(image):
        state = image.select("state_1km")
        valid = image.select("sur_refl_b01").gte(-100).And(image.select("sur_refl_b02").gte(-100))
        clear = state.bitwiseAnd(0b11).eq(0)
        no_shadow_internal = state.bitwiseAnd(1 << 2).eq(0).And(state.bitwiseAnd(1 << 10).eq(0))
        not_adjacent = state.bitwiseAnd(1 << 13).eq(0)
        masks = {
            "modis_none": valid,
            "modis_cloud_state_only": valid.And(clear),
            "modis_standard": valid.And(clear).And(no_shadow_internal),
            "modis_strict": valid.And(clear).And(no_shadow_internal).And(not_adjacent),
        }
        return keep_time(masks[option], image)

    collection = ee.ImageCollection(MODIS[0]).merge(ee.ImageCollection(MODIS[1]))
    return collection.filterDate(start, end).filterBounds(region).map(modis), 500


def clear_matrix(ee, option: str, year: int, cells) -> np.ndarray:
    """Boolean (day, cell): at least one clear pixel of any image that day."""
    start, end = date(year, 1, 1), date(year, 6, 1)
    n_days = (end - start).days
    collection, scale = clear_collection(
        ee, option, start.isoformat(), end.isoformat(), cells.geometry().bounds()
    )
    empty = ee.Image(0).selfMask().rename("clear").toByte()
    origin = ee.Date(start.isoformat())

    def daily(offset):
        day = origin.advance(offset, "day")
        images = collection.filterDate(day, day.advance(1, "day"))
        return ee.Image(ee.Algorithms.If(images.size().gt(0), images.max(), empty))

    # One band per day and a single reduction: per-day reductions exceed EE's concurrency limit.
    stack = ee.ImageCollection.fromImages(ee.List.sequence(0, n_days - 1).map(daily)).toBands()
    features = stack.reduceRegions(cells, ee.Reducer.count(), scale, tileScale=4).getInfo()["features"]
    matrix = np.zeros((n_days, len(features)), dtype=bool)
    for j, feature in enumerate(features):
        for key, value in feature["properties"].items():
            if key.endswith("_clear"):
                matrix[int(key.split("_")[0]), j] = (value or 0) > 0
    return matrix


def weekly_coverage(clear: np.ndarray, year: int, min_days: int) -> float:
    """Share of (cell, complete epi week inside January-May) with >= min_days clear days."""
    start = date(year, 1, 1)
    sunday = start + timedelta(days=(6 - start.weekday()) % 7)
    weeks = []
    while sunday + timedelta(days=6) < date(year, 6, 1):
        offset = (sunday - start).days
        weeks.append(clear[offset : offset + 7].sum(axis=0) >= min_days)
        sunday += timedelta(days=7)
    return float(np.mean(weeks))


def ndvi_by_mask_class(ee, year: int, region) -> list[dict]:
    bins = np.linspace(-0.2, 1.0, 25)
    centers = (bins[:-1] + bins[1:]) / 2

    def classes(image):
        state = image.select("state_1km")
        red, nir = image.select("sur_refl_b01"), image.select("sur_refl_b02")
        valid = red.gte(-100).And(nir.gte(-100)).And(red.lte(16000)).And(nir.lte(16000))
        ndvi = nir.subtract(red).divide(nir.add(red))
        clear = state.bitwiseAnd(0b11).eq(0)
        no_shadow_internal = state.bitwiseAnd(1 << 2).eq(0).And(state.bitwiseAnd(1 << 10).eq(0))
        not_adjacent = state.bitwiseAnd(1 << 13).eq(0)
        masks = {
            "clear_strict": clear.And(no_shadow_internal).And(not_adjacent),
            "adjacent_only": clear.And(no_shadow_internal).And(not_adjacent.Not()),
            "shadow_or_internal_cloud": clear.And(no_shadow_internal.Not()),
            "cloudy_mod35": clear.Not(),
        }
        return ee.Image.cat([ndvi.updateMask(valid.And(m)).rename(k) for k, m in masks.items()])

    collection = (
        ee.ImageCollection(MODIS[0])
        .merge(ee.ImageCollection(MODIS[1]))
        .filterDate(f"{year}-01-01", f"{year}-06-01")
        .filterBounds(region)
        .map(classes)
    )
    histograms = collection.map(
        lambda i: ee.Feature(None, i.reduceRegion(ee.Reducer.fixedHistogram(-0.2, 1.0, 24), region, 500))
    ).getInfo()["features"]
    rows = []
    for key, label in QUALITY_CLASSES.items():
        counts = np.zeros(24)
        for feature in histograms:
            if histogram := feature["properties"].get(key):
                counts += np.asarray(histogram)[:, 1]
        cdf = np.cumsum(counts) / counts.sum()
        rows.append(
            {
                "year": year,
                "class": label,
                "pixel_days": int(counts.sum()),
                "ndvi_median": float(centers[np.searchsorted(cdf, 0.5)]),
                "share_ndvi_below_0.2": float(counts[centers < 0.2].sum() / counts.sum()),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--years", default="2024,2025,2026")
    args = parser.parse_args()
    years = [int(year) for year in args.years.split(",")]

    settings = Settings()
    ee = initialize_earth_engine(settings.gee_project, None)
    cells = cells_feature_collection(ee, DataLake(args.data_root))

    coverage, matrices = [], {}
    for option, label in OPTIONS.items():
        for year in years:
            clear = matrices[(option, year)] = clear_matrix(ee, option, year, cells)
            coverage.append(
                {
                    "option": label,
                    "year": year,
                    "cell_days": clear.mean(),
                    "cell_weeks_ge1": weekly_coverage(clear, year, 1),
                    "cell_weeks_ge2": weekly_coverage(clear, year, 2),
                }
            )
            print(coverage[-1], flush=True)
    for year in years:
        either = matrices[("modis_standard", year)] | matrices[("s2_cloud_score_plus", year)]
        coverage.append(
            {
                "option": "MODIS `standard` + Sentinel-2 (cualquiera de los dos)",
                "year": year,
                "cell_days": either.mean(),
                "cell_weeks_ge1": weekly_coverage(either, year, 1),
                "cell_weeks_ge2": weekly_coverage(either, year, 2),
            }
        )
    coverage = pd.DataFrame(coverage)
    quality = pd.DataFrame(
        [row for year in years for row in ndvi_by_mask_class(ee, year, cells.geometry().bounds())]
    )

    out = args.output_dir / "qc"
    out.mkdir(parents=True, exist_ok=True)
    coverage.to_csv(out / "gee_cloud_mask_coverage.csv", index=False)
    quality.to_csv(out / "gee_cloud_mask_ndvi_quality.csv", index=False)

    def pct(value: float) -> str:
        return f"{value:.1%}"

    table = coverage.pivot(index="option", columns="year", values="cell_weeks_ge1").reindex(
        [*OPTIONS.values(), "MODIS `standard` + Sentinel-2 (cualquiera de los dos)"]
    )
    lines = [
        "# Diagnóstico de máscara de nubes para NDVI/NDWI (GEE, Cali, enero a mayo)",
        "",
        "Generado por `examples/cali_2026/gee_cloud_mask_diagnostic.py` sobre las 160 celdas de 1 km.",
        "",
        "## Celdas-semana epidemiológica con al menos 1 día despejado",
        "",
        "| Opción | " + " | ".join(str(y) for y in years) + " |",
        "|---|" + "---|" * len(years),
        *[f"| {name} | " + " | ".join(pct(v) for v in row) + " |" for name, row in table.iterrows()],
        "",
        "Detalle por celda-día y con ≥2 días: `gee_cloud_mask_coverage.csv`.",
        "",
        "## NDVI de los píxeles-día MODIS que agrega cada nivel de máscara",
        "",
        "Las nubes acercan el NDVI a 0: un nivel que agrega muchos valores bajos está contaminado.",
        "",
        "| Año | Clase | Píxeles-día | NDVI mediano | NDVI < 0.2 |",
        "|---|---|---|---|---|",
        *[
            f"| {r.year} | {r['class']} | {r.pixel_days:,} | {r.ndvi_median:.2f} | {pct(r['share_ndvi_below_0.2'])} |"
            for _, r in quality.iterrows()
        ],
        "",
        "**Decisión:** `cloud_mask: standard` por defecto (se conservan los píxeles solo adyacentes a nube, "
        "casi limpios; se quitan sombra y nube interna, contaminados) y Sentinel-2 como segundo producto. "
        "El código original no enmascaraba: sin huecos, pero la mayoría de sus valores eran nubes.",
    ]
    (out / "gee_cloud_mask_diagnostic.md").write_text("\n".join(lines) + "\n")
    print(out / "gee_cloud_mask_diagnostic.md")


if __name__ == "__main__":
    main()
