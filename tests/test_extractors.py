from __future__ import annotations

from datetime import date

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.io import MemoryFile

from climate_pipeline.config import load_pipeline_config
from climate_pipeline.domain.artifacts import BronzeArtifact, MissingData, ProductStatus
from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.domain.variables import AGERA5_VARIABLES, CHIRPS_VARIABLES
from climate_pipeline.extractors.agera5 import (
    Agera5Extractor,
    MonthlyRequest,
    build_request,
    is_unavailable_error,
)
from climate_pipeline.extractors.base import ExtractionFailure
from climate_pipeline.extractors.chirps import ChirpsExtractor, ChirpsOptions, gdal_path
from climate_pipeline.extractors.gee import (
    gee_variable_specs,
    geotiff_to_arrays,
    resolve_mask,
    resolve_product,
)
from climate_pipeline.extractors.mswep import mswep_file_name
from climate_pipeline.storage.netcdf import read_netcdf

SPECS = {spec.name: spec for spec in AGERA5_VARIABLES}


def _chirps(lake, aoi, settings, pipeline_config):
    return ChirpsExtractor(lake, aoi, pipeline_config.source("chirps"), settings, list(CHIRPS_VARIABLES))


def test_chirps_reads_only_the_aoi_window_and_masks_nodata(
    lake, aoi, settings, pipeline_config, chirps_mirror
):
    day = date(2026, 1, 2)
    chirps_mirror["publish"](day, "final")
    extractor = _chirps(lake, aoi, settings, pipeline_config)
    [outcome] = list(extractor.fetch({"precipitation_flux": [day]}))
    assert isinstance(outcome, BronzeArtifact)
    assert outcome.status is ProductStatus.FINAL
    assert outcome.path == lake.bronze_path("chirps", day)
    dataset = read_netcdf(outcome.path)
    array = dataset[next(iter(dataset.data_vars))]
    # Window covers the AOI (+2 cells margin), far smaller than the 20x20 source file.
    assert array.sizes["x"] < 20 and array.sizes["y"] < 20
    assert float(array.x.min()) < aoi.bounds[0] < aoi.bounds[2] < float(array.x.max())
    assert dataset.attrs.get("product_status") or array.attrs["product_status"] == "final"
    assert np.nanmin(array.values) >= 0  # -9999 never leaks


def test_chirps_falls_back_to_prelim_then_reports_missing(
    lake, aoi, settings, pipeline_config, chirps_mirror
):
    chirps_mirror["publish"](date(2026, 1, 3), "prelim")
    extractor = _chirps(lake, aoi, settings, pipeline_config)
    outcomes = {
        o.day: o for o in extractor.fetch({"precipitation_flux": [date(2026, 1, 3), date(2026, 1, 4)]})
    }
    assert outcomes[date(2026, 1, 3)].status is ProductStatus.PRELIM
    assert isinstance(outcomes[date(2026, 1, 4)], MissingData)


def test_chirps_default_urls() -> None:
    options = ChirpsOptions()
    (_, final), (_, prelim) = options.templates()
    url = final.format(base_url=options.base_url, disaggregation="rnl", year=2026, month=3, day=5)
    assert url.endswith("/CHIRPS/v3.0/daily/final/rnl/2026/chirps-v3.0.rnl.2026.03.05.tif")
    assert "prelim/sat/" in prelim
    v2_final = ChirpsOptions(version="v2").templates()[0][1]
    assert v2_final.endswith(".tif.gz")
    assert gdal_path("https://x/a.tif.gz") == "/vsigzip//vsicurl/https://x/a.tif.gz"
    assert gdal_path("/local/a.tif") == "/local/a.tif"


def test_agera5_request_body(aoi) -> None:
    task = MonthlyRequest(SPECS["temp_max_2m"], 2026, 2, (date(2026, 2, 27), date(2026, 2, 28)))
    body = build_request(task, aoi.cds_area(0.2), "2_0")
    assert body["variable"] == "2m_temperature"
    assert body["statistic"] == ["24_hour_maximum"]
    assert body["day"] == ["27", "28"]  # only the pending, valid days
    north, west, south, east = body["area"]
    assert north > south and east > west
    rh = build_request(
        MonthlyRequest(SPECS["relative_humidity_2m_06h"], 2026, 1, (date(2026, 1, 1),)), [1, 0, 0, 1], "2_0"
    )
    assert rh["time"] == ["06_00"] and "statistic" not in rh


def test_agera5_writes_one_bronze_file_per_day(lake, aoi, settings, fake_cds):
    config = load_pipeline_config(None).source("agera5")
    extractor = Agera5Extractor(
        lake,
        aoi,
        config,
        settings,
        [SPECS["temp_mean_2m"], SPECS["solar_radiation_flux"]],
        client_factory=lambda: fake_cds,
    )
    days = [date(2026, 1, 30), date(2026, 1, 31), date(2026, 2, 1)]
    outcomes = list(extractor.fetch({"temp_mean_2m": days, "solar_radiation_flux": days[:1]}))
    artifacts = [o for o in outcomes if isinstance(o, BronzeArtifact)]
    assert len(artifacts) == 4
    assert len(fake_cds.requests) == 3  # temp: Jan + Feb ; radiation: Jan
    path = lake.bronze_path("agera5", date(2026, 1, 31), "temp_mean_2m")
    assert path.exists()
    dataset = read_netcdf(path)
    assert "Temperature_Air_2m_Mean_24h" in dataset.data_vars  # raw name and units kept in Bronze
    assert dataset["Temperature_Air_2m_Mean_24h"].attrs["units"] == "K"


