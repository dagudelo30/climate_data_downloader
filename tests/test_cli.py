import json

import pytest
from typer.testing import CliRunner

from climate_pipeline.cli import app

runner = CliRunner()


def test_variables_lists_catalog_with_bounds() -> None:
    result = runner.invoke(app, ["variables"])
    assert result.exit_code == 0
    assert "agera5_temp_mean" in result.output and "degC" in result.output
    assert "gee_ndvi" in result.output and "[-1, 1]" in result.output
    assert "weekly=sum(>=7d)" in result.output


def test_plan_outputs_github_matrix(aoi_file, tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "plan",
            "--aoi-path",
            str(aoi_file),
            "--sources",
            "imerg,agera5,chirps",
            "--today",
            "2026-10-27",
            "--data-root",
            str(tmp_path / "lake"),
            "--github-matrix",
        ],
    )
    assert result.exit_code == 0, result.output
    # chirps has no column of its own by default: IMERG measures the same with 2 days of latency.
    assert json.loads(result.output.strip().splitlines()[-1]) == {"source": ["imerg", "agera5"]}


def test_plan_json_respects_latency(aoi_file, tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "plan",
            "--aoi-path",
            str(aoi_file),
            "--sources",
            "agera5",
            "--no-incremental",
            "--start-date",
            "2026-10-01",
            "--end-date",
            "2026-10-27",
            "--today",
            "2026-10-27",
            "--data-root",
            str(tmp_path / "lake"),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["agera5"]["window_end"] == "2026-10-19"


def test_unknown_source_is_a_usage_error(aoi_file, tmp_path) -> None:
    result = runner.invoke(
        app,
        ["plan", "--aoi-path", str(aoi_file), "--sources", "era6", "--data-root", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "unknown sources" in result.output


def test_make_raster_without_gold_fails_cleanly(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "make-raster",
            "--grid-name",
            "1km",
            "--variable",
            "chirps_precip",
            "--day",
            "2026-01-01",
            "--output",
            str(tmp_path / "x.tif"),
            "--data-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0


def _config(tmp_path, aoi_file, **extra) -> str:
    path = tmp_path / "pipeline.yaml"
    lines = [
        "run:",
        f"  aoi_path: {aoi_file}",
        "  sources: [imerg, agera5]",
        "  start_date: 2026-10-01",
        "  target_grids: {1km: 'EPSG:9377@1000'}",
        *[f"  {key}: {value}" for key, value in extra.items()],
    ]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def test_plan_reads_the_declared_exercise(aoi_file, tmp_path) -> None:
    config = _config(tmp_path, aoi_file)
    result = runner.invoke(
        app,
        [
            "plan",
            "--config",
            config,
            "--no-incremental",
            "--today",
            "2026-10-27",
            "--data-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"imerg", "agera5"}  # run.sources, no --aoi-path needed
    assert payload["imerg"]["window_start"] == "2026-10-01"  # run.start_date
    assert payload["imerg"]["window_end"] == "2026-10-25"  # today − 2 days of IMERG latency


def test_coverage_on_an_empty_lake_reports_every_day_as_a_gap(aoi_file, tmp_path) -> None:
    config = _config(tmp_path, aoi_file)
    report = tmp_path / "coverage.json"
    arguments = ["coverage", "--config", config, "--today", "2026-10-27", "--data-root", str(tmp_path)]
    result = runner.invoke(app, [*arguments, "--report-path", str(report)])
    assert result.exit_code == 0, result.output
    assert "not_requested" in result.output
    sources = {item["source"]: item for item in json.loads(report.read_text())["1km"]["sources"]}
    assert sources["imerg"]["states"] == {"not_requested": 25} and sources["imerg"]["stale"]
    assert sources["agera5"]["gaps"] == [["2026-10-01", "2026-10-19"]]
    assert runner.invoke(app, [*arguments, "--fail-on-gaps"]).exit_code == 1


def test_sync_requires_a_start_date(aoi_file, tmp_path) -> None:
    path = tmp_path / "pipeline.yaml"
    path.write_text(f"run: {{aoi_path: {aoi_file}, target_grids: {{1km: 'EPSG:9377@1000'}}}}\n")
    result = runner.invoke(app, ["sync", "--config", str(path), "--data-root", str(tmp_path)])
    assert result.exit_code == 2 and "start-date" in result.output


@pytest.mark.postgres
def test_db_commands(pg_database, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", pg_database.url)
    monkeypatch.setenv("DATABASE_SCHEMA", pg_database.schema)
    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0, result.output
    assert "migrations applied now none" in result.output and "variables" in result.output
    status = runner.invoke(app, ["db", "status"])
    assert status.exit_code == 0 and "== freshness" in status.output
    selected = pg_database.connection().execute("SELECT count(*) FROM variable WHERE selected").fetchone()[0]
    assert selected == 73


def test_postgres_without_database_url_is_a_clean_error(aoi_file, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    result = runner.invoke(
        app, ["plan", "--aoi-path", str(aoi_file), "--sources", "imerg", "--output-format", "postgres"]
    )
    assert result.exit_code == 2 and "DATABASE_URL" in result.output
