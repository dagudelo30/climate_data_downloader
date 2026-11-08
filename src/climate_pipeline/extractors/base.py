"""Extractor contract and shared helpers.

An extractor turns ``{variable_name: [dates]}`` requests into Bronze files and yields
one result per ``(variable, date)``:

* :class:`BronzeArtifact` when data was persisted,
* :class:`MissingData` when the provider has not published it (expected latency),
* :class:`ExtractionFailure` for unexpected errors (network, auth, corrupt file...).

Failures never abort the other dates of the batch; the orchestrator records them and
the CLI exits non-zero at the end of the run.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import ClassVar, TypeVar

import httpx
import numpy as np
import xarray as xr
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from climate_pipeline.config import Settings, SourceConfig
from climate_pipeline.domain.aoi import AreaOfInterest
from climate_pipeline.domain.artifacts import BronzeArtifact, MissingData, ProductStatus
from climate_pipeline.domain.errors import DataNotAvailableError
from climate_pipeline.domain.variables import VariableSpec
from climate_pipeline.processing.spatial import standardize_spatial_dims
from climate_pipeline.storage.layout import DataLake
from climate_pipeline.storage.netcdf import write_netcdf_atomic

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class ExtractionFailure:
    source: str
    variable: str
    day: date
    error: str


@dataclass(frozen=True)
class PendingBronze:
    """Downloaded data not yet written.

    Worker threads only download; :meth:`BaseExtractor.run_parallel` writes Bronze in the
    consuming (main) thread. HDF5 enables automatic error printing per thread, and the
    netCDF library only disables it in the thread that initialised it, so creating NetCDF
    files from worker threads floods the logs with spurious HDF5 diagnostics.
    """

    data: xr.DataArray
    spec: VariableSpec
    day: date
    status: ProductStatus
    origin: str


ExtractionOutcome = BronzeArtifact | MissingData | ExtractionFailure
WorkerOutcome = ExtractionOutcome | PendingBronze


class RequestThrottle:
    """Thread-safe minimum spacing between requests to a rate-limited host."""

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval_s = min_interval_s
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        if self.min_interval_s <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.min_interval_s
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


EXTRACTORS: dict[str, type[BaseExtractor]] = {}


def register_extractor(cls: type[BaseExtractor]) -> type[BaseExtractor]:
    EXTRACTORS[cls.name] = cls
    return cls


def _is_transient_http_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    # 403/429 are what rate-limited public mirrors (e.g. CHC) answer under load.
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code >= 500 or exc.response.status_code in (403, 429)
    )


http_retry = retry(
    retry=retry_if_exception(_is_transient_http_error),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    reraise=True,
)


class BaseExtractor(ABC):
    name: ClassVar[str]
    #: Bronze keeps one file per variable unless the source has a single variable.
    single_variable: ClassVar[bool] = False

    def __init__(
        self,
        lake: DataLake,
        aoi: AreaOfInterest,
        config: SourceConfig,
        settings: Settings,
        variables: Sequence[VariableSpec],
        http_client: httpx.Client | None = None,
    ) -> None:
        self.lake = lake
        self.aoi = aoi
        self.config = config
        self.settings = settings
        self.variables = {spec.name: spec for spec in variables}
        self._http = http_client or httpx.Client(timeout=120.0, follow_redirects=True)

    @abstractmethod
    def fetch(self, requests: Mapping[str, Sequence[date]]) -> Iterator[ExtractionOutcome]:
        """Download the requested ``{variable: dates}`` into Bronze."""

    def close(self) -> None:
        self._http.close()

    # --- helpers --------------------------------------------------------------------
    @http_retry
    def http_exists(self, url: str) -> bool:
        """``True`` on 2xx, ``False`` on 404/410; transient errors are retried."""
        response = self._http.head(url)
        if response.status_code in (404, 410):
            return False
        response.raise_for_status()
        return True

    @http_retry
    def http_get(self, url: str) -> bytes:
        response = self._http.get(url)
        if response.status_code in (404, 410):
            raise DataNotAvailableError(f"{url} returned {response.status_code}")
        response.raise_for_status()
        return response.content

    def write_bronze(
        self,
        data: xr.DataArray,
        spec: VariableSpec,
        day: date,
        status: ProductStatus,
        origin: str,
    ) -> BronzeArtifact:
        """Persist one raw variable/day with provenance attributes."""
        array = standardize_spatial_dims(data)
        if array.dtype.kind == "f" and not np.isfinite(array.values).any():
            # A fully-masked array is not an error but carries no information either.
            logger.debug("%s %s %s: all values are NaN", self.name, spec.name, day)
        array.attrs.update(
            {
                "source": self.name,
                "variable": spec.name,
                "date": day.isoformat(),
                "product_status": status.value,
                "origin": origin,
                "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        path = self.lake.bronze_path(self.name, day, None if self.single_variable else spec.name)
        write_netcdf_atomic(array.rename(array.name or spec.name), path)
        return BronzeArtifact(
            source=self.name, variable=spec.name, day=day, path=path, status=status, origin=origin
        )

    def run_parallel(
        self,
        tasks: Iterable[T],
        worker: Callable[[T], list[WorkerOutcome]],
        on_error: Callable[[T, Exception], list[ExtractionOutcome]],
    ) -> Iterator[ExtractionOutcome]:
        """Run ``worker`` over ``tasks`` with ``config.max_workers`` threads.

        Downloads happen in the pool; :class:`PendingBronze` results are written here, in
        the consuming thread.
        """
        task_list = list(tasks)
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            futures = {pool.submit(worker, task): task for task in task_list}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    results = future.result()
                    written = [self._materialize(result) for result in results]
                except Exception as exc:  # isolate failures per task
                    logger.exception("%s: task %s failed", self.name, task)
                    yield from on_error(task, exc)
                    continue
                yield from written

    def _materialize(self, result: WorkerOutcome) -> ExtractionOutcome:
        if isinstance(result, PendingBronze):
            return self.write_bronze(result.data, result.spec, result.day, result.status, result.origin)
        return result
