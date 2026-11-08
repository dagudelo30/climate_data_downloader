"""Daily climate and satellite sources read from Google Earth Engine image collections.

Each source (``era5land``, ``imerg``, ``gsmap``, ``modis``, ``viirs``, ``smap``, ``landsat``)
is a list of :class:`GeeDataset`. A dataset declares its collections, its cadence and the
variables computed from each image. Unit conversion and per-variable quality masks run
server-side, so Bronze is already in the target units.

Every source is **daily** and **operational** (published within days; checked against the
live catalog on 2026-11-08, see ``climate-pipeline availability``):

=============  ===============================================================
``daily``      the image(s) of day ``d`` (averaged when two satellites overpass)
``subdaily``   images in ``[d, d+1)`` (shifted by the ``utc_offset_hours`` option),
               averaged; the day stays *missing* until ``min_images`` exist, so an
               incomplete last day is never written as if it were complete
=============  ===============================================================

Spatial handling: a dataset is downloaded on its **native grid** (CRS and transform of an
image over the AOI), so Bronze holds the provider pixels unchanged and every resampling
decision is left to Gold. SMAP's EASE grid is irregular in latitude and is downloaded in
EPSG:4326 at ``scale_m`` instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

import numpy as np

from climate_pipeline.domain.artifacts import MissingData, ProductStatus
from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.domain.variables import (
    QualityBounds,
    ResamplingMethod,
    VariableSpec,
    WeeklyAggregation,
)
from climate_pipeline.extractors.base import (
    BaseExtractor,
    ExtractionFailure,
    ExtractionOutcome,
    PendingBronze,
    register_extractor,
)
from climate_pipeline.extractors.gee import GEE_NODATA, geotiff_to_arrays, initialize_earth_engine

logger = logging.getLogger(__name__)

METERS_PER_DEGREE = 111_320.0

Compute = Callable[[Any], Any]  # ee.Image -> single-band ee.Image in target units
Mask = Callable[[Any], Any]  # ee.Image -> boolean ee.Image (1 = keep)


class Cadence(StrEnum):
    SUBDAILY = "subdaily"
    DAILY = "daily"


# --- physical bounds (target units) ------------------------------------------------------
TEMPERATURE = QualityBounds(-90.0, 60.0)
SURFACE_TEMPERATURE = QualityBounds(-90.0, 90.0)  # land surface temperature exceeds air temperature
DEWPOINT = QualityBounds(-90.0, 40.0)
RELATIVE_HUMIDITY = QualityBounds(0.0, 100.0, tolerance=2.0)
# Reanalysis accumulations carry tiny negative round-off values (e.g. -1e-6 m): clip them to 0.
PRECIPITATION = QualityBounds(0.0, 2000.0, tolerance=0.5)
SOLAR_RADIATION = QualityBounds(0.0, 45.0)
WIND_SPEED = QualityBounds(0.0, 75.0)
PRESSURE = QualityBounds(300.0, 1100.0)
VOLUMETRIC_SOIL_WATER = QualityBounds(0.0, 1.0, tolerance=0.02)
EVAPORATION = QualityBounds(-20.0, 60.0)  # negative = condensation / dew
WATER_BALANCE = QualityBounds(-100.0, 2000.0)
FRACTION = QualityBounds(0.0, 1.0, tolerance=0.01)
NORMALIZED_INDEX = QualityBounds(-1.0, 1.0)
NIGHT_LIGHTS = QualityBounds(0.0, 100_000.0)
WIND_COMPONENT = QualityBounds(-75.0, 75.0)
TEMPERATURE_RANGE = QualityBounds(0.0, 40.0)
VAPOUR_PRESSURE_DEFICIT = QualityBounds(0.0, 150.0)
SPECIFIC_HUMIDITY = QualityBounds(0.0, 40.0)
HEAT_FLUX = QualityBounds(-20.0, 40.0)
NET_THERMAL = QualityBounds(-30.0, 10.0)
LONGWAVE_DOWN = QualityBounds(0.0, 60.0)
LEAF_AREA = QualityBounds(0.0, 15.0)
BRIGHTNESS_TEMPERATURE = QualityBounds(150.0, 350.0)
BRIGHTNESS_TEMPERATURE_DIFFERENCE = QualityBounds(-100.0, 100.0)
REFLECTANCE = QualityBounds(0.0, 1.5)
WATER_VAPOUR_COLUMN = QualityBounds(0.0, 10.0)
AEROSOL_OPTICAL_DEPTH = QualityBounds(0.0, 5.0)
BACKSCATTER_DB = QualityBounds(-50.0, 30.0)


@dataclass(frozen=True)
class DatasetVariable:
    name: str
    compute: Compute
    units: str
    bounds: QualityBounds
    weekly: WeeklyAggregation = WeeklyAggregation.MEAN
    resampling: ResamplingMethod = ResamplingMethod.BILINEAR
    min_valid_days: int = 4
    mask: Mask | None = None
    default: bool = True
    description: str = ""
    #: How the images of the day are combined: mean (default), min or max.
    reducer: str = "mean"


@dataclass(frozen=True)
class GeeDataset:
    key: str
    collections: tuple[str, ...]
    cadence: Cadence
    variables: tuple[DatasetVariable, ...]
    #: Images required for a complete day (sub-daily products).
    min_images: int = 1
    native_grid: bool = True
    scale_m: float | None = None
    #: Band whose grid is the dataset grid.
    grid_band: str | None = None
    #: Extra server-side filter of the collection, ``(ee, collection) -> collection``.
    prefilter: Callable[[Any, Any], Any] | None = None
    #: Image property telling provisional from final data, and the values meaning final.
    status_property: tuple[str, frozenset[str]] | None = None
    resolution_label: str = ""
    agency: str = ""
    #: The product is gap-filled, so an image without any value over the AOI means "not
    #: processed yet" (VNP46A2 publishes empty placeholders for the latest days).
    empty_is_missing: bool = False

    def __post_init__(self) -> None:
        if not self.native_grid and self.scale_m is None:
            raise ConfigurationError(f"{self.key}: scale_m is required when native_grid is False")


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime  # exclusive

    @property
    def label(self) -> str:
        return f"{self.start:%Y-%m-%dT%H:%M}..{self.end:%Y-%m-%dT%H:%M}"


def period_window(cadence: Cadence, day: date, utc_offset_hours: float = 0.0) -> Window:
    """Time window (UTC) whose images define the value of ``day``.

    ``utc_offset_hours`` only shifts sub-daily products: with ``-5`` (Colombia) the day
    ``d`` is aggregated from 05:00 UTC of ``d`` to 05:00 UTC of ``d+1``.
    """
    midnight = datetime(day.year, day.month, day.day)
    if cadence is Cadence.SUBDAILY:
        start = midnight - timedelta(hours=utc_offset_hours)
        return Window(start, start + timedelta(days=1))
    if cadence is Cadence.DAILY:
        return Window(midnight, midnight + timedelta(days=1))
    raise ConfigurationError(f"unknown cadence {cadence!r}")


# --- helpers to build server-side computations ------------------------------------------
def band(name: str, scale: float = 1.0, offset: float = 0.0) -> Compute:
    def compute(image: Any) -> Any:
        selected = image.select(name).toFloat()
        if scale != 1.0:
            selected = selected.multiply(scale)
        if offset != 0.0:
            selected = selected.add(offset)
        return selected

    return compute


def kelvin_to_celsius(name: str, scale: float = 1.0) -> Compute:
    return band(name, scale=scale, offset=-273.15)


def bits_clear(band_name: str, bits: Sequence[int]) -> Mask:
    def mask(image: Any) -> Any:
        result = None
        for bit in bits:
            clear = image.select(band_name).bitwiseAnd(1 << bit).eq(0)
            result = clear if result is None else result.And(clear)
        return result

    return mask


def modis_lst_quality(qc_band: str) -> Mask:
    """MOD11/MYD11 ``QC_Day``/``QC_Night``: keep good quality (bits 0-1 = 0), or "other
    quality" (= 1) with an LST error ≤ 2 K (bits 6-7 ≤ 1)."""

    def mask(image: Any) -> Any:
        qc = image.select(qc_band)
        mandatory = qc.bitwiseAnd(0b11)
        error = qc.rightShift(6).bitwiseAnd(0b11)
        return mandatory.eq(0).Or(mandatory.eq(1).And(error.lte(1)))

    return mask


