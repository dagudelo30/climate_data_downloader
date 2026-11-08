"""New daily sources, nested grids, static urban layers and operational availability."""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pytest

from climate_pipeline.config import load_pipeline_config
from climate_pipeline.domain.dates import EpiWeek, date_range
from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.domain.grid import GridSpec, Nesting, block_mean, nest
from climate_pipeline.extractors import EXTRACTORS
from climate_pipeline.extractors.agera5 import Agera5Extractor
from climate_pipeline.extractors.gee_datasets import GEE_SOURCE_EXTRACTORS, Cadence, period_window
from climate_pipeline.pipeline import static as static_module
from climate_pipeline.pipeline.availability import last_complete_epiweek
from climate_pipeline.pipeline.gold import plan_nested_grids
from climate_pipeline.pipeline.orchestrator import NamedGrid, Pipeline, RunRequest, build_variable_catalog
from climate_pipeline.pipeline.urban import osm_layers
from climate_pipeline.storage.gold import GoldTable, OutputFormat, open_gold_store
from climate_pipeline.storage.raster import load_layer


# --- catalog / configuration ---------------------------------------------------------------
def test_every_configured_source_has_an_extractor_and_variables() -> None:
    config = load_pipeline_config(None)
    catalog = build_variable_catalog(config)
    for source in config.sources:
        assert source in EXTRACTORS, source
        specs = [spec for spec in catalog.all() if spec.source == source]
        assert specs, source
        assert all(spec.gold_column.startswith(f"{source}_") for spec in specs)
    for source in ("era5land", "imerg", "gsmap", "modis", "viirs", "smap", "landsat", "goes", "sentinel1"):
        assert source in config.sources


def test_period_window_daily_and_subdaily_with_local_offset() -> None:
    daily = period_window(Cadence.DAILY, date(2026, 11, 1))
    assert (daily.start, daily.end) == (datetime(2026, 11, 1), datetime(2026, 11, 2))
    colombia = period_window(Cadence.SUBDAILY, date(2026, 11, 1), utc_offset_hours=-5)
    assert (colombia.start, colombia.end) == (datetime(2026, 11, 1, 5), datetime(2026, 11, 2, 5))


def test_gee_source_tasks_are_one_per_day_and_dataset(lake, aoi, settings) -> None:
    config = load_pipeline_config(None)
    catalog = build_variable_catalog(config)
    specs = catalog.for_source("modis")
    extractor = GEE_SOURCE_EXTRACTORS["modis"](lake, aoi, config.source("modis"), settings, specs)
    days = [date(2026, 11, 1), date(2026, 11, 2)]
    tasks = extractor._tasks({"lst_day_terra": days, "lst_night_terra": days, "cloud_frac": days[:1]})
    assert sorted((task.dataset.key, task.day) for task in tasks) == [
        ("mod09ga_cloud", days[0]),
        ("mod11a1", days[0]),
        ("mod11a1", days[1]),
    ]
    assert all(
        task.names == ("lst_day_terra", "lst_night_terra") for task in tasks if task.dataset.key == "mod11a1"
    )


# --- nested grids --------------------------------------------------------------------------
def test_nest_and_block_mean(aoi) -> None:
    fine = GridSpec.from_resolution("EPSG:9377", 100, aoi)
    coarse = GridSpec.from_resolution("EPSG:9377", 1000, aoi)
    template = GridSpec.from_bounds(fine.crs, 100, coarse.bounds)
    nesting = nest(template, coarse)
    assert nesting == Nesting(10, 0, 0)
    values = np.arange(template.height * template.width, dtype="float32").reshape(template.shape)
    values[:10, :10] = np.nan
    values[0, 0] = 5.0
    result = block_mean(values, nesting, coarse.shape)
    assert result[0, 0] == pytest.approx(5.0)  # only one valid fine cell in the block
    expected = values[10:20, 20:30].mean()
    assert result[1, 2] == pytest.approx(expected)


def test_nest_rejects_grids_that_do_not_nest(aoi) -> None:
    fine = GridSpec.from_resolution("EPSG:9377", 100, aoi)
    with pytest.raises(ConfigurationError, match="not an integer"):
        nest(fine, GridSpec.from_resolution("EPSG:9377", 250, aoi))
    with pytest.raises(ConfigurationError, match="CRS"):
        nest(fine, GridSpec.from_resolution("EPSG:4326", 0.01, aoi))
    with pytest.raises(ConfigurationError, match="beyond"):
        nest(fine, GridSpec.from_resolution("EPSG:9377", 5000, aoi))  # fine not extended


