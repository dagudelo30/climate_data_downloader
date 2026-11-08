"""Value objects exchanged between extractors, storage and the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path


class ProductStatus(StrEnum):
    """Maturity of an upstream product for a given date."""

    FINAL = "final"
    PRELIM = "prelim"
    MISSING = "missing"

    @property
    def rank(self) -> int:
        """Lower is worse; used to aggregate the status of several variables."""
        return {ProductStatus.MISSING: 0, ProductStatus.PRELIM: 1, ProductStatus.FINAL: 2}[self]

    @classmethod
    def worst(cls, statuses: list[ProductStatus]) -> ProductStatus:
        if not statuses:
            return cls.MISSING
        return min(statuses, key=lambda status: status.rank)


class Layer(StrEnum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"


@dataclass(frozen=True)
class BronzeArtifact:
    """A raw file successfully persisted in the Bronze layer."""

    source: str
    variable: str
    day: date
    path: Path
    status: ProductStatus
    origin: str


@dataclass(frozen=True)
class MissingData:
    """Upstream data that is not published yet (expected latency, not a failure)."""

    source: str
    variable: str
    day: date
    reason: str


ExtractionResult = BronzeArtifact | MissingData