#: Landsat C2 L2 ``QA_PIXEL``: fill (0), dilated cloud (1), cirrus (2), cloud (3), shadow (4).
landsat_clear = bits_clear("QA_PIXEL", (0, 1, 2, 3, 4))


def landsat_index(first: str, second: str) -> Compute:
    """(first − second) / (first + second) on Collection-2 surface reflectance."""

    def compute(image: Any) -> Any:
        a = image.select(first).multiply(0.0000275).add(-0.2)
        b = image.select(second).multiply(0.0000275).add(-0.2)
        return a.subtract(b).divide(a.add(b))

    return compute


def relative_humidity_from_dewpoint(temperature: str, dewpoint: str) -> Compute:
    """Magnus formula (Alduchov & Eskridge 1996) on daily-mean T and Td (Kelvin)."""

    def compute(image: Any) -> Any:
        t = image.select(temperature).subtract(273.15)
        td = image.select(dewpoint).subtract(273.15)
        saturation = t.multiply(17.625).divide(t.add(243.04)).exp()
        actual = td.multiply(17.625).divide(td.add(243.04)).exp()
        return actual.divide(saturation).multiply(100.0)

    return compute


def wind_speed(u: str, v: str) -> Compute:
    def compute(image: Any) -> Any:
        return image.select(u).pow(2).add(image.select(v).pow(2)).sqrt()

    return compute


def water_depth(name: str, sign: float = 1.0) -> Compute:
    """ERA5 accumulations in metres of water → mm (ECMWF fluxes are negative upwards)."""
    return band(name, scale=1000.0 * sign)


def era5_water_balance(image: Any) -> Any:
    precipitation = image.select("total_precipitation_sum").multiply(1000.0)
    potential = image.select("potential_evaporation_sum").multiply(-1000.0)
    return precipitation.subtract(potential)


def modis_cloud_fraction(image: Any) -> Any:
    """MOD09GA ``state_1km`` bits 0-1: 1 cloudy, 2 mixed → 1; 0 clear → 0; 3 (not set) masked."""
    state = image.select("state_1km").bitwiseAnd(0b11)
    return state.eq(1).Or(state.eq(2)).toFloat().updateMask(state.neq(3))


def saturation_vapour_pressure(celsius: Any) -> Any:
    """Magnus (Alduchov & Eskridge 1996), hPa, from an ee.Image in °C."""
    return celsius.multiply(17.625).divide(celsius.add(243.04)).exp().multiply(6.1094)


def _celsius(image: Any, band_name: str) -> Any:
    return image.select(band_name).subtract(273.15)


def era5_vpd(image: Any) -> Any:
    saturation = saturation_vapour_pressure(_celsius(image, "temperature_2m"))
    actual = saturation_vapour_pressure(_celsius(image, "dewpoint_temperature_2m"))
    return saturation.subtract(actual).max(0.0)


def era5_rh_from(temperature: str) -> Compute:
    """RH of the daily-mean dewpoint at Tmax (≈ RHmin) or Tmin (≈ RHmax), FAO-56 style."""

    def compute(image: Any) -> Any:
        saturation = saturation_vapour_pressure(_celsius(image, temperature))
        actual = saturation_vapour_pressure(_celsius(image, "dewpoint_temperature_2m"))
        return actual.divide(saturation).multiply(100.0).min(100.0)

    return compute