def test_plan_nested_grids_orders_and_extends_the_template(aoi) -> None:
    grids = plan_nested_grids(
        [("5km", "EPSG:9377@5000"), ("100m", "EPSG:9377@100"), ("1km", "EPSG:9377@1000")], aoi
    )
    assert [grid.name for grid in grids] == ["100m", "1km", "5km"]
    assert grids[0].nesting is None
    assert grids[0].grid.bounds == grids[2].grid.bounds  # template covers the coarsest extent
    assert [grid.nesting.factor for grid in grids[1:]] == [10, 50]
    with pytest.raises(ConfigurationError, match="CRS>@<resolution"):
        plan_nested_grids([("a", "EPSG:9377@100"), ("b", "template.tif")], aoi)


START, END = date(2026, 1, 4), date(2026, 1, 10)


def test_nested_gold_coarse_cells_are_block_means_of_the_template(
    settings, pipeline_config, lake, fake_cds, chirps_mirror, aoi_file
) -> None:
    for day in date_range(START, END):
        chirps_mirror["publish"](day, "final")
    pipeline_config.sources["agera5"].variables = ["temp_mean_2m", "precipitation_flux"]

    def agera5_factory(*args, **kwargs):
        return Agera5Extractor(*args, client_factory=lambda: fake_cds, **kwargs)

    pipeline = Pipeline(settings, pipeline_config, lake=lake, extractor_factories={"agera5": agera5_factory})
    grids = [
        NamedGrid("1km", "EPSG:9377@1000"),
        NamedGrid("500m", "EPSG:9377@500"),
        NamedGrid("5km", "EPSG:9377@5000"),
    ]
    report = pipeline.run(
        RunRequest(
            aoi_path=aoi_file,
            sources=["chirps", "agera5"],
            start_date=START,
            end_date=END,
            target_grids=grids,
            nested=True,
            today=date(2026, 10, 27),
        )
    )
    assert report.ok, report.to_dict()
    assert [gold.grid_name for gold in report.gold] == ["500m", "1km", "5km"]
    assert all(gold.weeks_built == ["2026-W01"] for gold in report.gold)

    fine_store = open_gold_store(lake.gold_layout("500m"), OutputFormat.PARQUET)
    coarse_store = open_gold_store(lake.gold_layout("1km"), OutputFormat.PARQUET)
    fine_grid = GridSpec.load(lake.gold_layout("500m").grid_path)
    coarse_grid = GridSpec.load(lake.gold_layout("1km").grid_path)
    nesting = nest(fine_grid, coarse_grid)
    for column in ("chirps_precip", "agera5_temp_mean"):
        fine = load_layer(fine_store, fine_grid, column, day=START)
        coarse = load_layer(coarse_store, coarse_grid, column, day=START)
        k = nesting.factor
        window = fine[nesting.row_offset :, nesting.col_offset :][
            : coarse_grid.height * k, : coarse_grid.width * k
        ]
        complete = np.isfinite(window.reshape(coarse_grid.height, k, coarse_grid.width, k)).all(axis=(1, 3))
        assert complete.sum() > 5  # interior 1 km cells whose four 500 m cells are all in the AOI
        expected = block_mean(fine, nesting, coarse_grid.shape)
        np.testing.assert_allclose(coarse[complete], expected[complete], rtol=1e-5)

    weekly = coarse_store.query(f"SELECT * FROM {GoldTable.EPIWEEKLY}")
    assert weekly["chirps_precip"].notna().all()


def test_static_layers_on_nested_grids_merge_existing_columns(lake, aoi, monkeypatch) -> None:
    fine = GridSpec.from_bounds("EPSG:9377", 100, GridSpec.from_resolution("EPSG:9377", 1000, aoi).bounds)
    coarse = GridSpec.from_resolution("EPSG:9377", 1000, aoi)
    fine.save(lake.gold_layout("100m").grid_path)
    coarse.save(lake.gold_layout("1km").grid_path)
    rng = np.random.default_rng(0)
    layer = rng.random(fine.shape).astype("float32")

    def fake_layers(grid, layers, year, settings=None, ee=None, osm_ways=None):
        assert grid == fine and year == 2025
        return {f"urban_{name}": layer for name in layers}

    monkeypatch.setattr(static_module, "compute_static_layers", fake_layers)
    static_module.build_static_covariates(
        lake, ["1km", "100m"], aoi, OutputFormat.PARQUET, layers=["a"], year=2025, nested=True
    )
    frames = static_module.build_static_covariates(
        lake, ["1km", "100m"], aoi, OutputFormat.PARQUET, layers=["b"], year=2025, nested=True
    )
    coarse_frame = frames["1km"].set_index("cell_id")
    assert {"urban_a", "urban_b"} <= set(coarse_frame.columns)  # the first run's column is kept
    expected = block_mean(layer, nest(fine, coarse), coarse.shape).reshape(-1)
    np.testing.assert_allclose(coarse_frame["urban_b"], expected[coarse_frame.index], rtol=1e-6)


