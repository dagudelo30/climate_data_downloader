"""Runtime configuration.

* :class:`Settings`: secrets and paths from environment variables / ``.env``.
* :class:`PipelineConfig`: per-source behaviour (variables, latency, options), with
  defaults defined here and optionally overridden by a YAML file (``config/pipeline.yaml``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
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


class PipelineConfig(BaseModel):
    clip_mode: Literal["polygon", "bbox"] = "polygon"
    #: One column per physical quantity (see climate_pipeline.domain.redundancy).
    deduplicate: bool = True
    sources: dict[str, SourceConfig]

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
    "imerg": {  # NASA GPM IMERG V07: provisional within ~2 days, final run ~3.5 months later
        "latency": {"prelim_lag_days": 2, "final_lag_days": 120, "lookback_days": 150},
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
