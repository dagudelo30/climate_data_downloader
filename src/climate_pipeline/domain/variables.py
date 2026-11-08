"""Canonical variable catalog shared by every layer of the pipeline.

Each :class:`VariableSpec` answers, for a single variable of a single source:

* how to request it upstream (``request``),
* how it is named in Silver (``name``) and in Gold (``gold_column``),
* which units it must end up in and which physical bounds it must respect,
* how it is resampled to a common grid and aggregated to epidemiological weeks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from climate_pipeline.domain.errors import ConfigurationError


class ResamplingMethod(StrEnum):
    """Spatial resampling applied when moving Silver data onto the Gold grid."""

    BILINEAR = "bilinear"  # continuous fields (temperature, humidity, wind...)
    AVERAGE = "average"  # fluxes/accumulations: area-weighted, approximately mass conserving
    NEAREST = "nearest"  # categorical or count-like fields


class WeeklyAggregation(StrEnum):
    """Temporal aggregation from daily values to epidemiological weeks."""

    SUM = "sum"
    MEAN = "mean"
    MIN = "min"
    MAX = "max"


@dataclass(frozen=True)
class QualityBounds:
    """Physically plausible range of a variable, expressed in its target units.

    Values outside ``[minimum - tolerance, maximum + tolerance]`` are discarded (set to
    NaN). Values inside the tolerance band but outside ``[minimum, maximum]`` are clipped
    to the nearest bound; this absorbs harmless numerical noise such as a relative
    humidity of 100.3 %.
    """

    minimum: float
    maximum: float
    tolerance: float = 0.0

    def __post_init__(self) -> None:
        if self.minimum >= self.maximum:
            raise ValueError(f"invalid bounds: minimum {self.minimum} >= maximum {self.maximum}")
        if self.tolerance < 0:
            raise ValueError("tolerance must be non-negative")


@dataclass(frozen=True, eq=False)
class VariableSpec:
    source: str
    name: str
    gold_column: str
    units: str
    native_units: str
    bounds: QualityBounds
    resampling: ResamplingMethod
    weekly_aggregation: WeeklyAggregation
    min_valid_days: int = 4
    request: Mapping[str, Any] = field(default_factory=dict)
    default: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        if not 1 <= self.min_valid_days <= 7:
            raise ValueError(f"{self.gold_column}: min_valid_days must be within 1..7")
        if not self.gold_column.startswith(f"{self.source}_"):
            raise ValueError(f"gold column {self.gold_column!r} must start with '{self.source}_'")


# --- Physical bounds (target units) ----------------------------------------------------
# Daily precipitation world record is ~1,825 mm (La Réunion, 1966).
PRECIPITATION_BOUNDS = QualityBounds(0.0, 2000.0)
# Lowest/highest air temperatures ever measured near the surface: -89.2 °C / 56.7 °C.
AIR_TEMPERATURE_BOUNDS = QualityBounds(-90.0, 60.0)
DEWPOINT_BOUNDS = QualityBounds(-90.0, 40.0)
# Daily top-of-atmosphere irradiation never exceeds ~45 MJ m-2.
SOLAR_RADIATION_BOUNDS = QualityBounds(0.0, 45.0)
VAPOUR_PRESSURE_BOUNDS = QualityBounds(0.0, 80.0)
RELATIVE_HUMIDITY_BOUNDS = QualityBounds(0.0, 100.0, tolerance=2.0)
WIND_SPEED_BOUNDS = QualityBounds(0.0, 75.0)
CLOUD_COVER_BOUNDS = QualityBounds(0.0, 1.0, tolerance=0.01)
# Normalized difference indices are bounded by construction.
NORMALIZED_INDEX_BOUNDS = QualityBounds(-1.0, 1.0)


def _precipitation(source: str, description: str, **request: Any) -> VariableSpec:
    return VariableSpec(
        source=source,
        name="precipitation_flux",
        gold_column=f"{source}_precip",
        units="mm d-1",
        native_units="mm d-1",
        bounds=PRECIPITATION_BOUNDS,
        resampling=ResamplingMethod.AVERAGE,
        weekly_aggregation=WeeklyAggregation.SUM,
        # A weekly total with missing days is biased low: require the full week.
        min_valid_days=7,
        request=request,
        description=description,
    )


CHIRPS_VARIABLES: tuple[VariableSpec, ...] = (_precipitation("chirps", "CHIRPS daily precipitation (0.05°)"),)

MSWEP_VARIABLES: tuple[VariableSpec, ...] = (_precipitation("mswep", "MSWEP daily precipitation (0.1°)"),)


def _agera5_temperature(name: str, column: str, statistic: str, label: str) -> VariableSpec:
    return VariableSpec(
        source="agera5",
        name=name,
        gold_column=column,
        units="degC",
        native_units="K",
        bounds=AIR_TEMPERATURE_BOUNDS,
        resampling=ResamplingMethod.BILINEAR,
        weekly_aggregation=WeeklyAggregation.MEAN,
        request={"variable": "2m_temperature", "statistic": statistic},
        description=f"AgERA5 2 m air temperature, {label}",
    )


def _agera5_relative_humidity(hour: str) -> VariableSpec:
    return VariableSpec(
        source="agera5",
        name=f"relative_humidity_2m_{hour}h",
        gold_column=f"agera5_rh_{hour}h",
        units="%",
        native_units="%",
        bounds=RELATIVE_HUMIDITY_BOUNDS,
        resampling=ResamplingMethod.BILINEAR,
        weekly_aggregation=WeeklyAggregation.MEAN,
        request={"variable": "2m_relative_humidity", "time": f"{hour}_00"},
        description=f"AgERA5 2 m relative humidity at {hour}:00 local solar time",
    )


# Variables selected for temperature/precipitation downscaling and spatial interpolation:
# thermal drivers (Tmean/Tmax/Tmin, radiation), moisture (vapour pressure, dewpoint,
# relative humidity at the times of daily Tmin and Tmax), wind and a precipitation
# reference consistent with the other predictors.
AGERA5_VARIABLES: tuple[VariableSpec, ...] = (
    _agera5_temperature("temp_mean_2m", "agera5_temp_mean", "24_hour_mean", "24 h mean"),
    _agera5_temperature("temp_max_2m", "agera5_temp_max", "24_hour_maximum", "24 h maximum"),
    _agera5_temperature("temp_min_2m", "agera5_temp_min", "24_hour_minimum", "24 h minimum"),
    VariableSpec(
        source="agera5",
        name="dewpoint_temp_2m",
        gold_column="agera5_dewpoint_mean",
        units="degC",
        native_units="K",
        bounds=DEWPOINT_BOUNDS,
        resampling=ResamplingMethod.BILINEAR,
        weekly_aggregation=WeeklyAggregation.MEAN,
        request={"variable": "2m_dewpoint_temperature", "statistic": "24_hour_mean"},
        description="AgERA5 2 m dewpoint temperature, 24 h mean",
    ),
    VariableSpec(
        source="agera5",
        name="solar_radiation_flux",
        gold_column="agera5_solar_radiation",
        units="MJ m-2 d-1",
        native_units="J m-2 d-1",
        bounds=SOLAR_RADIATION_BOUNDS,
        resampling=ResamplingMethod.BILINEAR,
        weekly_aggregation=WeeklyAggregation.MEAN,
        request={"variable": "solar_radiation_flux"},
        description="AgERA5 daily surface solar radiation downwards",
    ),
    VariableSpec(
        source="agera5",
        name="vapour_pressure",
        gold_column="agera5_vapour_pressure",
        units="hPa",
        native_units="hPa",
        bounds=VAPOUR_PRESSURE_BOUNDS,
        resampling=ResamplingMethod.BILINEAR,
        weekly_aggregation=WeeklyAggregation.MEAN,
        request={"variable": "vapour_pressure", "statistic": "24_hour_mean"},
        description="AgERA5 vapour pressure, 24 h mean",
    ),
    _agera5_relative_humidity("06"),
    _agera5_relative_humidity("15"),
    VariableSpec(
        source="agera5",
        name="wind_speed_10m",
        gold_column="agera5_wind_speed",
        units="m s-1",
        native_units="m s-1",
        bounds=WIND_SPEED_BOUNDS,
        resampling=ResamplingMethod.BILINEAR,
        weekly_aggregation=WeeklyAggregation.MEAN,
        request={"variable": "10m_wind_speed", "statistic": "24_hour_mean"},
        description="AgERA5 10 m wind speed, 24 h mean",
    ),
    VariableSpec(
        source="agera5",
        name="precipitation_flux",
        gold_column="agera5_precip",
        units="mm d-1",
        native_units="mm d-1",
        bounds=PRECIPITATION_BOUNDS,
        resampling=ResamplingMethod.AVERAGE,
        weekly_aggregation=WeeklyAggregation.SUM,
        min_valid_days=7,
        request={"variable": "precipitation_flux"},
        description="AgERA5 daily total precipitation",
    ),
    VariableSpec(
        source="agera5",
        name="cloud_cover",
        gold_column="agera5_cloud_cover",
        units="1",
        native_units="1",
        bounds=CLOUD_COVER_BOUNDS,
        resampling=ResamplingMethod.BILINEAR,
        weekly_aggregation=WeeklyAggregation.MEAN,
        request={"variable": "cloud_cover", "statistic": "24_hour_mean"},
        description="AgERA5 total cloud cover, 24 h mean (radiation and precipitation proxy)",
    ),
    # Day (06-18 h local solar time) / night statistics: the diurnal cycle drives convection.
    _agera5_temperature("temp_day_max_2m", "agera5_temp_day_max", "day_time_maximum", "daytime maximum"),
    _agera5_temperature("temp_day_mean_2m", "agera5_temp_day_mean", "day_time_mean", "daytime mean"),
    _agera5_temperature("temp_night_mean_2m", "agera5_temp_night_mean", "night_time_mean", "night-time mean"),
    _agera5_temperature(
        "temp_night_min_2m", "agera5_temp_night_min", "night_time_minimum", "night-time minimum"
    ),
    _agera5_relative_humidity("09"),
    _agera5_relative_humidity("12"),
    _agera5_relative_humidity("18"),
    VariableSpec(
        source="agera5",
        name="relative_humidity_2m_max",
        gold_column="agera5_rh_max",
        units="%",
        native_units="%",
        bounds=RELATIVE_HUMIDITY_BOUNDS,
        resampling=ResamplingMethod.BILINEAR,
        weekly_aggregation=WeeklyAggregation.MEAN,
        request={"variable": "2m_relative_humidity_derived", "statistic": "24_hour_maximum"},
        description="AgERA5 daily maximum relative humidity (derived from hourly T and Td)",
    ),
    VariableSpec(
        source="agera5",
        name="relative_humidity_2m_min",
        gold_column="agera5_rh_min",
        units="%",
        native_units="%",
        bounds=RELATIVE_HUMIDITY_BOUNDS,
        resampling=ResamplingMethod.BILINEAR,
        weekly_aggregation=WeeklyAggregation.MEAN,
        request={"variable": "2m_relative_humidity_derived", "statistic": "24_hour_minimum"},
        description="AgERA5 daily minimum relative humidity (derived from hourly T and Td)",
    ),
    VariableSpec(
        source="agera5",
        name="vapour_pressure_deficit_tmax",
        gold_column="agera5_vpd_tmax",
        units="hPa",
        native_units="hPa",
        bounds=QualityBounds(0.0, 150.0),
        resampling=ResamplingMethod.BILINEAR,
        weekly_aggregation=WeeklyAggregation.MEAN,
        request={"variable": "vapour_pressure_deficit_at_maximum_temperature"},
        description="AgERA5 vapour pressure deficit at the daily maximum temperature",
    ),
    VariableSpec(
        source="agera5",
        name="reference_evapotranspiration",
        gold_column="agera5_et0",
        units="mm d-1",
        native_units="mm d-1",
        bounds=QualityBounds(0.0, 30.0),
        resampling=ResamplingMethod.AVERAGE,
        weekly_aggregation=WeeklyAggregation.SUM,
        min_valid_days=7,
        request={"variable": "reference_evapotranspiration"},
        description="AgERA5 FAO-56 Penman-Monteith reference evapotranspiration",
    ),
    VariableSpec(
        source="agera5",
        name="precipitation_duration_fraction",
        gold_column="agera5_precip_hours_frac",
        units="1",
        native_units="1",
        bounds=CLOUD_COVER_BOUNDS,
        resampling=ResamplingMethod.BILINEAR,
        weekly_aggregation=WeeklyAggregation.MEAN,
        request={"variable": "precipitation_duration_fraction"},
        description="AgERA5 fraction of the day's hours with precipitation",
    ),
)


def normalized_index_spec(source: str, name: str, description: str) -> VariableSpec:
    """Spec for a normalized difference index (NDVI, NDWI...) produced by a GEE product."""
    return VariableSpec(
        source=source,
        name=name,
        gold_column=f"{source}_{name}",
        units="1",
        native_units="1",
        bounds=NORMALIZED_INDEX_BOUNDS,
        resampling=ResamplingMethod.AVERAGE,
        weekly_aggregation=WeeklyAggregation.MEAN,
        # Daily optical indices are mostly cloud-masked in the Andes, and vegetation indices
        # change slowly: one clear day is a valid weekly estimate (the ``_ndays`` column
        # tells how many clear days the value rests on).
        min_valid_days=1,
        description=description,
    )


class VariableCatalog:
    """Registry of every variable the pipeline knows about."""

    def __init__(self, specs: Iterable[VariableSpec]) -> None:
        self._by_source: dict[str, dict[str, VariableSpec]] = {}
        self._by_column: dict[str, VariableSpec] = {}
        for spec in specs:
            source_specs = self._by_source.setdefault(spec.source, {})
            if spec.name in source_specs:
                raise ConfigurationError(f"duplicated variable {spec.source}/{spec.name}")
            if spec.gold_column in self._by_column:
                raise ConfigurationError(f"duplicated gold column {spec.gold_column}")
            source_specs[spec.name] = spec
            self._by_column[spec.gold_column] = spec

    @property
    def sources(self) -> list[str]:
        return sorted(self._by_source)

    def for_source(self, source: str, names: Iterable[str] | None = None) -> list[VariableSpec]:
        """Variables of ``source``; the defaults when ``names`` is ``None``."""
        if source not in self._by_source:
            raise ConfigurationError(f"unknown source {source!r}; known: {self.sources}")
        available = self._by_source[source]
        if names is None:
            return [spec for spec in available.values() if spec.default]
        unknown = sorted(set(names) - set(available))
        if unknown:
            raise ConfigurationError(
                f"unknown variables for {source}: {unknown}; available: {sorted(available)}"
            )
        return [available[name] for name in names]

    def get(self, source: str, name: str) -> VariableSpec:
        try:
            return self._by_source[source][name]
        except KeyError as exc:
            raise ConfigurationError(f"unknown variable {source}/{name}") from exc

    def has(self, source: str, name: str) -> bool:
        return name in self._by_source.get(source, {})

    def by_gold_column(self, column: str) -> VariableSpec:
        try:
            return self._by_column[column]
        except KeyError as exc:
            raise ConfigurationError(f"unknown gold column {column!r}") from exc

    def has_gold_column(self, column: str) -> bool:
        return column in self._by_column

    def all(self) -> list[VariableSpec]:
        return list(self._by_column.values())


def default_specs() -> list[VariableSpec]:
    """Static specs; GEE specs are added from the GEE product configuration."""
    return [*CHIRPS_VARIABLES, *MSWEP_VARIABLES, *AGERA5_VARIABLES]