# --- source catalog ----------------------------------------------------------------------
def test_new_variables_cover_every_target_family() -> None:
    columns = {
        spec.gold_column for spec in build_variable_catalog(load_pipeline_config(None)).all() if spec.default
    }
    expected = {
        # precipitation and its satellite precursors
        "imerg_precip",
        "goes_cold_cloud_frac_235",
        "goes_bt_c08",
        "goes_btd_c13_c15",
        "modis_column_wv",
        "agera5_precip_hours_frac",
        # temperature
        "agera5_temp_day_max",
        "agera5_temp_night_min",
        "era5land_skin_temp",
        "viirs_ntl",
        "modis_lst_night_aqua",
        # humidity, wind, radiation
        "agera5_rh_max",
        "agera5_rh_min",
        "agera5_vpd_tmax",
        "era5land_u_wind_10m",
        "era5land_thermal_radiation_down",
        "goes_refl_c02_day",
        "modis_aod_055",
        "agera5_et0",
        # surface
        "sentinel1_vh_vv_db",
        "gee_s2_ndre",
        "gee_evi",
    }
    assert expected <= columns


def test_no_source_is_coarser_than_agera5() -> None:
    from climate_pipeline.pipeline.availability import NATIVE_RESOLUTION

    config = load_pipeline_config(None)
    for source in config.sources:
        assert NATIVE_RESOLUTION[source][0] <= 11_100, source


def test_goes_daily_reducers() -> None:
    from climate_pipeline.extractors.gee_datasets import GOES_EAST

    reducers = {variable.name: variable.reducer for variable in GOES_EAST.variables}
    assert reducers["bt_c13_min"] == "min" and reducers["bt_c13"] == "mean"
    assert GOES_EAST.min_images == 120


# --- OpenStreetMap ---------------------------------------------------------------------------
def test_osm_road_and_intersection_density() -> None:
    from pyproj import Transformer

    grid = GridSpec("EPSG:9377", (100.0, 0.0, 4_600_000.0, 0.0, -100.0, 1_950_000.0), 20, 20)
    to_wgs = Transformer.from_crs("EPSG:9377", "EPSG:4326", always_xy=True)

    def point(x: float, y: float) -> dict[str, float]:
        lon, lat = to_wgs.transform(x, y)
        return {"lon": lon, "lat": lat}

    y = 1_950_000.0 - 550.0  # middle of row 5
    centre = (4_600_000.0 + 1050.0, y)
    ways = [
        {
            "tags": {"highway": "primary"},
            "nodes": [1, 2],
            "geometry": [point(4_600_050.0, y), point(*centre)],
        },
        {
            "tags": {"highway": "residential"},
            "nodes": [2, 3],
            "geometry": [point(*centre), point(4_601_950.0, y)],
        },
        {
            "tags": {"highway": "residential"},
            "nodes": [2, 4],
            "geometry": [point(*centre), point(centre[0], y - 800)],
        },
        {
            "tags": {"highway": "footway"},
            "nodes": [5, 6],
            "geometry": [point(4_600_050.0, y), point(4_600_050.0, y - 900)],
        },
    ]
    layers = osm_layers(ways, grid)
    road = layers["urban_osm_road_density_km_km2"]
    assert road[5, 1:10] == pytest.approx(np.full(9, 10.0), rel=0.01)  # 100 m of road per 0.01 km² cell
    assert layers["urban_osm_major_road_density_km_km2"][5, 12] == 0.0
    assert road[0, 0] == 0.0  # the footway is not a drivable road
    intersections = layers["urban_osm_intersection_density_km2"]
    assert intersections.sum() == pytest.approx(100.0)  # one junction in a 0.01 km² cell
    assert intersections[5, 10] == pytest.approx(100.0)


