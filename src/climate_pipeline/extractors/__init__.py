"""Source extractors (upstream → Bronze)."""

# Import modules for their registration side effect.
from climate_pipeline.extractors import (  # noqa: F401
    agera5,
    chirps,
    gee,
    gee_datasets,
    mswep,
)
from climate_pipeline.extractors.base import EXTRACTORS, BaseExtractor, register_extractor

__all__ = ["EXTRACTORS", "BaseExtractor", "register_extractor"]
