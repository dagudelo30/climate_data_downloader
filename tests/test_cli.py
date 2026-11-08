import json

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
