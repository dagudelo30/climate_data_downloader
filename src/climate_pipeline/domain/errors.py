"""Exception hierarchy.

The split between *expected* and *unexpected* failures is what keeps scheduled runs
green: upstream latency raises :class:`DataNotAvailableError` and is recorded as
``missing`` in the catalog, while anything else bubbles up as a real failure.
"""


class ClimatePipelineError(Exception):
    """Base class for every error raised by the pipeline."""


class ConfigurationError(ClimatePipelineError):
    """Invalid user input or configuration (bad AOI, unknown source, bad grid spec...)."""


class DataNotAvailableError(ClimatePipelineError):
    """The upstream provider has not published data for the requested date (yet)."""


class EmptySelectionError(ClimatePipelineError):
    """The area of interest does not intersect the dataset extent."""


class GridMismatchError(ClimatePipelineError):
    """The requested target grid differs from the grid the Gold store was built with."""


class QualityError(ClimatePipelineError):
    """A dataset violates a structural invariant (duplicated keys, missing columns...)."""
