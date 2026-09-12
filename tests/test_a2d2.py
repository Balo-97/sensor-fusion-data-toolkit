import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from scenario_metadata.a2d2_download import BASE, download_a2d2, selection


@pytest.fixture
def paired(tmp_path):
    np = pytest.importorskip("numpy")
    root = tmp_path / "dataset"
    root.mkdir()
    view = {"origin": [0, 0, 0], "x-axis": [1, 0, 0], "y-axis": [0, 1, 0]}
    matrix = [[10, 0, 8], [0, 10, 6], [0, 0, 1]]
    config = {"vehicle": {"view": view}, "cameras": {"front_center": {"view": view,
        "CamMatrix": matrix, "CamMatrixOriginal": matrix, "Distortion": [[0, 0, 0, 0, 0]],
        "Resolution": [16, 12], "Lens": "Telecam", "tstamp_delay": 25000}},
        "lidars": {"front_center": {"view": view}, "front_left": {"view": view}}}
    (root / "cams_lidars.json").write_text(json.dumps(config))
    (root / "LICENSE.txt").write_text("fixture")
    (root / "README.txt").write_text("fixture")
    pair = selection("20180810_150607", "front_center", 60, 1)[0]
    for key in ("image", "sidecar", "lidar"):
        (root / pair[key]).parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 12), "green").save(root / pair["image"])
    (root / pair["sidecar"]).write_text(json.dumps({"cam_name": "front_center", "cam_tstamp": 100_000_000,
        "image_png": Path(pair["image"]).name, "pcld_npz": Path(pair["lidar"]).name,
        "pcld_view": view, "lidar_ids": {"0": "front_center", "1": "front_left"}}))
    arrays = {"points": np.arange(12, dtype=float).reshape(4, 3), "reflectance": np.array([1, 2, 3, 4]),
        "row": np.array([1., 2., 3., 4.]), "col": np.array([2., 3., 4., 5.]),
        "distance": np.array([1., 2., 3., 4.]), "depth": np.array([1., 2., 3., 4.]),
        "timestamp": np.array([62_999_990, 62_999_995, 63_000_000, 63_000_005], dtype="int64"),
        "rectime": np.array([99_999_990, 99_999_995, 100_000_000, 100_000_005], dtype="int64"),
        "lidar_id": np.array([0, 1, 0, 1]), "valid": np.array([True, True, False, True])}
    np.savez_compressed(root / pair["lidar"], **arrays)
    return root, pair, arrays


def test_pair_metadata_and_clock_preservation(paired, tmp_path):
    from scenario_metadata.a2d2 import annotate_a2d2
    from scenario_metadata.downloads import sha256_file
    from xml.etree import ElementTree as ET
    root, pair, _ = paired
    output = tmp_path / "out"
    report = annotate_a2d2(root, output, max_frames=1)
    assert report["status"] == "passed" and report["total_point_rows"] == 4
    assert report["observed_physical_lidar_sensors"] == ["front_center", "front_left"]
    frame = report["pairs"][0]
    assert frame["timing"]["rectime_minus_camera_us"]["min"] == -10
    assert frame["timing"]["rectime_minus_camera_us"]["max"] == 5
    assert frame["timing"]["rectime_minus_timestamp_us"]["min"] == 37_000_000
    assert frame["timing"]["camera_tstamp_delay_us"] == 25000
    assert not frame["timing"]["clock_conversion_applied"]
    assert not frame["timing"]["camera_delay_applied"]
    assert frame["lidar"]["reported_invalid_points"] == 1
    for role, artifact in frame["artifacts"].items():
        assert sha256_file(root / pair[role]) == artifact["sha256"] == sha256_file(output / artifact["file"])
        assert json.loads((output / artifact["metadata_json"]).read_text())["annotation_status"] == "ok"
        ET.parse(output / artifact["metadata_xml"])
    ET.parse(output / "fusion_manifest.xml")
    assert len((output / frame["point_csv"]).read_text().splitlines()) == 5
    assert frame["preview_rows"] == 4
    assert (output / frame["point_csv_preview"]).read_bytes() == (output / frame["point_csv"]).read_bytes()
    assert json.loads((output / frame["point_csv_metadata_json"]).read_text())["csv"]["row_count"] == 4
    import csv
    with (output / frame["point_csv"]).open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [int(row["timestamp"]) for row in rows] == [62_999_990, 62_999_995, 63_000_000, 63_000_005]
    assert [float(row["x"]) for row in rows] == [0, 3, 6, 9]
    calibration = json.loads((output / "metadata/cams_lidars.json.metadata.json").read_text())["calibration"]
    assert calibration["camera_count"] == 1 and calibration["lidar_count"] == 2


def test_prefixed_schema_and_prefix_statistics(paired):
    np = pytest.importorskip("numpy")
    from scenario_metadata.a2d2_metadata import load_cloud, summarize_cloud
    root, pair, arrays = paired
    np.savez(root / pair["lidar"], **{"pcloud_points" if key == "points" else "pcloud_attr."+key: value for key,value in arrays.items()})
    values, mapping = load_cloud(root / pair["lidar"])
    summary = summarize_cloud(values, mapping, max_rows=2)
    assert summary["point_count"] == 4 and summary["points_sampled"] == 2 and summary["sample_limited"]
    assert mapping["points"] == "pcloud_points"
    assert summary["lidar_counts"] == {"0": 1, "1": 1}


