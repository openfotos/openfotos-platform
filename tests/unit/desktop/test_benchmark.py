from pathlib import Path

from PIL import Image

from openfotos_desktop.benchmark_inventory import run_benchmark


def test_benchmark_report_is_repeatable_and_redacted(tmp_path: Path) -> None:
    source = tmp_path / "private-event-folder"
    source.mkdir()
    Image.new("RGB", (4, 4), color="blue").save(source / "private-name.jpg", format="JPEG")

    report = run_benchmark(source, tmp_path / "benchmark.sqlite3")

    assert report["format"] == "openfotos-inventory-benchmark-v1"
    assert report["accepted_count"] == 1
    assert report["blocking_issue_count"] == 0
    assert "private-event-folder" not in str(report)
    assert "private-name.jpg" not in str(report)