def era5_specific_humidity(image: Any) -> Any:
    """q = 622 e / (p − 0.378 e), g kg-1, with e from the dewpoint and p the surface pressure."""
    vapour = saturation_vapour_pressure(_celsius(image, "dewpoint_temperature_2m"))
    pressure = image.select("surface_pressure").multiply(0.01)
    return vapour.multiply(622.0).divide(pressure.subtract(vapour.multiply(0.378)))


def band_difference(first: str, second: str, scale: float = 1.0) -> Compute:
    def compute(image: Any) -> Any:
        return image.select(first).subtract(image.select(second)).multiply(scale)

    return compute


# --- GOES-East ABI (MCMIP: DN with per-image scale/offset properties) ----------------------
def goes_channel(channel: int) -> Compute:
    """Brightness temperature (K, IR channels) or reflectance factor (visible) of an ABI channel."""
    name = f"CMI_C{channel:02d}"

    def compute(image: Any) -> Any:
        import ee

        scale = ee.Number(image.get(f"{name}_scale"))
        offset = ee.Number(image.get(f"{name}_offset"))
        return image.select(name).toFloat().multiply(scale).add(offset)

    return compute


def goes_quality(*channels: int) -> Mask:
    """DQF 0 (good) or 1 (conditionally usable) on every channel used."""

    def mask(image: Any) -> Any:
        result = None
        for channel in channels:
            good = image.select(f"DQF_C{channel:02d}").lte(1)
            result = good if result is None else result.And(good)
        return result

    return mask


def goes_difference(first: int, second: int) -> Compute:
    def compute(image: Any) -> Any:
        return goes_channel(first)(image).subtract(goes_channel(second)(image))

    return compute


def goes_cold_cloud(threshold_k: float) -> Compute:
    """1 when the 10.3 µm brightness temperature is below ``threshold_k`` (cold cloud tops):
    the daily mean is the cold-cloud-duration fraction behind TAMSAT and CHIRPS."""

    def compute(image: Any) -> Any:
        return goes_channel(13)(image).lt(threshold_k).toFloat()

    return compute


def goes_daytime_reflectance(image: Any) -> Any:
    """0.64 µm reflectance only for 13-21 UTC slots (08-16 h in Colombia): cloud albedo."""
    import ee

    hour = ee.Number(ee.Date(image.get("system:time_start")).get("hour"))  # UTC
    daytime = ee.Image.constant(hour.gte(13).And(hour.lte(21)))
    return goes_channel(2)(image).updateMask(daytime)


def maiac_clear(image: Any) -> Any:
    """MCD19A2 ``AOD_QA`` bits 0-2 = 1: clear."""
    return image.select("AOD_QA").bitwiseAnd(0b111).eq(1)


def viirs_lst_quality(image: Any) -> Any:
    """VNP21 ``QC`` bits 0-1: 0 good, 1 other quality (2 cloud, 3 not produced)."""
    return image.select("QC").bitwiseAnd(0b11).lte(1)


def sentinel1_valid(image: Any) -> Any:
    """Drop near/far-range edges and noise-floor pixels."""
    angle = image.select("angle")
    return angle.gt(30.0).And(angle.lt(46.0)).And(image.select("VV").gt(-30.0))


def sentinel1_dual_pol(ee: Any, collection: Any) -> Any:
    return (
        collection.filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
    )


def _precip(name: str, compute: Compute, description: str) -> DatasetVariable:
    return DatasetVariable(
        name=name,
        compute=compute,
        units="mm d-1",
        bounds=PRECIPITATION,
        weekly=WeeklyAggregation.SUM,
        resampling=ResamplingMethod.AVERAGE,
        min_valid_days=7,
        description=description,
    )


def _daily_sum(
    name: str, compute: Compute, units: str, bounds: QualityBounds, description: str
) -> DatasetVariable:
    return DatasetVariable(
        name=name,
        compute=compute,
        units=units,
        bounds=bounds,
        weekly=WeeklyAggregation.SUM,
        resampling=ResamplingMethod.AVERAGE,
        min_valid_days=7,
        description=description,
    )


def _lst(name: str, value_band: str, qc_band: str, label: str) -> DatasetVariable:
    return DatasetVariable(
        name=name,
        compute=kelvin_to_celsius(value_band, scale=0.02),
        units="degC",
        bounds=SURFACE_TEMPERATURE,
        mask=modis_lst_quality(qc_band),
        min_valid_days=1,
        description=f"MODIS {label} land surface temperature, 1 km (QC: good or error <= 2 K)",
    )


def _var(
    name: str, compute: Compute, units: str, bounds: QualityBounds, description: str, **kwargs: Any
) -> DatasetVariable:
    return DatasetVariable(name, compute, units, bounds, description=description, **kwargs)


