"""Runtime configuration.

* :class:`Settings`: secrets and paths from environment variables / ``.env``.
* :class:`PipelineConfig`: per-source behaviour (variables, latency, options), with
  defaults defined here (``DEFAULT_SOURCES``) and optionally overridden by a YAML file.
* :class:`RunDefaults` (``run:`` in the YAML): the exercise itself (AOI, sources, start date,
  target grids, Gold back-end, static layers), so ``climate-pipeline sync --config X`` needs no
  other flag. Command-line options override it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from climate_pipeline.domain.errors import ConfigurationError


class Settings(BaseSettings):
    """Secrets and environment-dependent paths. Never stored in the repository."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    climate_data_root: Path = Path("data")
    # When unset, cdsapi falls back to ~/.cdsapirc.
    cdsapi_url: str | None = None
    cdsapi_key: SecretStr | None = None
    gee_project: str | None = None
    gee_service_account_key: Path | None = None
    mswep_service_account_key: Path | None = None
    mswep_oauth_client: Path | None = None
    mswep_token: Path = Path("data/auxiliary/tokens/mswep_token.json")
    #: postgresql://user:password@host:5432/db ; needed by --output-format postgres and `sync`.
    database_url: SecretStr | None = None
    database_schema: str = "climate"

    def database_url_value(self) -> str | None:
        return self.database_url.get_secret_value() if self.database_url else None


class LatencyConfig(BaseModel):
    """Publication latency of an upstream product, in days after the observation date."""

    final_lag_days: int = Field(ge=0)
    prelim_lag_days: int | None = Field(default=None, ge=0)
    lookback_days: int = Field(default=60, ge=1)
    reprocess_prelim: bool = True

    @property
    def earliest_lag_days(self) -> int:
        if self.prelim_lag_days is None:
            return self.final_lag_days
        return min(self.prelim_lag_days, self.final_lag_days)


class SourceConfig(BaseModel):
    variables: list[str] | None = None  # None → catalog defaults
    latency: LatencyConfig
    max_workers: int = Field(default=4, ge=1, le=16)
    options: dict[str, Any] = Field(default_factory=dict)


StaticLayer = Literal["terrain", "urban", "osm"]
GoldBackend = Literal["parquet", "duckdb", "postgres"]


class StaticDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layers: list[StaticLayer] = Field(default_factory=lambda: ["terrain", "urban", "osm"])
    #: Year of the urban composites; None → the year of the run's end date.
    year: int | None = Field(default=None, ge=2015)


class RunDefaults(BaseModel):
    """The exercise declared in YAML; every field can be overridden on the command line."""

    model_config = ConfigDict(extra="forbid")

    aoi_path: Path | None = None
    sources: list[str] = Field(default_factory=list)
    #: First day the database must hold; `sync` fills every gap from here to ``end_date``.
    start_date: date | None = None
    #: None → today (the operational, real-time setting).
    end_date: date | None = None
    #: Gold grids: ``{name: "<CRS>@<resolution>" | template raster}``.
    target_grids: dict[str, str] = Field(default_factory=dict)
    nested: bool = False
    output_format: GoldBackend = "parquet"
    static: StaticDefaults = Field(default_factory=StaticDefaults)
    #: `coverage` flags a source as stale when its last Gold day is this many days behind
    #: the last day the provider could have published (today − latency).
    max_behind_days: int = Field(default=3, ge=0)

    @field_validator("sources")
    @classmethod
    def _known(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(DEFAULT_SOURCES))
        if unknown:
            raise ValueError(f"unknown sources in run.sources: {unknown}")
        return value


class PipelineConfig(BaseModel):
    clip_mode: Literal["polygon", "bbox"] = "polygon"
    #: One column per physical quantity (see climate_pipeline.domain.redundancy).
    deduplicate: bool = True
    #: Sources ingested at the same time (each one also parallelises its own requests).
    parallel_sources: int = Field(default=4, ge=1, le=16)
    sources: dict[str, SourceConfig]
    run: RunDefaults = Field(default_factory=RunDefaults)

    @field_validator("sources")
    @classmethod
    def _known_sources(cls, value: dict[str, SourceConfig]) -> dict[str, SourceConfig]:
        unknown = set(value) - set(DEFAULT_SOURCES)
        if unknown:
            raise ValueError(f"unknown sources in configuration: {sorted(unknown)}")
        return value

    def source(self, name: str) -> SourceConfig:
        try:
            return self.sources[name]
        except KeyError as exc:
            raise ConfigurationError(f"source {name!r} is not configured") from exc


