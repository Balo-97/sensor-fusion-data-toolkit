import hashlib
import json
from unittest.mock import patch

from PIL import Image

from scenario_metadata.benchmark import run_benchmark


def test_real_annotation_checks_and_failure_propagation(tmp_path):
    root = tmp_path / "data" / "fixture"
    root.mkdir(parents=True)
    image = root / "tiny.png"
    Image.new("RGB", (17, 11)).save(image)
    csv = root / "data.csv"
    csv.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    manifest = {"datasets": [{"id": "fixture", "title": "Synthetic unit fixture", "source_url": "https://example.org",
                             "scope": "unit test only", "license": "test", "files": []}]}
    for path, expected in [(image, {"width": 17, "height": 11, "bit_depth": 8}),
                           (csv, {"rows": 2, "columns": 2, "headers": ["a", "b"]})]:
        manifest["datasets"][0]["files"].append({"path": path.name, "url": "https://example.org/" + path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size,
            "expected": expected})
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with patch("scenario_metadata.downloads.urlopen", side_effect=AssertionError("Should use verified cache")):
        report = run_benchmark(tmp_path / "data", tmp_path / "out", manifest_path=manifest_path)
    assert report["status"] == "passed"
    assert report["files_passed"] == 2
    assert report["original_npl_tool_tested"] is False
    manifest["datasets"][0]["files"][0]["expected"]["width"] = 999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = run_benchmark(tmp_path / "data", tmp_path / "bad", manifest_path=manifest_path)
    assert report["status"] == "failed"
    assert report["files_passed"] == 1
    assert report["datasets_passed"] == 0
    assert json.loads((tmp_path / "bad" / "benchmark_report.json").read_text())["status"] == "failed"