@pytest.mark.parametrize("change", ["object", "length", "shape", "duplicate", "negative_timestamp", "oversize_header"])
def test_npz_invalid_arrays(paired, change):
    np = pytest.importorskip("numpy")
    from scenario_metadata.a2d2_metadata import load_cloud
    root, pair, arrays = paired
    path = root / pair["lidar"]
    if change == "object":
        arrays["points"] = arrays["points"].astype(object)
    elif change == "length":
        arrays["row"] = arrays["row"][:2]
    elif change == "shape":
        arrays["points"] = arrays["points"].reshape(12)
    elif change == "duplicate":
        arrays["pcloud_points"] = arrays["points"]
    elif change == "negative_timestamp":
        arrays["timestamp"][0] = -1
    if change == "oversize_header":
        header = io.BytesIO()
        np.lib.format.write_array_header_1_0(header, {"descr": "<f8", "fortran_order": False, "shape": (10_000_000, 3)})
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("points.npy", header.getvalue())
    else:
        np.savez(path, **arrays)
    with pytest.raises(ValueError):
        load_cloud(path)


def test_generic_invalid_npz_is_partial(tmp_path):
    from scenario_metadata.metadata import annotate_file
    path = tmp_path / "bad.npz"
    path.write_bytes(b"not a zip")
    assert annotate_file(path)["annotation_status"] == "partial"


@pytest.mark.parametrize("field,value", [("pcld_npz", "other.npz"), ("cam_name", "front_left"),
    ("image_png", "other.png"), ("pcld_view", {"origin": "bad"}), ("cam_tstamp", -1)])
def test_wrong_association(paired, tmp_path, field, value):
    from scenario_metadata.a2d2 import annotate_a2d2
    root, pair, _ = paired
    sidecar = json.loads((root / pair["sidecar"]).read_text())
    sidecar[field] = value
    (root / pair["sidecar"]).write_text(json.dumps(sidecar))
    with pytest.raises(ValueError):
        annotate_a2d2(root, tmp_path / "out", max_frames=1)
    assert not (tmp_path / "out").exists()


def test_nonfinite_and_missing_rectime(paired, tmp_path):
    np = pytest.importorskip("numpy")
    from scenario_metadata.a2d2 import annotate_a2d2
    root, pair, arrays = paired
    arrays["points"][0, 0] = np.nan
    arrays.pop("rectime")
    np.savez(root / pair["lidar"], **arrays)
    report = annotate_a2d2(root, tmp_path / "out", max_frames=1, export_points=False)
    assert report["status"] == "completed_with_warnings"
    frame = report["pairs"][0]
    assert frame["timing"]["rectime_minus_camera_us"] is None
    assert frame["point_csv"] is None
    assert frame["lidar"]["fields"]["x"]["nonfinite_count"] == 1


def test_bad_full_calibration(paired):
    from scenario_metadata.a2d2_metadata import full_calibration
    root, _, _ = paired
    config = json.loads((root / "cams_lidars.json").read_text())
    config["cameras"]["front_center"]["CamMatrix"][0][0] = -1
    config["lidars"]["front_center"]["view"]["x-axis"] = [2, 0, 0]
    result = full_calibration(config)
    assert result["status"] == "partial" and len(result["validation_errors"]) == 2


def test_missing_input_and_existing_output(paired, tmp_path):
    from scenario_metadata.a2d2 import annotate_a2d2
    root, pair, _ = paired
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("keep")
    with pytest.raises(ValueError, match="empty"):
        annotate_a2d2(root, out, max_frames=1)
    (root / pair["image"]).unlink()
    with pytest.raises(FileNotFoundError):
        annotate_a2d2(root, tmp_path / "new", max_frames=1)


@pytest.fixture
def mock_source(paired, monkeypatch):
    root, _, _ = paired
    calls = []
    class Response(io.BytesIO):
        status = 200
        def geturl(self):
            return self.url
    def urlopen(request, timeout):
        key = request.full_url.removeprefix(BASE)
        data = (root / key).read_bytes()
        digest = hashlib.md5(data).hexdigest()
        response = Response(data)
        response.url = request.full_url
        response.headers = {"ETag": '"'+digest+'"', "Content-Length": str(len(data))}
        calls.append(request.get_method())
        if request.get_method() == "GET":
            assert request.get_header("If-match") == '"'+digest+'"'
        return response
    monkeypatch.setattr("scenario_metadata.a2d2_download.urlopen", urlopen)
    return calls


def test_download_cache_repair_and_cli(mock_source, tmp_path):
    from scenario_metadata.cli import main
    root = tmp_path / "downloaded"
    first = download_a2d2(root, max_frames=1)
    assert len(first["files"]) == 6 and mock_source.count("GET") == 6
    second = download_a2d2(root, max_frames=1)
    assert second["transferred_bytes"] == 0 and all(item["cached"] for item in second["files"])
    Path(second["files"][-1]["path"]).write_bytes(b"corrupted")
    third = download_a2d2(root, max_frames=1)
    assert sum(not item["cached"] for item in third["files"]) == 1
    assert main(["a2d2", "run", "--data-dir", str(root), "--max-frames", "1", "--output", str(tmp_path / "cli-out")]) == 0
    assert (tmp_path / "cli-out/fusion_manifest.json").exists()


def test_download_budget_before_get(mock_source, tmp_path):
    with pytest.raises(ValueError, match="exceed"):
        download_a2d2(tmp_path / "downloaded", max_frames=1, budget=1)
    assert "GET" not in mock_source


@pytest.mark.parametrize("sequence,camera,start,count", [("../../x", "front_center", 60, 1),
    ("20180810_150607", "../x", 60, 1), ("20180810_150607", "front_center", -1, 1),
    ("20180810_150607", "front_center", 60, 0)])
def test_selection_validation(sequence, camera, start, count):
    with pytest.raises(ValueError):
        selection(sequence, camera, start, count)
