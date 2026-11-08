"""One column per physical quantity: redundancy rules between sources.

When two sources measure the same quantity with the same temporal sampling, only one goes
to Gold, chosen in this order (operations first: the model needs the most recent data):

1. **lower latency** (days until the provider publishes the day; a difference of one day
   or less is a tie, because it almost never changes the epidemiological week available);
2. **finer native resolution** (provider pixel, in metres);
3. **the most sensible product** (``sense``: lower is better, with the reason written down).

Two further rules:

* products with a different temporal sampling are **not** redundant: a daily sensor (MODIS
  NDVI, 1 overpass/day) and a 5-8-day revisit sensor (Sentinel-2) complement each other,
  because the first fills the cloud gaps of the second;
* a column that can be computed exactly from columns already kept (e.g. ERA5-Land P − PET
  from the kept precipitation and ET₀) is dropped as **derivable**.

Dropped columns stay in the catalog with ``default=False``: they can still be requested
explicitly (``sources.<name>.variables``) or kept by setting ``deduplicate: false``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

LATENCY_TIE_DAYS = 1


@dataclass(frozen=True)
class Candidate:
    column: str
    resolution_m: float
    sense: int = 0
    note: str = ""

    @property
    def source(self) -> str:
        return self.column.split("_", 1)[0]


@dataclass(frozen=True)
class Quantity:
    name: str
    label: str
    sampling: str
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class Derivable:
    column: str
    inputs: tuple[str, ...]
    reason: str


@dataclass
class Decision:
    quantity: str
    label: str
    sampling: str
    kept: str
    dropped: dict[str, str] = field(default_factory=dict)
    rule: str = ""


_DAILY = "daily"
_REVISIT = "5-8 day revisit"

#: Ranking: (1) resolution, (2) latency, (3) sense. Notes explain the "sense" tie-breaks.
QUANTITIES: tuple[Quantity, ...] = (
    Quantity(
        "precipitation",
        "Precipitación diaria",
        _DAILY,
        (
            Candidate("chirps_precip", 5_550),
            Candidate("imerg_precip", 11_100, 1, "IMERG V07: el producto de referencia de la misión GPM"),
            Candidate("gsmap_precip", 11_100, 2),
            Candidate("mswep_precip", 11_100, 3),
            Candidate("era5land_precip", 11_100, 4),
            Candidate("agera5_precip", 11_100, 5),
        ),
    ),
    *(
        Quantity(
            name,
            label,
            _DAILY,
            (
                Candidate(
                    f"agera5_{column}", 11_100, 0, "agregación por día solar local y corrección a 0.1°"
                ),
                Candidate(f"era5land_{era5_column}", 11_100, 1, "días UTC (19:00-19:00 en Colombia)"),
            ),
        )
        for name, label, column, era5_column in (
            ("air_temperature_mean", "Temperatura media del aire", "temp_mean", "temp_mean"),
            ("air_temperature_max", "Temperatura máxima del aire", "temp_max", "temp_max"),
            ("air_temperature_min", "Temperatura mínima del aire", "temp_min", "temp_min"),
            ("dewpoint", "Punto de rocío", "dewpoint_mean", "dewpoint_mean"),
            ("solar_radiation", "Radiación solar descendente", "solar_radiation", "solar_radiation"),
            ("wind_speed", "Velocidad del viento", "wind_speed", "wind_speed"),
        )
    ),
    Quantity(
        "relative_humidity_max",
        "Humedad relativa máxima",
        _DAILY,
        (
            Candidate("agera5_rh_max", 11_100, 0, "derivada de T y Td horarias"),
            Candidate("era5land_rh_max_est", 11_100, 1, "aproximación con Td media y Tmín"),
        ),
    ),
    Quantity(
        "relative_humidity_min",
        "Humedad relativa mínima",
        _DAILY,
        (
            Candidate("agera5_rh_min", 11_100, 0, "derivada de T y Td horarias"),
            Candidate("era5land_rh_min_est", 11_100, 1, "aproximación con Td media y Tmáx"),
        ),
    ),
    Quantity(
        "humidity_content",
        "Contenido de vapor cerca de la superficie",
        _DAILY,
        (
            Candidate("agera5_vapour_pressure", 11_100, 0, "medida directa del vapor"),
            Candidate("era5land_specific_humidity", 11_100, 1, "misma información vía presión"),
        ),
    ),
    Quantity(
        "reference_et",
        "Evapotranspiración de referencia / potencial",
        _DAILY,
        (
            Candidate("agera5_et0", 11_100, 0, "FAO-56 Penman-Monteith, estándar agroclimático"),
            Candidate("era5land_pet", 11_100, 1, "evaporación potencial de superficie libre"),
        ),
    ),
    Quantity(
        "cloudiness",
        "Nubosidad diaria",
        _DAILY,
        (
            Candidate("modis_cloud_frac", 1_000),
            Candidate("agera5_cloud_cover", 11_100),
        ),
    ),
    Quantity(
        "lst_day_1330",
        "Temperatura de superficie diurna (~13:30)",
        _DAILY,
        (
            Candidate("modis_lst_day_aqua", 1_000),
            Candidate("viirs_lst_day", 1_000),
        ),
    ),
    Quantity(
        "lst_night_0130",
        "Temperatura de superficie nocturna (~01:30)",
        _DAILY,
        (
            Candidate("modis_lst_night_aqua", 1_000),
            Candidate("viirs_lst_night", 1_000),
        ),
    ),
    Quantity(
        "soil_moisture_surface",
        "Humedad del suelo superficial",
        _DAILY,
        (
            Candidate("smap_sm_surface", 9_000),
            Candidate("era5land_soil_water_l1", 11_100),
        ),
    ),
    Quantity(
        "soil_moisture_rootzone",
        "Humedad del suelo en la zona de raíces",
        _DAILY,
        (
            Candidate("smap_sm_rootzone", 9_000),
            Candidate("era5land_soil_water_l2", 11_100),
            Candidate("era5land_soil_water_l3", 11_100),
        ),
    ),
    Quantity(
        "ndvi_revisit",
        "NDVI de alta resolución",
        _REVISIT,
        (
            Candidate("gee_s2_ndvi", 10),
            Candidate("landsat_ndvi", 30),
        ),
    ),
    Quantity(
        "moisture_index_revisit",
        "Índice de humedad / construido (SWIR–NIR) de alta resolución",
        _REVISIT,
        (
            Candidate("gee_s2_ndwi", 20, 0, "NDBI = −NDWI con las mismas bandas SWIR/NIR"),
            Candidate("landsat_ndbi", 30),
        ),
    ),
)

#: Columns computable from kept columns (dropped only when every input is kept).
DERIVABLE: tuple[Derivable, ...] = (
    Derivable("era5land_temp_range", ("agera5_temp_max", "agera5_temp_min"), "Tmáx − Tmín"),
    Derivable("era5land_rh_mean", ("agera5_temp_mean", "agera5_dewpoint_mean"), "HR de T y Td medias"),
    Derivable("era5land_vpd_mean", ("agera5_temp_mean", "agera5_dewpoint_mean"), "es(T) − e(Td)"),
    Derivable("era5land_water_balance", ("@precipitation", "@reference_et"), "P − ET₀"),
    Derivable("era5land_latent_heat_flux", ("era5land_evaporation",), "λ · evaporación"),
    Derivable(
        "era5land_net_solar_radiation",
        ("agera5_solar_radiation", "era5land_albedo"),
        "radiación · (1 − albedo)",
    ),
)


def _latency_days(column: str, latency_of: Callable[[str], int | None]) -> int | None:
    return latency_of(column.split("_", 1)[0])


def decide(
    available: Iterable[str],
    latency_of: Callable[[str], int | None],
    quantities: Iterable[Quantity] = QUANTITIES,
    derivable: Iterable[Derivable] = DERIVABLE,
) -> list[Decision]:
    """Choose one column per quantity among ``available`` columns."""
    available = set(available)
    decisions: list[Decision] = []
    for quantity in quantities:
        candidates = [candidate for candidate in quantity.candidates if candidate.column in available]
        if len(candidates) < 2:
            continue
        latencies = {
            candidate.column: _latency_days(candidate.column, latency_of) for candidate in candidates
        }
        known = [value for value in latencies.values() if value is not None]
        pool = candidates
        rule = "latencia"
        if known:
            best = min(known)
            pool = [
                candidate
                for candidate in candidates
                if latencies[candidate.column] is not None
                and latencies[candidate.column] - best <= LATENCY_TIE_DAYS
            ]
        if len(pool) > 1:
            finest = min(candidate.resolution_m for candidate in pool)
            pool = [candidate for candidate in pool if candidate.resolution_m == finest]
            rule = "resolución" if len(pool) == 1 else "criterio"
            pool = sorted(pool, key=lambda candidate: candidate.sense)
        kept = pool[0]
        decision = Decision(quantity.name, quantity.label, quantity.sampling, kept.column, rule=rule)
        lag_kept = latencies[kept.column]
        for candidate in candidates:
            if candidate.column == kept.column:
                continue
            lag_other = latencies[candidate.column]
            if lag_kept is not None and (lag_other is None or lag_other - lag_kept > LATENCY_TIE_DAYS):
                reason = f"latencia {lag_other if lag_other is not None else '?'} d vs {lag_kept} d"
            elif candidate.resolution_m > kept.resolution_m:
                reason = (
                    f"misma latencia; resolución {_fmt(candidate.resolution_m)} vs {_fmt(kept.resolution_m)}"
                )
            else:
                reason = f"misma latencia y resolución; {kept.note or 'producto preferido'}"
            decision.dropped[candidate.column] = reason
        decisions.append(decision)

    kept_columns = available - {column for decision in decisions for column in decision.dropped}
    kept_by_quantity = {decision.quantity: decision.kept for decision in decisions}
    for item in derivable:
        inputs = tuple(
            kept_by_quantity.get(name[1:], "") if name.startswith("@") else name for name in item.inputs
        )
        if item.column in kept_columns and all(column in kept_columns for column in inputs):
            item = Derivable(item.column, inputs, item.reason)
            decisions.append(
                Decision(
                    item.column,
                    f"Derivable: {item.reason}",
                    _DAILY,
                    ", ".join(item.inputs),
                    {item.column: f"se calcula de {', '.join(item.inputs)}"},
                    rule="derivable",
                )
            )
    return decisions


def dropped_columns(decisions: Iterable[Decision]) -> dict[str, str]:
    return {column: reason for decision in decisions for column, reason in decision.dropped.items()}


def _fmt(metres: float) -> str:
    return f"{metres:.0f} m" if metres < 1000 else f"{metres / 1000:.3g} km"