# Latencies observed on the providers' servers (see README, "Latencia de datos").
DEFAULT_SOURCES: dict[str, dict[str, Any]] = {
    "chirps": {
        # v3 prelim ("sat") ~7 days; final for month M appears around day 20 of M+1.
        "latency": {"prelim_lag_days": 7, "final_lag_days": 52, "lookback_days": 90},
        # CHC rate-limits aggressive clients (HTTP 403): keep concurrency low.
        "max_workers": 2,
        "options": {"version": "v3", "disaggregation": "rnl"},
    },
    "agera5": {
        "latency": {"final_lag_days": 8, "lookback_days": 45},
        "options": {"version": "2_0"},
    },
    "mswep": {
        "latency": {"prelim_lag_days": 2, "final_lag_days": 120, "lookback_days": 30},
        "options": {
            "folders": [
                {"id": "1gWoZ2bK2u5osJ8Iw-dvguZ56Kmz2QWrL", "status": "final"},
                {"id": "1aehP6YDNOO73ab3tvTZet2Sh5uPdG9I_", "status": "prelim"},
            ]
        },
    },
    "gee": {
        # MOD09GA/MYD09GA are usually ingested in Earth Engine 2-4 days after acquisition.
        "latency": {"final_lag_days": 4, "lookback_days": 30},
        "options": {"products": ["modis_terra_aqua_indices", "sentinel2_indices"], "cloud_mask": "standard"},
    },
    # --- daily operational sources added for the multi-source Gold (latencies observed on
    # 2026-11-08 with `climate-pipeline availability`) -------------------------------------
    "era5land": {  # ECMWF ERA5-Land daily aggregates (GEE), ~7 days behind
        "latency": {"final_lag_days": 7, "lookback_days": 30},
        "max_workers": 6,
    },
    # NASA GPM IMERG V07: provisional within ~2 days. The Final Run ("permanent" in Earth Engine)
    # lags ~13 months: on 2026-11-09 the last permanent day was 2025-09-30. A shorter final lag
    # would make every run re-download months of provisional days that cannot be upgraded yet.
    "imerg": {
        "latency": {"prelim_lag_days": 2, "final_lag_days": 420, "lookback_days": 30},
        "max_workers": 6,
        "options": {"utc_offset_hours": 0},
    },
    "gsmap": {  # JAXA GSMaP v8 operational (gauge calibrated), hourly, ~1-2 days
        "latency": {"final_lag_days": 2, "lookback_days": 30},
        "max_workers": 6,
        "options": {"utc_offset_hours": 0},
    },
    "modis": {  # MOD11A1/MYD11A1 LST and MOD09GA/MYD09GA cloud state, 2-3 days
        "latency": {"final_lag_days": 3, "lookback_days": 30},
        "max_workers": 6,
    },
    "viirs": {  # VIIRS Black Marble VNP46A2 daily night lights, ~6 days
        "latency": {"final_lag_days": 6, "lookback_days": 30},
        "max_workers": 6,
    },
    "smap": {  # SMAP L4 3-hourly soil moisture, ~3 days
        "latency": {"final_lag_days": 3, "lookback_days": 30},
        "max_workers": 6,
        "options": {"utc_offset_hours": 0},
    },
    "landsat": {  # Landsat 8/9 C2 L2, scenes appear ~1-2 weeks after acquisition
        "latency": {"final_lag_days": 7, "lookback_days": 60},
        "max_workers": 6,
    },
    "goes": {  # GOES-East ABI full disk every 10 min; a day is complete ~1 day later
        "latency": {"final_lag_days": 1, "lookback_days": 30},
        "max_workers": 4,
        "options": {"utc_offset_hours": 0},
    },
    "sentinel1": {  # Sentinel-1C/D GRD, ingested in Earth Engine within ~1-3 days
        "latency": {"final_lag_days": 3, "lookback_days": 30},
        "max_workers": 6,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_pipeline_config(path: Path | None = None) -> PipelineConfig:
    payload: dict[str, Any] = {"sources": DEFAULT_SOURCES}
    if path is not None:
        if not path.exists():
            raise ConfigurationError(f"configuration file not found: {path}")
        override = yaml.safe_load(path.read_text()) or {}
        if not isinstance(override, dict):
            raise ConfigurationError(f"configuration root must be a mapping: {path}")
        payload = _deep_merge(payload, override)
    try:
        return PipelineConfig.model_validate(payload)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