# --- dataset definitions -----------------------------------------------------------------
ERA5_LAND = GeeDataset(
    key="era5land_daily",
    collections=("ECMWF/ERA5_LAND/DAILY_AGGR",),
    cadence=Cadence.DAILY,
    grid_band="temperature_2m",
    resolution_label="0.1° (~11 km)",
    agency="ECMWF / Copernicus C3S",
    variables=(
        _var(
            "temp_mean",
            kelvin_to_celsius("temperature_2m"),
            "degC",
            TEMPERATURE,
            "ERA5-Land 2 m air temperature, daily mean",
        ),
        _var(
            "temp_max",
            kelvin_to_celsius("temperature_2m_max"),
            "degC",
            TEMPERATURE,
            "ERA5-Land 2 m air temperature, daily maximum",
        ),
        _var(
            "temp_min",
            kelvin_to_celsius("temperature_2m_min"),
            "degC",
            TEMPERATURE,
            "ERA5-Land 2 m air temperature, daily minimum",
        ),
        _var(
            "dewpoint_mean",
            kelvin_to_celsius("dewpoint_temperature_2m"),
            "degC",
            DEWPOINT,
            "ERA5-Land 2 m dewpoint, daily mean",
        ),
        _var(
            "rh_mean",
            relative_humidity_from_dewpoint("temperature_2m", "dewpoint_temperature_2m"),
            "%",
            RELATIVE_HUMIDITY,
            "ERA5-Land relative humidity from daily-mean T and Td (Magnus)",
        ),
        _var(
            "skin_temp",
            kelvin_to_celsius("skin_temperature"),
            "degC",
            SURFACE_TEMPERATURE,
            "ERA5-Land skin temperature, daily mean",
        ),
        _precip("precip", water_depth("total_precipitation_sum"), "ERA5-Land total precipitation, daily sum"),
        _var(
            "solar_radiation",
            band("surface_solar_radiation_downwards_sum", 1e-6),
            "MJ m-2 d-1",
            SOLAR_RADIATION,
            "ERA5-Land surface solar radiation downwards, daily sum",
        ),
        _var(
            "wind_speed",
            wind_speed("u_component_of_wind_10m", "v_component_of_wind_10m"),
            "m s-1",
            WIND_SPEED,
            "ERA5-Land 10 m wind speed of the daily-mean u/v components (a lower bound of the mean speed)",
        ),
        _var(
            "surface_pressure",
            band("surface_pressure", 0.01),
            "hPa",
            PRESSURE,
            "ERA5-Land surface pressure, daily mean",
        ),
        _var(
            "soil_water_l1",
            band("volumetric_soil_water_layer_1"),
            "m3 m-3",
            VOLUMETRIC_SOIL_WATER,
            "ERA5-Land volumetric soil water 0-7 cm, daily mean",
        ),
        _daily_sum(
            "evaporation",
            water_depth("total_evaporation_sum", sign=-1.0),
            "mm d-1",
            EVAPORATION,
            "ERA5-Land actual evaporation, daily sum (positive upwards)",
        ),
        _daily_sum(
            "pet",
            water_depth("potential_evaporation_sum", sign=-1.0),
            "mm d-1",
            EVAPORATION,
            "ERA5-Land potential evaporation, daily sum (positive upwards)",
        ),
        _daily_sum(
            "water_balance",
            era5_water_balance,
            "mm d-1",
            WATER_BALANCE,
            "ERA5-Land climatic water balance P - PET (input of SPEI-type drought indices)",
        ),
        _var(
            "u_wind_10m",
            band("u_component_of_wind_10m"),
            "m s-1",
            WIND_COMPONENT,
            "ERA5-Land 10 m eastward wind, daily mean",
        ),
        _var(
            "v_wind_10m",
            band("v_component_of_wind_10m"),
            "m s-1",
            WIND_COMPONENT,
            "ERA5-Land 10 m northward wind, daily mean",
        ),
        _var(
            "temp_range",
            band_difference("temperature_2m_max", "temperature_2m_min"),
            "degC",
            TEMPERATURE_RANGE,
            "ERA5-Land diurnal temperature range (Tmax - Tmin)",
        ),
        _var(
            "vpd_mean",
            era5_vpd,
            "hPa",
            VAPOUR_PRESSURE_DEFICIT,
            "ERA5-Land vapour pressure deficit from daily-mean T and Td",
        ),
        _var(
            "rh_min_est",
            era5_rh_from("temperature_2m_max"),
            "%",
            RELATIVE_HUMIDITY,
            "ERA5-Land RH at Tmax (≈ daily minimum)",
        ),
        _var(
            "rh_max_est",
            era5_rh_from("temperature_2m_min"),
            "%",
            RELATIVE_HUMIDITY,
            "ERA5-Land RH at Tmin (≈ daily maximum)",
        ),
        _var(
            "specific_humidity",
            era5_specific_humidity,
            "g kg-1",
            SPECIFIC_HUMIDITY,
            "ERA5-Land 2 m specific humidity",
        ),
        _var(
            "soil_water_l2",
            band("volumetric_soil_water_layer_2"),
            "m3 m-3",
            VOLUMETRIC_SOIL_WATER,
            "ERA5-Land soil water 7-28 cm",
        ),
        _var(
            "soil_water_l3",
            band("volumetric_soil_water_layer_3"),
            "m3 m-3",
            VOLUMETRIC_SOIL_WATER,
            "ERA5-Land soil water 28-100 cm",
        ),
        _var(
            "soil_temp_l1",
            kelvin_to_celsius("soil_temperature_level_1"),
            "degC",
            SURFACE_TEMPERATURE,
            "ERA5-Land soil temperature 0-7 cm",
        ),
        _var(
            "latent_heat_flux",
            band("surface_latent_heat_flux_sum", -1e-6),
            "MJ m-2 d-1",
            HEAT_FLUX,
            "ERA5-Land surface latent heat flux, daily sum (positive upwards)",
        ),
        _var(
            "sensible_heat_flux",
            band("surface_sensible_heat_flux_sum", -1e-6),
            "MJ m-2 d-1",
            HEAT_FLUX,
            "ERA5-Land surface sensible heat flux, daily sum (positive upwards)",
        ),
        _var(
            "net_solar_radiation",
            band("surface_net_solar_radiation_sum", 1e-6),
            "MJ m-2 d-1",
            SOLAR_RADIATION,
            "ERA5-Land surface net shortwave radiation, daily sum",
        ),
        _var(
            "net_thermal_radiation",
            band("surface_net_thermal_radiation_sum", 1e-6),
            "MJ m-2 d-1",
            NET_THERMAL,
            "ERA5-Land surface net longwave radiation, daily sum (negative = loss)",
        ),
        _var(
            "thermal_radiation_down",
            band("surface_thermal_radiation_downwards_sum", 1e-6),
            "MJ m-2 d-1",
            LONGWAVE_DOWN,
            "ERA5-Land downward longwave radiation, daily sum (cloud and humidity signal)",
        ),
        _daily_sum(
            "runoff", water_depth("runoff_sum"), "mm d-1", PRECIPITATION, "ERA5-Land total runoff, daily sum"
        ),
        _var(
            "lai_high",
            band("leaf_area_index_high_vegetation"),
            "m2 m-2",
            LEAF_AREA,
            "ERA5-Land LAI of high vegetation",
        ),
        _var(
            "lai_low",
            band("leaf_area_index_low_vegetation"),
            "m2 m-2",
            LEAF_AREA,
            "ERA5-Land LAI of low vegetation",
        ),
        _var("albedo", band("forecast_albedo"), "1", FRACTION, "ERA5-Land forecast albedo, daily mean"),
    ),
)