def test_agera5_unavailable_dates_become_missing(lake, aoi, settings):
    from tests.conftest import FakeCdsClient

    client = FakeCdsClient(unavailable_after=date(2026, 10, 18))
    config = load_pipeline_config(None).source("agera5")
    extractor = Agera5Extractor(
        lake, aoi, config, settings, [SPECS["temp_mean_2m"]], client_factory=lambda: client
    )
    outcomes = list(extractor.fetch({"temp_mean_2m": [date(2026, 10, 18), date(2026, 10, 19)]}))
    statuses = {o.day: type(o) for o in outcomes}
    assert statuses == {date(2026, 10, 18): BronzeArtifact, date(2026, 10, 19): MissingData}


def test_agera5_unexpected_errors_are_isolated_failures(lake, aoi, settings):
    class BrokenClient:
        def retrieve(self, *args, **kwargs):
            raise PermissionError("401 Unauthorized: invalid API key")

    config = load_pipeline_config(None).source("agera5")
    extractor = Agera5Extractor(
        lake, aoi, config, settings, [SPECS["temp_mean_2m"]], client_factory=BrokenClient
    )
    outcomes = list(extractor.fetch({"temp_mean_2m": [date(2026, 1, 1), date(2026, 1, 2)]}))
    assert all(isinstance(o, ExtractionFailure) for o in outcomes) and len(outcomes) == 2
    assert "401" in outcomes[0].error


def test_unavailable_error_classification() -> None:
    assert is_unavailable_error(RuntimeError("There is no data matching your request"))
    assert not is_unavailable_error(RuntimeError("401 Unauthorized"))


def test_gee_specs_and_geotiff_parsing() -> None:
    specs = gee_variable_specs(["modis_terra_aqua_indices"])
    assert [spec.gold_column for spec in specs] == ["gee_ndvi", "gee_ndwi", "gee_evi", "gee_mndwi"]
    assert all((spec.bounds.minimum, spec.bounds.maximum) == (-1, 1) for spec in specs)

    data = np.array([[[0.5, -9999.0], [0.7, 0.1]], [[0.2, 0.3], [-9999.0, -0.4]]], dtype="float32")
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=2,
            height=2,
            count=2,
            dtype="float32",
            crs="EPSG:4326",
            transform=Affine(0.005, 0, -76.6, 0, -0.005, 3.5),
        ) as dataset:
            dataset.write(data)
        payload = memory.read()
    arrays = geotiff_to_arrays(payload, ["ndvi", "ndwi"], nodata=-9999.0)
    assert np.isnan(arrays["ndvi"].values[0, 1]) and np.isnan(arrays["ndwi"].values[1, 0])
    assert arrays["ndvi"].rio.crs.to_epsg() == 4326
    with pytest.raises(ValueError, match="bands"):
        geotiff_to_arrays(payload, ["ndvi"], nodata=-9999.0)


def test_mswep_file_name() -> None:
    assert mswep_file_name(date(2026, 1, 1)) == "2026001.nc"
    assert mswep_file_name(date(2024, 12, 31)) == "2024366.nc"


def test_rasterio_import_is_available() -> None:
    assert rasterio.__version__


class _BitImage:
    """Numpy stand-in for the few ee.Image operations the cloud masks use."""

    def __init__(self, values: np.ndarray) -> None:
        self.values = np.asarray(values)

    def select(self, _band: str) -> _BitImage:
        return self

    def bitwiseAnd(self, bits: int) -> _BitImage:  # noqa: N802 (Earth Engine API name)
        return _BitImage(self.values & bits)

    def eq(self, value: int) -> _BitImage:
        return _BitImage(self.values == value)

    def gte(self, value: float) -> _BitImage:
        return _BitImage(self.values >= value)

    def And(self, other: _BitImage) -> _BitImage:  # noqa: N802
        return _BitImage(self.values & other.values)


def test_modis_cloud_masks_keep_cloud_adjacent_pixels_only_in_standard() -> None:
    clear, cloudy, mixed, shadow, internal_cloud, adjacent = 0, 0b01, 0b10, 1 << 2, 1 << 10, 1 << 13
    state = _BitImage(np.array([clear, cloudy, mixed, shadow, internal_cloud, adjacent, adjacent | shadow]))
    product = resolve_product("modis_terra_aqua_indices")
    standard = resolve_mask(product, "standard")(state).values
    strict = resolve_mask(product, "strict")(state).values
    assert standard.tolist() == [True, False, False, False, False, True, False]
    assert strict.tolist() == [True, False, False, False, False, False, False]


def test_cloud_score_plus_mask_and_unknown_variant() -> None:
    product = resolve_product("sentinel2_indices")
    scores = _BitImage(np.array([0.2, 0.59, 0.6, 0.85]))
    assert resolve_mask(product, "standard")(scores).values.tolist() == [False, False, True, True]
    assert resolve_mask(product, "strict")(scores).values.tolist() == [False, False, False, True]
    with pytest.raises(ConfigurationError, match="cloud_mask"):
        resolve_mask(product, "none")


def test_gee_products_have_distinct_columns() -> None:
    specs = gee_variable_specs(["modis_terra_aqua_indices", "sentinel2_indices"])
    assert [spec.gold_column for spec in specs] == [
        "gee_ndvi",
        "gee_ndwi",
        "gee_evi",
        "gee_mndwi",
        "gee_s2_ndvi",
        "gee_s2_ndwi",
        "gee_s2_evi",
        "gee_s2_mndwi",
        "gee_s2_ndre",
        "gee_s2_bsi",
    ]
    assert all(spec.min_valid_days == 1 for spec in specs)
    with pytest.raises(ConfigurationError, match="same columns"):
        gee_variable_specs(["modis_terra_aqua_indices", "modis_mod09ga_indices"])
