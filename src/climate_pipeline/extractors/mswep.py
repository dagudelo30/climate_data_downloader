"""MSWEP daily precipitation (GloH2O), distributed through shared Google Drive folders.

Access must be requested at https://www.gloh2o.org/mswep/. Daily files are named
``YYYYDDD.nc`` (year + day of year). Folders are searched in the configured order and
each folder declares the product status it holds (``Past`` → final, ``NRT`` → prelim).

Authentication (``Settings``):

* ``MSWEP_SERVICE_ACCOUNT_KEY``: service account JSON (CI); the Drive folders must be
  shared with the service account e-mail;
* ``MSWEP_OAUTH_CLIENT`` + ``MSWEP_TOKEN``: desktop OAuth flow for local use.

The Google API client is not thread-safe, so downloads run sequentially.
"""

from __future__ import annotations

import io
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from climate_pipeline.domain.artifacts import MissingData, ProductStatus
from climate_pipeline.domain.errors import ConfigurationError
from climate_pipeline.extractors.base import (
    BaseExtractor,
    ExtractionFailure,
    ExtractionOutcome,
    register_extractor,
)
from climate_pipeline.processing.harmonize import select_data_variable
from climate_pipeline.processing.spatial import standardize_spatial_dims, subset_to_bounds
from climate_pipeline.storage.netcdf import read_netcdf

_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


class MswepFolder(BaseModel):
    id: str
    status: ProductStatus


class MswepOptions(BaseModel):
    folders: list[MswepFolder]
    margin_cells: int = 2


def mswep_file_name(day: date) -> str:
    return f"{day.year}{day.timetuple().tm_yday:03d}.nc"


@register_extractor
class MswepExtractor(BaseExtractor):
    name = "mswep"
    single_variable = True

    def __init__(self, *args: Any, drive_service: Any | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._service = drive_service

    @property
    def options(self) -> MswepOptions:
        return MswepOptions.model_validate(self.config.options)

    def _drive(self) -> Any:
        if self._service is None:
            self._service = build_drive_service(
                service_account_key=self.settings.mswep_service_account_key,
                oauth_client=self.settings.mswep_oauth_client,
                token_path=self.settings.mswep_token,
            )
        return self._service

    def _find(self, folder_id: str, file_name: str) -> str | None:
        response = (
            self._drive()
            .files()
            .list(
                q=f"name = '{file_name}' and '{folder_id}' in parents and trashed = false",
                fields="files(id, name)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
        files = response.get("files", [])
        return files[0]["id"] if files else None

    def _download(self, file_id: str, destination: Path) -> None:
        from googleapiclient.http import MediaIoBaseDownload

        request = self._drive().files().get_media(fileId=file_id, supportsAllDrives=True)
        with io.FileIO(destination, "wb") as handle:
            downloader = MediaIoBaseDownload(handle, request, chunksize=16 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=3)

    def fetch(self, requests: Mapping[str, Sequence[date]]) -> Iterator[ExtractionOutcome]:
        options = self.options
        spec = next(iter(self.variables.values()))
        self._drive()  # fail fast on missing credentials, before iterating dates
        for day in sorted({day for dates in requests.values() for day in dates}):
            try:
                yield self._fetch_day(day, spec, options)
            except Exception as exc:
                yield ExtractionFailure(self.name, spec.name, day, repr(exc))

    def _fetch_day(self, day: date, spec: Any, options: MswepOptions) -> ExtractionOutcome:
        file_name = mswep_file_name(day)
        for folder in options.folders:
            file_id = self._find(folder.id, file_name)
            if file_id is None:
                continue
            with tempfile.TemporaryDirectory(dir=self.lake.tmp) as workdir:
                local = Path(workdir) / file_name
                self._download(file_id, local)
                array = standardize_spatial_dims(select_data_variable(read_netcdf(local), "precipitation"))
                array = subset_to_bounds(array, self.aoi.bounds, buffer_cells=options.margin_cells)
                return self.write_bronze(array, spec, day, folder.status, f"gdrive:{folder.id}/{file_name}")
        return MissingData(self.name, spec.name, day, f"{file_name} not found in configured folders")


def build_drive_service(service_account_key: Path | None, oauth_client: Path | None, token_path: Path) -> Any:
    from googleapiclient.discovery import build

    if service_account_key is not None:
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            str(service_account_key), scopes=_SCOPES
        )
    elif oauth_client is not None:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        credentials = None
        if token_path.exists():
            credentials = Credentials.from_authorized_user_file(str(token_path), _SCOPES)
        if credentials is None or not credentials.valid:
            if credentials is not None and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(oauth_client), _SCOPES)
                credentials = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(credentials.to_json())
    else:
        raise ConfigurationError("MSWEP needs MSWEP_SERVICE_ACCOUNT_KEY or MSWEP_OAUTH_CLIENT (see README)")
    return build("drive", "v3", credentials=credentials, cache_discovery=False)