IMERG = GeeDataset(
    key="imerg_v07",
    collections=("NASA/GPM_L3/IMERG_V07",),
    cadence=Cadence.SUBDAILY,
    min_images=48,
    grid_band="precipitation",
    status_property=("status", frozenset({"permanent"})),
    resolution_label="0.1° (~11 km), 30 min",
    agency="NASA GPM",
    variables=(
        # Mean of the half-hourly mm/h rates × 24 = daily total.
        _precip("precip", band("precipitation", 24.0), "GPM IMERG V07 precipitation, daily total"),
        _var(
            "precip_liquid_prob",
            band("probabilityLiquidPrecipitation", 0.01),
            "1",
            FRACTION,
            "IMERG probability of liquid precipitation",
            default=False,
        ),
    ),
)

GSMAP = GeeDataset(
    key="gsmap_v8",
    collections=("JAXA/GPM_L3/GSMaP/v8/operational",),
    cadence=Cadence.SUBDAILY,
    min_images=24,
    grid_band="hourlyPrecipRateGC",
    resolution_label="0.1° (~11 km), hourly",
    agency="JAXA",
    variables=(
        _precip(
            "precip", band("hourlyPrecipRateGC", 24.0), "GSMaP v8 gauge-calibrated precipitation, daily total"
        ),
    ),
)

MODIS_LST_TERRA = GeeDataset(
    key="mod11a1",
    collections=("MODIS/061/MOD11A1",),
    cadence=Cadence.DAILY,
    grid_band="LST_Day_1km",
    resolution_label="1 km",
    agency="NASA LP DAAC",
    variables=(
        _lst("lst_day_terra", "LST_Day_1km", "QC_Day", "Terra day (~10:30)"),
        _lst("lst_night_terra", "LST_Night_1km", "QC_Night", "Terra night (~22:30)"),
    ),
)
MODIS_LST_AQUA = GeeDataset(
    key="myd11a1",
    collections=("MODIS/061/MYD11A1",),
    cadence=Cadence.DAILY,
    grid_band="LST_Day_1km",
    resolution_label="1 km",
    agency="NASA LP DAAC",
    variables=(
        _lst("lst_day_aqua", "LST_Day_1km", "QC_Day", "Aqua day (~13:30)"),
        _lst("lst_night_aqua", "LST_Night_1km", "QC_Night", "Aqua night (~01:30)"),
    ),
)
MODIS_CLOUD = GeeDataset(
    key="mod09ga_cloud",
    collections=("MODIS/061/MOD09GA", "MODIS/061/MYD09GA"),
    cadence=Cadence.DAILY,
    grid_band="state_1km",
    resolution_label="1 km",
    agency="NASA LP DAAC",
    variables=(
        _var(
            "cloud_frac",
            modis_cloud_fraction,
            "1",
            FRACTION,
            "MODIS Terra+Aqua cloudy fraction of the day's overpasses (MOD35 state)",
            resampling=ResamplingMethod.AVERAGE,
        ),
    ),
)

VIIRS_DAILY = GeeDataset(
    key="vnp46a2",
    collections=("NASA/VIIRS/002/VNP46A2",),
    cadence=Cadence.DAILY,
    grid_band="Gap_Filled_DNB_BRDF_Corrected_NTL",
    empty_is_missing=True,
    resolution_label="15 arc-s (~500 m)",
    agency="NASA Black Marble",
    variables=(
        _var(
            "ntl",
            band("Gap_Filled_DNB_BRDF_Corrected_NTL"),
            "nW cm-2 sr-1",
            NIGHT_LIGHTS,
            "VIIRS Black Marble gap-filled BRDF-corrected night-time lights",
            resampling=ResamplingMethod.AVERAGE,
            min_valid_days=1,
        ),
    ),
)

SMAP_L4 = GeeDataset(
    key="spl4smgp",
    collections=("NASA/SMAP/SPL4SMGP/008",),
    cadence=Cadence.SUBDAILY,
    min_images=8,
    native_grid=False,  # EASE-Grid 2.0: irregular in latitude, download in EPSG:4326 instead
    scale_m=9000.0,
    resolution_label="9 km, 3-hourly",
    agency="NASA SMAP",
    variables=(
        _var(
            "sm_surface",
            band("sm_surface"),
            "m3 m-3",
            VOLUMETRIC_SOIL_WATER,
            "SMAP L4 surface (0-5 cm) soil moisture, daily mean",
        ),
        _var(
            "sm_rootzone",
            band("sm_rootzone"),
            "m3 m-3",
            VOLUMETRIC_SOIL_WATER,
            "SMAP L4 root-zone (0-100 cm) soil moisture, daily mean",
        ),
        _var(
            "surface_temp",
            kelvin_to_celsius("surface_temp"),
            "degC",
            SURFACE_TEMPERATURE,
            "SMAP L4 mean land surface temperature",
            default=False,
        ),
    ),
)

