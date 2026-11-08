"""Data quality control.

Three levels of checks, applied at increasing levels of integration:

1. **Physical bounds** per variable (Silver): impossible values become NaN, values within
   a small tolerance band are clipped. Example: NDVI = 1.7 → NaN; RH = 100.4 % → 100 %.
2. **Cross-variable consistency** per Gold row: e.g. ``Tmin <= Tmean <= Tmax``. Rows that
   violate a rule beyond its tolerance have the involved values set to NaN, because we
   cannot know which of them is wrong.
3. **Structural invariants** on Gold tables (unique keys, required columns, values
   within bounds after QC). These raise :class:`QualityError`: they indicate a bug, not
   bad upstream data, and must stop the pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import xarray as xr

from climate_pipeline.domain.errors import QualityError
from climate_pipeline.domain.variables import QualityBounds, VariableCatalog


@dataclass
class BoundsReport:
    """Outcome of applying :class:`QualityBounds` to one array."""

    variable: str
    total_values: int = 0
    valid_values: int = 0
    non_finite: int = 0
    below_minimum: int = 0
    above_maximum: int = 0
    clipped: int = 0

    @property
    def rejected(self) -> int:
        return self.non_finite + self.below_minimum + self.above_maximum

    def merge(self, other: BoundsReport) -> BoundsReport:
        return BoundsReport(
            variable=self.variable,
            total_values=self.total_values + other.total_values,
            valid_values=self.valid_values + other.valid_values,
            non_finite=self.non_finite + other.non_finite,
            below_minimum=self.below_minimum + other.below_minimum,
            above_maximum=self.above_maximum + other.above_maximum,
            clipped=self.clipped + other.clipped,
        )


def apply_bounds(
    values: xr.DataArray, bounds: QualityBounds, variable: str
) -> tuple[xr.DataArray, BoundsReport]:
    """Mask impossible values and clip values inside the tolerance band.

    NaN inputs (e.g. outside the AOI) are not counted as rejections.
    """
    data = np.asarray(values.values, dtype="float64")
    present = ~np.isnan(data)
    finite = np.isfinite(data)
    non_finite = present & ~finite  # +/- inf, typically a 0/0 or x/0 in index formulas

    lower_hard = bounds.minimum - bounds.tolerance
    upper_hard = bounds.maximum + bounds.tolerance
    with np.errstate(invalid="ignore"):
        below = finite & (data < lower_hard)
        above = finite & (data > upper_hard)
        clip_low = finite & (data >= lower_hard) & (data < bounds.minimum)
        clip_high = finite & (data <= upper_hard) & (data > bounds.maximum)

    cleaned = data.copy()
    cleaned[non_finite | below | above] = np.nan
    cleaned[clip_low] = bounds.minimum
    cleaned[clip_high] = bounds.maximum

    report = BoundsReport(
        variable=variable,
        total_values=int(present.sum()),
        valid_values=int((~np.isnan(cleaned)).sum()),
        non_finite=int(non_finite.sum()),
        below_minimum=int(below.sum()),
        above_maximum=int(above.sum()),
        clipped=int(clip_low.sum() + clip_high.sum()),
    )
    return values.copy(data=cleaned), report


@dataclass(frozen=True)
class ConsistencyRule:
    """``lower <= upper + tolerance`` must hold wherever both columns are present."""

    lower: str
    upper: str
    tolerance: float = 0.0
    description: str = ""

    @property
    def label(self) -> str:
        return self.description or f"{self.lower} <= {self.upper}"


DEFAULT_CONSISTENCY_RULES: tuple[ConsistencyRule, ...] = (
    ConsistencyRule("agera5_temp_min", "agera5_temp_mean", 0.5, "Tmin <= Tmean"),
    ConsistencyRule("agera5_temp_mean", "agera5_temp_max", 0.5, "Tmean <= Tmax"),
    ConsistencyRule("agera5_temp_min", "agera5_temp_max", 0.5, "Tmin <= Tmax"),
    ConsistencyRule("agera5_dewpoint_mean", "agera5_temp_mean", 0.5, "Tdew <= Tmean"),
    ConsistencyRule("era5land_temp_min", "era5land_temp_mean", 0.5, "ERA5-Land Tmin <= Tmean"),
    ConsistencyRule("era5land_temp_mean", "era5land_temp_max", 0.5, "ERA5-Land Tmean <= Tmax"),
    ConsistencyRule("era5land_dewpoint_mean", "era5land_temp_mean", 0.5, "ERA5-Land Tdew <= Tmean"),
)


@dataclass
class ConsistencyReport:
    violations: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.violations.values())


def enforce_consistency(
    frame: pd.DataFrame, rules: Iterable[ConsistencyRule] = DEFAULT_CONSISTENCY_RULES
) -> tuple[pd.DataFrame, ConsistencyReport]:
    """Null out both members of every violated rule (rules on absent columns are skipped)."""
    result = frame.copy()
    report = ConsistencyReport()
    # Evaluate every rule on the original values so the order of rules does not matter.
    to_null: dict[str, np.ndarray] = {}
    for rule in rules:
        if rule.lower not in frame.columns or rule.upper not in frame.columns:
            continue
        lower = frame[rule.lower].to_numpy(dtype="float64")
        upper = frame[rule.upper].to_numpy(dtype="float64")
        with np.errstate(invalid="ignore"):
            violated = (lower > upper + rule.tolerance) & ~np.isnan(lower) & ~np.isnan(upper)
        report.violations[rule.label] = int(violated.sum())
        for column in (rule.lower, rule.upper):
            to_null[column] = to_null.get(column, np.zeros(len(frame), bool)) | violated
    for column, mask in to_null.items():
        if mask.any():
            result.loc[mask, column] = np.nan
    return result, report


def check_unique_keys(frame: pd.DataFrame, keys: Sequence[str], table: str) -> None:
    missing = [key for key in keys if key not in frame.columns]
    if missing:
        raise QualityError(f"{table}: missing key columns {missing}")
    duplicated = frame.duplicated(subset=list(keys))
    if duplicated.any():
        sample = frame.loc[duplicated, list(keys)].head(3).to_dict("records")
        raise QualityError(f"{table}: {int(duplicated.sum())} duplicated keys, e.g. {sample}")


def check_bounds(frame: pd.DataFrame, catalog: VariableCatalog, table: str) -> None:
    """Every catalog column must lie within its physical bounds (post-QC invariant)."""
    for column in frame.columns:
        if not catalog.has_gold_column(column):
            continue
        spec = catalog.by_gold_column(column)
        values = frame[column].to_numpy(dtype="float64")
        valid = values[~np.isnan(values)]
        if valid.size == 0:
            continue
        if not np.isfinite(valid).all():
            raise QualityError(f"{table}.{column}: contains infinite values")
        if table.endswith("epiweekly") and spec.weekly_aggregation.value == "sum":
            low, high = spec.bounds.minimum, spec.bounds.maximum * 7
        else:
            low, high = spec.bounds.minimum, spec.bounds.maximum
        out = (valid < low) | (valid > high)
        if out.any():
            raise QualityError(
                f"{table}.{column}: {int(out.sum())} values outside [{low}, {high}] "
                f"(min={valid.min():.3f}, max={valid.max():.3f})"
            )
