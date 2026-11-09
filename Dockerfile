# climate-pipeline image: `docker run ghcr.io/<owner>/climate-pipeline sync --config ...`
# rasterio, pyogrio, netCDF4 and psycopg ship binary wheels (GDAL/HDF5/libpq bundled); the GDAL
# wheel only needs the system libexpat.
FROM ghcr.io/astral-sh/uv:0.9.15-python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/* && useradd --create-home --uid 1000 pipeline
COPY --from=build --chown=pipeline:pipeline /app/.venv /app/.venv
WORKDIR /work
COPY --chown=pipeline:pipeline examples ./examples
COPY --chown=pipeline:pipeline data/auxiliary/shapefiles ./data/auxiliary/shapefiles
USER pipeline
ENV PATH="/app/.venv/bin:$PATH" CLIMATE_DATA_ROOT=/work/data PYTHONUNBUFFERED=1
ENTRYPOINT ["climate-pipeline"]
CMD ["--help"]