LANDSAT = GeeDataset(
    key="landsat_c2_l2",
    collections=("LANDSAT/LC08/C02/T1_L2", "LANDSAT/LC09/C02/T1_L2"),
    cadence=Cadence.DAILY,
    grid_band="SR_B4",
    resolution_label="30 m (L8+L9, ~8-day revisit)",
    agency="USGS / NASA",
    variables=(
        _var(
            "lst",
            band("ST_B10", scale=0.00341802, offset=149.0 - 273.15),
            "degC",
            SURFACE_TEMPERATURE,
            "Landsat 8/9 Collection-2 surface temperature (cloud-masked)",
            mask=landsat_clear,
            resampling=ResamplingMethod.AVERAGE,
            min_valid_days=1,
        ),
        _var(
            "ndvi",
            landsat_index("SR_B5", "SR_B4"),
            "1",
            NORMALIZED_INDEX,
            "Landsat 8/9 NDVI (cloud-masked)",
            mask=landsat_clear,
            resampling=ResamplingMethod.AVERAGE,
            min_valid_days=1,
        ),
        _var(
            "ndbi",
            landsat_index("SR_B6", "SR_B5"),
            "1",
            NORMALIZED_INDEX,
            "Landsat 8/9 NDBI built-up index (SWIR1 - NIR)/(SWIR1 + NIR), cloud-masked",
            mask=landsat_clear,
            resampling=ResamplingMethod.AVERAGE,
            min_valid_days=1,
        ),
    ),
)

MODIS_MAIAC = GeeDataset(
    key="mcd19a2",
    collections=("MODIS/061/MCD19A2_GRANULES",),
    cadence=Cadence.DAILY,
    grid_band="Column_WV",
    resolution_label="1 km",
    agency="NASA LP DAAC (MAIAC)",
    variables=(
        _var(
            "column_wv",
            band("Column_WV", 0.001),
            "cm",
            WATER_VAPOUR_COLUMN,
            "MAIAC column water vapour (precipitable water), clear sky",
            mask=maiac_clear,
            resampling=ResamplingMethod.AVERAGE,
            min_valid_days=1,
        ),
        _var(
            "aod_055",
            band("Optical_Depth_055", 0.001),
            "1",
            AEROSOL_OPTICAL_DEPTH,
            "MAIAC aerosol optical depth at 0.55 µm (radiation attenuation)",
            mask=maiac_clear,
            resampling=ResamplingMethod.AVERAGE,
            min_valid_days=1,
        ),
    ),
)


def _viirs_lst(key: str, collection: str, name: str, label: str) -> GeeDataset:
    return GeeDataset(
        key=key,
        collections=(collection,),
        cadence=Cadence.DAILY,
        grid_band="LST_1KM",
        resolution_label="1 km",
        agency="NASA (VIIRS VNP21)",
        variables=(
            _var(
                name,
                kelvin_to_celsius("LST_1KM"),
                "degC",
                SURFACE_TEMPERATURE,
                f"VIIRS {label} land surface temperature, 1 km (QC good or other quality)",
                mask=viirs_lst_quality,
                min_valid_days=1,
            ),
        ),
    )


VIIRS_LST_DAY = _viirs_lst("vnp21a1d", "NASA/VIIRS/002/VNP21A1D", "lst_day", "day (~13:30)")
VIIRS_LST_NIGHT = _viirs_lst("vnp21a1n", "NASA/VIIRS/002/VNP21A1N", "lst_night", "night (~01:30)")


def _goes_bt(channel: int, label: str) -> DatasetVariable:
    return _var(
        f"bt_c{channel:02d}",
        goes_channel(channel),
        "K",
        BRIGHTNESS_TEMPERATURE,
        f"GOES-East ABI band {channel} ({label}) brightness temperature, daily mean of 10-min scans",
        mask=goes_quality(channel),
    )


GOES_EAST = GeeDataset(
    key="goes_east_abi",
    # GOES-16 was GOES-East until 2025-04-07; GOES-19 since then (same 75.2° W slot).
    collections=("NOAA/GOES/16/MCMIPF", "NOAA/GOES/19/MCMIPF"),
    cadence=Cadence.SUBDAILY,
    min_images=120,  # of 144 full-disk scans per day
    native_grid=False,
    scale_m=2000.0,
    resolution_label="2 km, 10 min",
    agency="NOAA GOES-East ABI",
    variables=(
        _goes_bt(8, "6.2 µm upper-level water vapour"),
        _goes_bt(9, "6.9 µm mid-level water vapour"),
        _goes_bt(10, "7.3 µm lower-level water vapour"),
        _goes_bt(11, "8.4 µm cloud-top phase"),
        _goes_bt(13, "10.3 µm clean infrared window"),
        _goes_bt(14, "11.2 µm infrared window"),
        _goes_bt(15, "12.3 µm dirty infrared window"),
        _var(
            "bt_c13_min",
            goes_channel(13),
            "K",
            BRIGHTNESS_TEMPERATURE,
            "GOES-East coldest 10.3 µm brightness temperature of the day (deepest convection)",
            mask=goes_quality(13),
            reducer="min",
        ),
        _var(
            "btd_c13_c15",
            goes_difference(13, 15),
            "K",
            BRIGHTNESS_TEMPERATURE_DIFFERENCE,
            "GOES-East split window 10.3 - 12.3 µm (low-level moisture, thin cirrus)",
            mask=goes_quality(13, 15),
        ),
        _var(
            "btd_c08_c13",
            goes_difference(8, 13),
            "K",
            BRIGHTNESS_TEMPERATURE_DIFFERENCE,
            "GOES-East water vapour - window 6.2 - 10.3 µm (near 0 or positive: deep convection)",
            mask=goes_quality(8, 13),
        ),
        _var(
            "btd_c11_c14",
            goes_difference(11, 14),
            "K",
            BRIGHTNESS_TEMPERATURE_DIFFERENCE,
            "GOES-East 8.4 - 11.2 µm (cloud-top phase: ice vs water)",
            mask=goes_quality(11, 14),
        ),
        _var(
            "cold_cloud_frac_235",
            goes_cold_cloud(235.0),
            "1",
            FRACTION,
            "GOES-East fraction of scans with 10.3 µm < 235 K (cold cloud duration)",
            mask=goes_quality(13),
            resampling=ResamplingMethod.AVERAGE,
        ),
        _var(
            "cold_cloud_frac_220",
            goes_cold_cloud(220.0),
            "1",
            FRACTION,
            "GOES-East fraction of scans with 10.3 µm < 220 K (deep convection)",
            mask=goes_quality(13),
            resampling=ResamplingMethod.AVERAGE,
        ),
        _var(
            "refl_c02_day",
            goes_daytime_reflectance,
            "1",
            REFLECTANCE,
            "GOES-East 0.64 µm reflectance, mean of 08-16 h local scans (cloud albedo / solar radiation)",
            mask=goes_quality(2),
            resampling=ResamplingMethod.AVERAGE,
        ),
    ),
)

