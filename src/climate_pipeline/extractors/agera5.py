"""AgERA5 (``sis-agrometeorological-indicators``) through the Copernicus CDS API.

One CDS request per ``(variable, month)`` with only the pending days of that month and
the AOI bounding box, submitted concurrently because each request waits in the CDS
queue for a minute or more. Every returned daily NetCDF becomes one Bronze file.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import cdsapi
from pydantic import BaseModel

from climate_pipeline.domain.artifacts import MissingData, ProductStatus
from climate_pipeline.domain.dates import group_by_month
from climate_pipeline.domain.variables import VariableSpec
from climate_pipeline.extractors.base import (
    BaseExtractor,
    ExtractionFailure,
    ExtractionOutcome,
    PendingBronze,
    register_extractor,
)
from climate_pipeline.processing.harmonize import select_data_variable
from climate_pipeline.storage.netcdf import read_netcdf

_FILE_DATE = re.compile(r"_(\d{8})_")
_UNAVAILABLE_HINTS = (
    "no data",
    "not available",
    "no matching data",
    "none of the data",
    "is not yet available",
    "out of range",
)


class Agera5Options(BaseModel):
    dataset: str = "sis-agrometeorological-indicators"
    version: str = "2_0"
    area_margin_deg: float = 0.2


@dataclass(frozen=True)
class MonthlyRequest:
    spec: VariableSpec
    year: int
    month: int
    days: tuple[date, ...]


def build_request(task: MonthlyRequest, area: list[float], version: str) -> dict[str, Any]:
    """CDS request body for one variable and one month (only the requested days)."""
    request: dict[str, Any] = {
        "variable": task.spec.request["variable"],
        "year": [f"{task.year}"],
        "month": [f"{task.month:02d}"],
        "day": [f"{day.day:02d}" for day in task.days],
        "version": version,
        "area": area,
    }
    if "statistic" in task.spec.request:
        request["statistic"] = [task.spec.request["statistic"]]
    if "time" in task.spec.request:
        request["time"] = [task.spec.request["time"]]
    return request


def is_unavailable_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(hint in message for hint in _UNAVAILABLE_HINTS)


def parse_file_date(path: Path) -> date | None:
    match = _FILE_DATE.search(path.name)
    return datetime.strptime(match.group(1), "%Y%m%d").date() if match else None


@register_extractor
class Agera5Extractor(BaseExtractor):
    name = "agera5"

    def __init__(self, *args: Any, client_factory: Callable[[], Any] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client_factory = client_factory or self._default_client

    def _default_client(self) -> Any:
        kwargs: dict[str, Any] = {"quiet": True, "progress": False}
        if self.settings.cdsapi_url:
            kwargs["url"] = self.settings.cdsapi_url
        if self.settings.cdsapi_key:
            kwargs["key"] = self.settings.cdsapi_key.get_secret_value()
        return cdsapi.Client(**kwargs)

    @property
    def options(self) -> Agera5Options:
        return Agera5Options.model_validate(self.config.options)

    def fetch(self, requests: Mapping[str, Sequence[date]]) -> Iterator[ExtractionOutcome]:
        options = self.options
        area = self.aoi.cds_area(options.area_margin_deg)
        tasks = [
            MonthlyRequest(self.variables[name], year, month, tuple(days))
            for name, dates in requests.items()
            for (year, month), days in group_by_month(dates).items()
        ]

        def worker(task: MonthlyRequest) -> list[ExtractionOutcome]:
            body = build_request(task, area, options.version)
            with tempfile.TemporaryDirectory(dir=self.lake.tmp) as workdir:
                archive = Path(workdir) / "response.zip"
                try:
                    self._client_factory().retrieve(options.dataset, body, str(archive))
                except Exception as exc:
                    if is_unavailable_error(exc):
                        return [
                            MissingData(self.name, task.spec.name, day, f"CDS: {exc}") for day in task.days
                        ]
                    raise
                return self._unpack(task, archive, Path(workdir), body)

        def on_error(task: MonthlyRequest, exc: Exception) -> list[ExtractionOutcome]:
            return [ExtractionFailure(self.name, task.spec.name, day, repr(exc)) for day in task.days]

        yield from self.run_parallel(tasks, worker, on_error)

    def _unpack(
        self, task: MonthlyRequest, archive: Path, workdir: Path, body: dict[str, Any]
    ) -> list[ExtractionOutcome]:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                members = [name for name in bundle.namelist() if name.endswith(".nc")]
                for member in members:
                    bundle.extract(member, workdir)
            files = [workdir / member for member in members]
        else:  # a single NetCDF is returned for one-day requests by some CDS versions
            files = [archive.rename(archive.with_suffix(".nc"))]

        outcomes: list[ExtractionOutcome] = []
        received: set[date] = set()
        requested = set(task.days)
        origin = (
            f"cds:{self.options.dataset}:{body['variable']}:{body.get('statistic') or body.get('time') or ''}"
        )
        for path in files:
            day = parse_file_date(path)
            dataset = read_netcdf(path)
            if day is None and "time" in dataset.coords:
                day = dataset["time"].dt.date.values.flatten()[0]
            if day is None or day not in requested:
                continue
            status = ProductStatus.PRELIM if "prelim" in path.name.lower() else ProductStatus.FINAL
            array = select_data_variable(dataset)
            outcomes.append(PendingBronze(array, task.spec, day, status, origin))
            received.add(day)
        outcomes.extend(
            MissingData(self.name, task.spec.name, day, "not returned by CDS")
            for day in sorted(requested - received)
        )
        return outcomes
