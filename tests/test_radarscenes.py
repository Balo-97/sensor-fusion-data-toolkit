import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from scenario_metadata.remote_zip import HTTPRangeReader
from scenario_metadata.radarscenes_download import download_sequences, extract_member, list_sequences, sequence_name


def test_range_zip_and_cache(monkeypatch):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("sample.txt", "radar" * 30000)
    payload = data.getvalue()
    calls = []

    class Response(io.BytesIO):
        status = 206

        def geturl(self):
            return "https://example.test/archive.zip"

    def request(req, timeout):
        start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", req.get_header("Range")).groups())
        calls.append((start, end))
        response = Response(payload[start:end+1])
        response.headers = {"Content-Range": f"bytes {start}-{end}/{len(payload)}"}
        return response

    monkeypatch.setattr("scenario_metadata.remote_zip.urlopen", request)
    with HTTPRangeReader("https://example.test/archive.zip", len(payload)) as reader:
        with zipfile.ZipFile(reader) as zipped:
            assert zipped.read("sample.txt") == b"radar" * 30000
        transferred = reader.transferred
        reader.seek(0)
        assert reader.read(4) == payload[:4]
        assert reader.transferred == transferred
    assert calls


@pytest.mark.parametrize("status,content_range,expected", [(200, "", "honor Range"), (206, "bytes 0-98/100", "Content-Range")])
def test_range_refuses_full_or_wrong_response(monkeypatch, status, content_range, expected):
    class Response(io.BytesIO):
        def geturl(self):
            return "https://example.test/archive.zip"
    response = Response(b"x" * 100)
    response.status, response.headers = status, {"Content-Range": content_range}
    monkeypatch.setattr("scenario_metadata.remote_zip.urlopen", lambda *a, **kw: response)
    with pytest.raises(RuntimeError, match=expected):
        HTTPRangeReader("https://example.test/archive.zip", 100).read(1)