SENTINEL1 = GeeDataset(
    key="s1_grd",
    collections=("COPERNICUS/S1_GRD",),
    cadence=Cadence.DAILY,
    native_grid=False,
    scale_m=30.0,
    prefilter=sentinel1_dual_pol,
    resolution_label="10 m (IW GRD), delivered at 30 m; ~6-day revisit",
    agency="ESA Copernicus",
    variables=(
        _var(
            "vv_db",
            band("VV"),
            "dB",
            BACKSCATTER_DB,
            "Sentinel-1 VV backscatter (soil moisture, open water, built-up)",
            mask=sentinel1_valid,
            resampling=ResamplingMethod.AVERAGE,
            min_valid_days=1,
        ),
        _var(
            "vh_db",
            band("VH"),
            "dB",
            BACKSCATTER_DB,
            "Sentinel-1 VH backscatter (vegetation volume)",
            mask=sentinel1_valid,
            resampling=ResamplingMethod.AVERAGE,
            min_valid_days=1,
        ),
        _var(
            "vh_vv_db",
            band_difference("VH", "VV"),
            "dB",
            BACKSCATTER_DB,
            "Sentinel-1 cross-polarisation ratio VH - VV (vegetation vs built-up structure)",
            mask=sentinel1_valid,
            resampling=ResamplingMethod.AVERAGE,
            min_valid_days=1,
        ),
    ),
)

#: source name -> datasets. Gold columns are ``{source}_{variable}``.
GEE_SOURCES: dict[str, tuple[GeeDataset, ...]] = {
    "era5land": (ERA5_LAND,),
    "imerg": (IMERG,),
    "gsmap": (GSMAP,),
    "modis": (MODIS_LST_TERRA, MODIS_LST_AQUA, MODIS_CLOUD, MODIS_MAIAC),
    "viirs": (VIIRS_DAILY, VIIRS_LST_DAY, VIIRS_LST_NIGHT),
    "smap": (SMAP_L4,),
    "landsat": (LANDSAT,),
    "goes": (GOES_EAST,),
    "sentinel1": (SENTINEL1,),
}


def dataset_variable_spec(source: str, variable: DatasetVariable) -> VariableSpec:
    return VariableSpec(
        source=source,
        name=variable.name,
        gold_column=f"{source}_{variable.name}",
        units=variable.units,
        native_units=variable.units,
        bounds=variable.bounds,
        resampling=variable.resampling,
        weekly_aggregation=variable.weekly,
        min_valid_days=variable.min_valid_days,
        default=variable.default,
        description=variable.description,
    )


def gee_dataset_specs() -> list[VariableSpec]:
    return [
        dataset_variable_spec(source, variable)
        for source, datasets in GEE_SOURCES.items()
        for dataset in datasets
        for variable in dataset.variables
    ]


@dataclass(frozen=True)
class _Task:
    dataset: GeeDataset
    window: Window
    names: tuple[str, ...]
    day: date


