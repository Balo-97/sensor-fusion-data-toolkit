import json

import pytest

from scenario_metadata.metadata import annotate_file
from scenario_metadata.structured import csv_metadata


def test_csv_types_missing_quotes_and_duplicate_headers(tmp_path):
    path = tmp_path / "table.csv"
    path.write_text('\ufeffid,value,value\n1,2.5,"a,b"\n2,,text\n3,NaN,"multi\nline"\n', encoding="utf-8")
    result = csv_metadata(path)
    assert result["row_count"] == 3
    assert result["duplicate_headers"] == ["value"]
    assert result["columns"][0]["numeric_max"] == 3
    assert result["columns"][1]["missing_count"] == 1
    assert result["columns"][1]["nonfinite_count"] == 1
    assert result["columns"][2]["observed_types"] == ["text"]
    json.dumps(result, allow_nan=False)


def test_csv_headerless_and_ragged(tmp_path):
    path = tmp_path / "table.csv"
    path.write_text("1;2\n3;4\n5\n", encoding="utf-8")
    result = annotate_file(path, csv_header=False, csv_delimiter=";")
    assert result["csv"]["row_count"] == 3
    assert result["csv"]["headers"] == ["column_1", "column_2"]
    assert result["csv"]["ragged_rows"] == 1
    assert result["annotation_status"] == "partial"


def test_csv_sample_never_claims_full_count(tmp_path, monkeypatch):
    import scenario_metadata.structured as module
    monkeypatch.setattr(module, "MAX_ROWS", 2)
    path = tmp_path / "large.csv"
    path.write_text("a\n1\n2\n3\n")
    result = csv_metadata(path)
    assert result["rows_scanned"] == 2 and result["row_count"] is None
    assert result["sample_limited"]


def test_invalid_csv_reports_partial_and_retains_hash(tmp_path):
    path = tmp_path / "invalid.csv"
    path.write_text('a,b\n1,"unfinished')
    result = annotate_file(path)
    assert result["annotation_status"] == "partial"
    assert len(result["file"]["sha256"]) == 64


def test_astyx_radar_is_numeric_and_requires_layout(tmp_path):
    folder = tmp_path / "radar_6455"
    folder.mkdir()
    path = folder / "000000.txt"
    path.write_text("\nX Y Z V_r Mag\n1 2 3 -4 5\n2 3 4 0 6\n")
    result = annotate_file(path)
    assert result["radar"]["detection_count"] == 2
    assert result["radar"]["columns"][3]["numeric_min"] == -4
    other = tmp_path / "notes.txt"
    other.write_text(path.read_text())
    assert "radar" not in annotate_file(other)
    path.write_text(path.read_text()+"1 2 3 NaN 5\n")
    assert annotate_file(path)["annotation_status"] == "partial"


def test_astyx_calibration_checks_shape_and_rotation(tmp_path):
    path = tmp_path / "calibration.json"
    identity = [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
    value = {"sensors": [{"sensor_uid": "camera", "calib_data": {
        "T_to_ref_COS": identity, "K": [[800,0,320],[0,800,240],[0,0,1]]}}]}
    path.write_text(json.dumps(value))
    result = annotate_file(path)
    assert result["calibration"]["status"] == "ok"
    assert result["calibration"]["sensors"][0]["K"][0][0] == 800
    identity[0][0] = -1
    path.write_text(json.dumps(value))
    result = annotate_file(path)
    assert result["annotation_status"] == "partial"
    assert any("determinant" in e for e in result["calibration"]["validation_errors"])


def test_a2d2_pose_is_not_claimed_as_complete_calibration(tmp_path):
    path = tmp_path / "frame.json"
    value = {"cam_name":"front_center", "cam_tstamp":12345, "image_png":"frame.png",
             "pcld_view":{"origin":[1,2,3],"x-axis":[1,0,0],"y-axis":[0,1,0]}}
    path.write_text(json.dumps(value))
    result = annotate_file(path)
    assert result["calibration"]["schema"] == "a2d2-frame-sidecar"
    assert result["calibration"]["source_timestamp_raw"] == 12345
    assert "not the full" in result["calibration"]["validation_scope"]


@pytest.mark.parametrize("content", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', '{'])
def test_json_rejects_ambiguous_or_invalid_values(tmp_path, content):
    path = tmp_path / "invalid.json"
    path.write_text(content)
    assert annotate_file(path)["annotation_status"] == "partial"


def test_unknown_json_gets_structure_without_guessed_calibration(tmp_path):
    path = tmp_path / "unknown.json"
    path.write_text('{"K": [1, 2, 3], "vendor_specific": true}')
    result = annotate_file(path)
    assert result["json"]["top_level_keys"] == ["K", "vendor_specific"]
    assert "calibration" not in result