def test_range_budget_before_network(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("Network called despite exhausted budget")
    monkeypatch.setattr("scenario_metadata.remote_zip.urlopen", forbidden)
    with pytest.raises(RuntimeError, match="budget"):
        HTTPRangeReader("https://example.test/archive.zip", 1000, budget=100).read(1)


def test_local_archive_download_cache_and_repair(tmp_path):
    archive = tmp_path / "sample.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("RadarScenes/Readme.md", "fixture")
        zipped.writestr("RadarScenes/data/sensors.json", "{}")
        zipped.writestr("RadarScenes/data/sequences.json", json.dumps({"sequences": {"sequence_158": {"n_scenes": 1}}}))
        zipped.writestr("RadarScenes/data/sequence_158/scenes.json", "{}")
        zipped.writestr("RadarScenes/data/sequence_158/radar_data.h5", "fixture radar")
    root = tmp_path / "data"
    first = download_sequences(root, ["sequence_158"], archive=archive, include_camera=False)
    assert not any(f["cached"] for f in first["files"])
    assert not first["publisher_archive_md5_verified"]
    assert list_sequences(archive)[0]["sequence"] == "sequence_158"
    assert all(f["cached"] for f in download_sequences(root, ["sequence_158"], archive=archive, include_camera=False)["files"])
    (root / "data/sequence_158/radar_data.h5").write_text("fixture BADAR")
    repaired = download_sequences(root, ["sequence_158"], archive=archive, include_camera=False)
    assert sum(not f["cached"] for f in repaired["files"]) == 1
    with pytest.raises(ValueError, match="extracted"):
        download_sequences(root, ["sequence_158"], archive=archive, max_extracted_bytes=1)


def test_zip_traversal_and_crc(tmp_path):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as zipped:
        zipped.writestr("RadarScenes/../escape.txt", "bad")
        zipped.writestr("RadarScenes/valid.txt", "original")
    damaged = raw.getvalue().replace(b"original", b"corrupt!")
    with zipfile.ZipFile(io.BytesIO(damaged)) as zipped:
        with pytest.raises(ValueError):
            extract_member(zipped, "RadarScenes/../escape.txt", tmp_path, 100)
        with pytest.raises(zipfile.BadZipFile):
            extract_member(zipped, "RadarScenes/valid.txt", tmp_path, 100)
    assert not (tmp_path / "valid.txt").exists()
    assert not (tmp_path / "valid.txt.part").exists()


@pytest.mark.parametrize("name", ["../sequence_1", "sequence_159", "sequence_0", "sequence_01", "sequence_1/camera"])
def test_invalid_sequence(name):
    with pytest.raises(ValueError):
        sequence_name(name)


@pytest.fixture
def radar_fixture(tmp_path):
    h5py = pytest.importorskip("h5py")
    np = pytest.importorskip("numpy")
    from scenario_metadata.radarscenes import RADAR_UNITS, ODOMETRY_UNITS
    root = tmp_path / "RadarScenes"
    folder = root / "data/sequence_158"
    folder.mkdir(parents=True)
    sensors = {f"radar_{i}": {"id": i, "x": 3.0, "y": 0.5, "yaw": 0.0} for i in range(1, 5)}
    (root / "data/sensors.json").write_text(json.dumps(sensors))
    (root / "data/sequences.json").write_text(json.dumps({"sequences": {"sequence_158": {"n_scenes": 3, "category": "train"}}}))
    radar_dtype = [(key, "S32" if key in ("uuid", "track_id") else "i8" if key in ("timestamp", "sensor_id", "label_id") else "f4") for key in RADAR_UNITS]
    rows = np.zeros(6, dtype=radar_dtype)
    rows["timestamp"] = [100, 100, 200, 200, 300, 300]
    rows["sensor_id"] = [1, 1, 2, 2, 1, 1]
    rows["uuid"] = [f"{i:032x}".encode() for i in range(6)]
    rows["track_id"] = [b"", b"abc", b"abc", b"", b"", b"def"]
    rows["label_id"] = [11, 0, 0, 11, 11, 7]
    odo = np.zeros(2, dtype=[(key, "i8" if key == "timestamp" else "f4") for key in ODOMETRY_UNITS])
    odo["timestamp"] = [90, 290]
    with h5py.File(folder / "radar_data.h5", "w") as f:
        f["radar_data"], f["odometry"] = rows, odo
    scenes = {str(t): {"sensor_id": sid, "radar_indices": [i*2, i*2+2], "odometry_index": 0 if i < 2 else 1,
              "odometry_timestamp": 90 if i < 2 else 290, "image_name": "150.jpg" if i < 2 else "350.jpg"}
              for i, (t, sid) in enumerate([(100, 1), (200, 2), (300, 1)])}
    (folder / "scenes.json").write_text(json.dumps({"sequence_name": "sequence_158", "category": "train",
        "first_timestamp": 100, "last_timestamp": 300, "scenes": scenes}))
    from PIL import Image
    (folder / "camera").mkdir()
    for name in ("150.jpg", "350.jpg"):
        Image.new("RGB", (32, 24), color="red").save(folder / "camera" / name)
    return root


def test_scene_selection_and_exports(radar_fixture, tmp_path):
    from scenario_metadata.radarscenes import annotate_sequence
    import csv
    output = tmp_path / "out"
    report = annotate_sequence(radar_fixture, "sequence_158", output, start_scene=1, max_scenes=10, sensor_id=1)
    assert report["selected_scenes"] == 1
    assert report["radar"]["rows_sampled"] == 2
    assert report["radar"]["label_counts"] == {"pedestrian": 1, "static": 1}
    assert report["radar"]["distinct_nonempty_track_ids"] == 1
    rows = list(csv.DictReader((output / "detections.csv").open()))
    assert rows[0]["timestamp"] == "300" and rows[0]["track_id"] == ""
    assert rows[1]["uuid"] == f"{5:032x}"
    selection = json.loads((output / "scenes.json").read_text())["selection"][0]
    assert selection["source_radar_indices"] == [4, 6]
    assert selection["exported_radar_indices"] == [0, 2]
    assert not report["chat2scenario_executed"]
    assert len(list((output / "metadata").glob("*.xml"))) == 5
    assert json.loads((output / "metadata/sensors.json.metadata.json").read_text())["calibration"]["sensor_count"] == 4
    with pytest.raises(ValueError, match="empty output"):
        annotate_sequence(radar_fixture, "sequence_158", output)


@pytest.mark.parametrize("field,value,match", [("radar_indices", [0, 99], "indices"),
    ("radar_indices", [2, 0], "indices"), ("sensor_id", 4, "mismatch"),
    ("odometry_timestamp", 100, "Odometry"), ("odometry_index", 99, "indices")])
def test_scene_association_rejected(radar_fixture, tmp_path, field, value, match):
    from scenario_metadata.radarscenes import annotate_sequence
    path = radar_fixture / "data/sequence_158/scenes.json"
    scenes = json.loads(path.read_text())
    scenes["scenes"]["100"][field] = value
    path.write_text(json.dumps(scenes))
    with pytest.raises(ValueError, match=match):
        annotate_sequence(radar_fixture, "sequence_158", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_hdf5_summary_limit_and_invalid(radar_fixture, tmp_path):
    import h5py
    from scenario_metadata.radarscenes import hdf5_metadata, annotate_sequence
    from scenario_metadata.metadata import annotate_file
    path = radar_fixture / "data/sequence_158/radar_data.h5"
    summary = hdf5_metadata(path, max_rows=2)
    assert summary["datasets"]["radar_data"]["total_rows"] == 6
    assert summary["datasets"]["radar_data"]["sample_limited"]
    with h5py.File(path, "r+") as f:
        row = f["radar_data"][0]
        row["range_sc"] = float("nan")
        f["radar_data"][0] = row
    assert annotate_file(path)["annotation_status"] == "partial"
    report = annotate_sequence(radar_fixture, "sequence_158", tmp_path / "out")
    assert report["status"] == "completed_with_warnings"
    assert report["association_validation"] == "passed"
    assert report["radar"]["numeric_fields"]["range_sc"]["nonfinite_count"] == 1
    assert "nan" in (tmp_path / "out/detections.csv").read_text()


def test_hdf5_external_link_rejected(tmp_path):
    h5py = pytest.importorskip("h5py")
    from scenario_metadata.metadata import annotate_file
    path = tmp_path / "external.h5"
    with h5py.File(path, "w") as f:
        f["radar_data"] = h5py.ExternalLink("missing.h5", "/radar_data")
    result = annotate_file(path)
    assert result["annotation_status"] == "partial"
    assert "internal" in result["radar"]["reason"]


def test_selection_limits(radar_fixture, tmp_path):
    from scenario_metadata.radarscenes import annotate_sequence
    for kwargs, message in [({"start_scene": 3}, "empty"), ({"max_detections": 1}, "max-detections"),
                             ({"max_scenes": 0}, "positive")]:
        with pytest.raises(ValueError, match=message):
            annotate_sequence(radar_fixture, "sequence_158", tmp_path / "out", **kwargs)


def fixture_archive(root, destination):
    (root / "Readme.md").write_text("Synthetic RadarScenes fixture")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in root.rglob("*"):
            if path.is_file():
                zipped.write(path, "RadarScenes/" + path.relative_to(root).as_posix())
    return destination


def test_camera_download_dedup_filter_cache(radar_fixture, tmp_path):
    archive = fixture_archive(radar_fixture, tmp_path / "fixture.zip")
    root = tmp_path / "downloaded"
    report = download_sequences(root, ["sequence_158"], archive=archive, max_scenes=2)
    assert report["unique_camera_images"] == 1
    assert len(report["files"]) == 6
    assert (root / "data/sequence_158/camera/150.jpg").is_file()
    assert not (root / "data/sequence_158/camera/350.jpg").exists()
    repeat = download_sequences(root, ["sequence_158"], archive=archive, max_scenes=2)
    assert all(item["cached"] for item in repeat["files"])
    filtered = download_sequences(root, ["sequence_158"], archive=archive, sensor_id=1, start_scene=1, max_scenes=1)
    assert filtered["camera_selections"][0]["selected_scenes"] == 1
    assert (root / "data/sequence_158/camera/350.jpg").is_file()
    target = root / "data/sequence_158/camera/350.jpg"
    target.write_bytes(b"broken")
    repaired = download_sequences(root, ["sequence_158"], archive=archive, start_scene=2, max_scenes=1)
    assert sum(not item["cached"] for item in repaired["files"]) == 1
    assert target.read_bytes() == (radar_fixture / "data/sequence_158/camera/350.jpg").read_bytes()


def test_camera_extraction_budget(radar_fixture, tmp_path):
    archive = fixture_archive(radar_fixture, tmp_path / "fixture.zip")
    with zipfile.ZipFile(archive) as zipped:
        base_size = sum(info.file_size for info in zipped.infolist() if "/camera/" not in info.filename)
    with pytest.raises(ValueError, match="Radar plus camera"):
        download_sequences(tmp_path / "downloaded", ["sequence_158"], archive=archive, max_extracted_bytes=base_size)
    assert not (tmp_path / "downloaded/data/sequence_158/camera").exists()


def test_camera_missing_archive_reference(radar_fixture, tmp_path):
    (radar_fixture / "data/sequence_158/camera/150.jpg").unlink()
    archive = fixture_archive(radar_fixture, tmp_path / "fixture.zip")
    with pytest.raises(ValueError, match="absent from archive"):
        download_sequences(tmp_path / "downloaded", ["sequence_158"], archive=archive)


def test_joint_metadata_offsets_and_hashes(radar_fixture, tmp_path):
    from scenario_metadata.radarscenes import annotate_sequence
    from scenario_metadata.downloads import sha256_file
    from xml.etree import ElementTree as ET
    output = tmp_path / "paired"
    report = annotate_sequence(radar_fixture, "sequence_158", output, max_camera_offset_ms=0.05)
    assert report["status"] == "passed"
    assert report["camera"]["unique_images_valid"] == 2
    assert report["camera"]["scenes_with_valid_images"] == 3
    assert report["camera"]["shared_images"] == 1
    manifest = json.loads((output / "fusion_manifest.json").read_text())
    assert [pair["camera_minus_radar_us"] for pair in manifest["pairs"]] == [50, -50, 50]
    assert [pair["odometry_minus_radar_us"] for pair in manifest["pairs"]] == [-10, -110, -10]
    assert [pair["scenes_sharing_camera_image"] for pair in manifest["pairs"]] == [2, 2, 1]
    assert all(pair["within_camera_time_tolerance"] for pair in manifest["pairs"])
    assert not manifest["modalities"]["lidar"]["available"]
    assert manifest["calibration_availability"]["radar_to_camera_extrinsics"] is None
    image = manifest["images"][0]
    assert sha256_file(output / image["file"]) == sha256_file(Path(image["source_path"])) == image["sha256"]
    metadata = json.loads((output / image["metadata_json"]).read_text())
    assert metadata["image"]["width"] == 32 and metadata["image"]["height"] == 24
    assert metadata["provenance"]["capture_timestamp_us"] == 150
    ET.parse(output / "fusion_manifest.xml")
    assert len(list((output / "metadata/camera").glob("*.metadata.xml"))) == 2


def test_camera_tolerance_and_missing_invalid_images(radar_fixture, tmp_path):
    from scenario_metadata.radarscenes import annotate_sequence
    (radar_fixture / "data/sequence_158/camera/150.jpg").unlink()
    (radar_fixture / "data/sequence_158/camera/350.jpg").write_bytes(b"corrupt JPEG")
    output = tmp_path / "paired"
    report = annotate_sequence(radar_fixture, "sequence_158", output, max_camera_offset_ms=0.01)
    assert report["status"] == "completed_with_warnings"
    assert report["camera"]["missing_images"] == 1
    assert report["camera"]["invalid_images"] == 1
    assert report["camera"]["scenes_with_valid_images"] == 0
    assert report["camera"]["outside_tolerance_scenes"] == 3
    manifest = json.loads((output / "fusion_manifest.json").read_text())
    assert [pair["pair_status"] for pair in manifest["pairs"]] == ["missing", "missing", "invalid_image"]
    assert manifest["pairs"][0]["camera_file"] is None


@pytest.mark.parametrize("name", ["../150.jpg", "not-a-time.jpg", "C:/150.jpg", "150.png"])
def test_camera_reference_validation(radar_fixture, tmp_path, name):
    from scenario_metadata.radarscenes import annotate_sequence
    path = radar_fixture / "data/sequence_158/scenes.json"
    data = json.loads(path.read_text())
    data["scenes"]["100"]["image_name"] = name
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        annotate_sequence(radar_fixture, "sequence_158", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_camera_default_optout_and_cli(radar_fixture, tmp_path):
    from scenario_metadata.radarscenes import annotate_sequence
    from scenario_metadata.cli import build_parser, main
    args = build_parser().parse_args(["radarscenes", "download", "--start-scene", "2", "--max-scenes", "1"])
    assert args.start_scene == 2 and not args.no_camera
    output = tmp_path / "out"
    report = annotate_sequence(radar_fixture, "sequence_158", output, include_camera=False)
    assert report["camera"] is None
    assert not (output / "fusion_manifest.json").exists()
    archive = fixture_archive(radar_fixture, tmp_path / "fixture.zip")
    assert main(["radarscenes", "run", "--archive", str(archive), "--data-dir", str(tmp_path / "downloaded"),
                 "--max-scenes", "2", "--output", str(tmp_path / "cli-out")]) == 0
    assert (tmp_path / "cli-out/camera/150.jpg").exists()
    assert not (tmp_path / "cli-out/camera/350.jpg").exists()


@pytest.mark.parametrize("tolerance", [-1, float("nan"), float("inf")])
def test_bad_camera_tolerance(radar_fixture, tmp_path, tolerance):
    from scenario_metadata.radarscenes import annotate_sequence
    with pytest.raises(ValueError, match="finite and nonnegative"):
        annotate_sequence(radar_fixture, "sequence_158", tmp_path / "out", max_camera_offset_ms=tolerance)