class GeeCollectionExtractor(BaseExtractor):
    """Shared extractor of every :data:`GEE_SOURCES` entry (one subclass per source)."""

    datasets: tuple[GeeDataset, ...] = ()

    def __init__(self, *args: Any, ee_module: Any | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ee = ee_module
        self._projections: dict[str, dict[str, Any] | None] = {}

    @property
    def utc_offset_hours(self) -> float:
        return float(self.config.options.get("utc_offset_hours", 0.0))

    def _earth_engine(self) -> Any:
        if self._ee is None:
            key = self.settings.gee_service_account_key
            self._ee = initialize_earth_engine(self.settings.gee_project, str(key) if key else None)
        return self._ee

    def _region(self, ee: Any, dataset: GeeDataset, projection: dict[str, Any] | None) -> Any:
        if projection is not None:
            step = abs(float(projection["transform"][0]))
            geographic = str(projection["crs"]).upper() == "EPSG:4326"
            cell_deg = step if geographic else step / METERS_PER_DEGREE
        else:
            cell_deg = (dataset.scale_m or 1000.0) / METERS_PER_DEGREE
        # Enough margin for every pixel touching the AOI (coarse products: 1.5 cells).
        min_x, min_y, max_x, max_y = self.aoi.expanded_bounds(max(0.02, 1.5 * cell_deg))
        return ee.Geometry.Rectangle([min_x, min_y, max_x, max_y], "EPSG:4326", False)

    def _native_projection(self, ee: Any, dataset: GeeDataset) -> dict[str, Any] | None:
        """CRS + transform of the provider grid over the AOI (cached per dataset)."""
        if not dataset.native_grid:
            return None
        if dataset.key not in self._projections:
            min_x, min_y, max_x, max_y = self.aoi.bounds
            area = ee.Geometry.Rectangle([min_x, min_y, max_x, max_y], "EPSG:4326", False)
            image = ee.ImageCollection(dataset.collections[0]).filterBounds(area).first()
            selected = image.select(dataset.grid_band) if dataset.grid_band else image.select(0)
            projection = selected.projection().getInfo()
            crs = projection.get("crs") or projection.get("wkt")
            self._projections[dataset.key] = (
                {"crs": crs, "transform": list(projection["transform"])}
                if crs and "transform" in projection
                else None
            )
        return self._projections[dataset.key]

    def _tasks(self, requests: Mapping[str, Sequence[date]]) -> list[_Task]:
        tasks: list[_Task] = []
        for dataset in self.datasets:
            names = tuple(variable.name for variable in dataset.variables if variable.name in requests)
            if not names:
                continue
            days = sorted({day for name in names for day in requests[name]})
            # Every requested variable is downloaded for every requested day of the dataset
            # (one multi-band GeoTIFF per day), which keeps Bronze consistent across variables.
            tasks.extend(
                _Task(dataset, period_window(dataset.cadence, day, self.utc_offset_hours), names, day)
                for day in days
            )
        return tasks

    def fetch(self, requests: Mapping[str, Sequence[date]]) -> Iterator[ExtractionOutcome]:
        tasks = self._tasks(requests)
        if not tasks:
            return
        ee = self._earth_engine()
        projections = {
            dataset.key: self._native_projection(ee, dataset) for dataset in {task.dataset for task in tasks}
        }

        def worker(task: _Task) -> list[ExtractionOutcome]:
            return self._fetch_day(ee, task, projections[task.dataset.key])

        def on_error(task: _Task, exc: Exception) -> list[ExtractionOutcome]:
            return [ExtractionFailure(self.name, name, task.day, repr(exc)) for name in task.names]

        yield from self.run_parallel(tasks, worker, on_error)

    def _fetch_day(self, ee: Any, task: _Task, projection: dict[str, Any] | None) -> list[ExtractionOutcome]:
        dataset = task.dataset
        region = self._region(ee, dataset, projection)
        collection = ee.ImageCollection(dataset.collections[0])
        for extra in dataset.collections[1:]:
            collection = collection.merge(ee.ImageCollection(extra))
        collection = collection.filterDate(
            task.window.start.isoformat(timespec="seconds"), task.window.end.isoformat(timespec="seconds")
        ).filterBounds(region)
        if dataset.prefilter is not None:
            collection = dataset.prefilter(ee, collection)

        probe: dict[str, Any] = {"n": collection.size()}
        if dataset.status_property:
            probe["status"] = collection.aggregate_array(dataset.status_property[0]).distinct()
        info = ee.Dictionary(probe).getInfo()
        count = int(info["n"])
        if count < dataset.min_images:
            reason = (
                f"{count} of {dataset.min_images} images in {task.window.label} (day not complete yet)"
                if count
                else f"no image in {dataset.collections} for {task.window.label}"
            )
            return [MissingData(self.name, name, task.day, reason) for name in task.names]
        status = ProductStatus.FINAL
        if dataset.status_property and not set(info.get("status") or []) <= dataset.status_property[1]:
            status = ProductStatus.PRELIM

        variables = {variable.name: variable for variable in dataset.variables}

        def to_variables(image: Any) -> Any:
            layers = []
            for name in task.names:
                variable = variables[name]
                layer = variable.compute(image).toFloat()
                if variable.mask is not None:
                    layer = layer.updateMask(variable.mask(image))
                layers.append(layer.rename(name))
            return ee.Image.cat(layers)

        # Mean (or min/max) of the valid observations of the day; masked pixels do not contribute.
        mapped = collection.map(to_variables)
        reducers: dict[str, list[str]] = {}
        for name in task.names:
            reducers.setdefault(variables[name].reducer, []).append(name)
        reduced = []
        for reducer, names in reducers.items():
            subset = mapped.select(names)
            reduced.append(
                subset.min() if reducer == "min" else subset.max() if reducer == "max" else subset.mean()
            )
        stacked = ee.Image.cat(reduced).select(list(task.names))
        parameters: dict[str, Any] = {"region": region, "format": "GEO_TIFF", "bands": list(task.names)}
        if projection is not None:
            parameters.update({"crs": projection["crs"], "crs_transform": projection["transform"]})
        else:
            parameters.update({"crs": "EPSG:4326", "scale": dataset.scale_m})
        payload = self.http_get(stacked.unmask(GEE_NODATA).getDownloadURL(parameters))
        arrays = geotiff_to_arrays(payload, list(task.names), nodata=GEE_NODATA)
        if dataset.empty_is_missing and all(not np.isfinite(array.values).any() for array in arrays.values()):
            reason = f"{dataset.collections}: image for {task.window.label} has no value over the AOI yet"
            return [MissingData(self.name, name, task.day, reason) for name in task.names]
        origin = f"gee:{'+'.join(dataset.collections)}:{task.window.label}"
        outcomes: list[ExtractionOutcome] = []
        for name in task.names:
            array = arrays[name]
            array.attrs["units"] = variables[name].units
            outcomes.append(PendingBronze(array, self.variables[name], task.day, status, origin))
        return outcomes


def _register(source: str, datasets: tuple[GeeDataset, ...]) -> type[GeeCollectionExtractor]:
    cls = type(
        f"{source.title()}Extractor",
        (GeeCollectionExtractor,),
        {"name": source, "datasets": datasets, "__doc__": f"Earth Engine source ``{source}``."},
    )
    register_extractor(cls)
    return cls


GEE_SOURCE_EXTRACTORS = {source: _register(source, datasets) for source, datasets in GEE_SOURCES.items()}