# --- availability --------------------------------------------------------------------------
def test_last_complete_epiweek() -> None:
    assert last_complete_epiweek(date(2026, 10, 31)) == EpiWeek.from_date(date(2026, 10, 25))  # Saturday
    assert last_complete_epiweek(date(2026, 11, 6)) == EpiWeek.from_date(date(2026, 10, 25))  # Friday
    assert last_complete_epiweek(date(2026, 11, 7)).label == "2026-W44"


# --- redundancy ----------------------------------------------------------------------------
def test_redundancy_prefers_latency_then_resolution_then_sense() -> None:
    from climate_pipeline.domain.redundancy import Candidate, Quantity, decide

    quantities = (
        Quantity("q1", "", "daily", (Candidate("a_x", 1000), Candidate("b_x", 500))),
        Quantity("q2", "", "daily", (Candidate("a_y", 1000, 0), Candidate("d_y", 500, 1))),
        Quantity("q3", "", "daily", (Candidate("a_z", 1000, 0), Candidate("d_z", 1000, 1))),
    )
    latency = {"a": 8, "b": 30, "d": 7}.get
    decisions = {
        d.quantity: d for d in decide(["a_x", "b_x", "a_y", "d_y", "a_z", "d_z"], latency, quantities, ())
    }
    assert decisions["q1"].kept == "a_x" and decisions["q1"].rule == "latencia"  # recent beats finer
    assert decisions["q2"].kept == "d_y" and decisions["q2"].rule == "resolución"  # 8 d vs 7 d tie → finer
    assert decisions["q3"].kept == "a_z" and decisions["q3"].rule == "criterio"


def test_default_catalog_has_one_column_per_quantity() -> None:
    from climate_pipeline.domain.redundancy import QUANTITIES

    catalog = build_variable_catalog(load_pipeline_config(None))
    defaults = {spec.gold_column for spec in catalog.all() if spec.default}
    for quantity in QUANTITIES:
        present = [candidate.column for candidate in quantity.candidates if candidate.column in defaults]
        assert len(present) <= 1, (quantity.name, present)
    assert "imerg_precip" in defaults and "chirps_precip" not in defaults  # 2 d vs 7 d latency
    assert "agera5_temp_max" in defaults and "era5land_temp_max" not in defaults
    assert "modis_lst_day_aqua" in defaults and "viirs_lst_day" not in defaults
    # Different temporal sampling is complementary, not redundant.
    assert {"gee_ndvi", "gee_s2_ndvi", "landsat_lst", "modis_lst_day_terra"} <= defaults
    # Explicit requests and deduplicate=false keep access to every column.
    assert catalog.get("chirps", "precipitation_flux").gold_column == "chirps_precip"
    config = load_pipeline_config(None)
    config.deduplicate = False
    assert build_variable_catalog(config).by_gold_column("chirps_precip").default


def test_parent_cell_ids_join_nested_grids(aoi) -> None:
    from climate_pipeline.domain.grid import parent_cell_ids

    coarse = GridSpec.from_resolution("EPSG:9377", 1000, aoi)
    fine = GridSpec.from_bounds("EPSG:9377", 100, coarse.bounds)
    ids = np.arange(fine.width * fine.height)
    parents = parent_cell_ids(fine, coarse, ids)
    assert (parents >= 0).all()
    assert np.bincount(parents).tolist() == [100] * (coarse.width * coarse.height)  # 10×10 children each
    assert parents[0] == 0 and parents[fine.width * 10 + 10] == coarse.width + 1


def test_static_layers_independent_grids_use_each_grid(lake, aoi, monkeypatch) -> None:
    grids = {
        "500m": GridSpec.from_resolution("EPSG:9377", 500, aoi),
        "5km": GridSpec.from_resolution("EPSG:9377", 5000, aoi),
    }
    for name, grid in grids.items():
        grid.save(lake.gold_layout(name).grid_path)
    seen = []

    def fake_layers(grid, layers, year, settings=None, ee=None, osm_ways=None):
        seen.append(grid.resolution[0])
        return {"urban_x": np.full(grid.shape, grid.resolution[0], dtype="float32")}

    monkeypatch.setattr(static_module, "compute_static_layers", fake_layers)
    frames = static_module.build_static_covariates(
        lake, list(grids), aoi, OutputFormat.PARQUET, layers=["terrain"]
    )
    assert sorted(seen) == [500.0, 5000.0]  # each grid computed at its own resolution
    assert (frames["5km"]["urban_x"] == 5000.0).all()
