"""Explicit local extensions for tabular data and recognized calibration schemas.

No units, timestamps or calibration conventions are inferred from arbitrary names.
Astyx fields are preserved as published; these routines do not calibrate a sensor.
"""
from __future__ import annotations

import csv
import itertools
import json
import math
import re
from pathlib import Path

MAX_ROWS = 100_000
MAX_TEXT_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024


class SampleLimit(Exception):
    pass


def _lines(path: Path):
    consumed = 0
    with path.open("rb") as stream:
        first = True
        while True:
            raw = stream.readline(MAX_LINE_BYTES + 1)
            if not raw:
                return
            consumed += len(raw)
            if len(raw) > MAX_LINE_BYTES or consumed > MAX_TEXT_BYTES:
                raise SampleLimit()
            yield raw.decode("utf-8-sig" if first else "utf-8")
            first = False


def _summarize(reader, has_header: bool) -> dict:
    first = next(reader, None)
    if first is None:
        return {"status": "ok", "headers": [], "column_count": 0, "rows_scanned": 0,
                "row_count": 0, "sample_limited": False, "columns": [], "ragged_rows": 0}
    if not first or len(first) > 4096:
        raise ValueError("The first record must have 1-4096 columns")
    headers = first if has_header else [f"column_{i+1}" for i in range(len(first))]
    rows = reader if has_header else itertools.chain([first], reader)
    columns = [{"index": i, "name": name, "missing_count": 0, "numeric_count": 0,
                "nonfinite_count": 0, "numeric_min": None, "numeric_max": None, "observed_types": set()}
               for i, name in enumerate(headers)]
    count, ragged, limited = 0, 0, False
    try:
        for row in rows:
            if count >= MAX_ROWS:
                limited = True
                break
            count += 1
            if len(row) != len(columns):
                ragged += 1
                continue
            for column, raw in zip(columns, row):
                value = raw.strip()
                if not value:
                    column["missing_count"] += 1
                    continue
                try:
                    number = int(value) if re.fullmatch(r"[+-]?\d+", value) else float(value)
                except ValueError:
                    column["observed_types"].add("text")
                    continue
                if isinstance(number, float) and not math.isfinite(number):
                    column["nonfinite_count"] += 1
                    continue
                column["observed_types"].add("integer" if isinstance(number, int) else "number")
                column["numeric_count"] += 1
                column["numeric_min"] = number if column["numeric_min"] is None else min(column["numeric_min"], number)
                column["numeric_max"] = number if column["numeric_max"] is None else max(column["numeric_max"], number)
    except SampleLimit:
        limited = True
    for column in columns:
        column["observed_types"] = sorted(column["observed_types"])
    return {"status": "partial" if ragged else "ok", "headers": headers, "column_count": len(headers),
            "header_assumption": "first record is header" if has_header else "no header; generated column names",
            "rows_scanned": count, "row_count": None if limited else count,
            "sample_limited": limited, "max_rows": MAX_ROWS, "max_text_bytes": MAX_TEXT_BYTES,
            "ragged_rows": ragged, "columns": columns,
            "duplicate_headers": sorted({x for x in headers if headers.count(x) > 1}),
            "statistics_scope": "complete, correctly sized records in scanned prefix; numeric ranges cover numeric values only",
            "units": "Not inferred. Whitespace-only cells count as missing; other missing-value conventions are not assumed."}


def csv_metadata(path: Path, *, delimiter: str = ",", has_header: bool = True) -> dict:
    if len(delimiter) != 1 or delimiter in {"\r", "\n", '"'}:
        raise ValueError("CSV delimiter must be one non-newline, non-quote character")
    result = _summarize(csv.reader(_lines(path), delimiter=delimiter, strict=True), has_header)
    result.update({"parser": "csv-summary-v1", "encoding": "UTF-8 (optional BOM)", "delimiter": delimiter})
    return result


def astyx_radar_metadata(path: Path) -> dict | None:
    """Recognize the published Astyx whitespace header; do not relabel other TXT."""
    lines = _lines(path)
    first = next((line for line in lines if line.strip()), "")
    if first.split() != ["X", "Y", "Z", "V_r", "Mag"]:
        return None
    result = _summarize(itertools.chain([first.split()], (line.split() for line in lines if line.strip())), True)
    if any(set(c["observed_types"]) - {"integer", "number"} or c["nonfinite_count"] for c in result["columns"]):
        result["status"] = "partial"
    result.update({"parser": "astyx-point-cloud-v1", "recognition": "exact published X Y Z V_r Mag header",
                   "representation": "processed radar detections, not raw ADC signals",
                   "detection_count": result["row_count"],
                   "coordinate_frame": "Preserved as stored; resolve using the matching calibration JSON.",
                   "units": "Source column labels preserved; units and radial-velocity sign convention are not inferred.",
                   "calibration_applied": False})
    return result


def _finite_vector(value, size: int) -> bool:
    return isinstance(value, list) and len(value) == size and all(
        isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in value)


def _matrix(value, size: int) -> bool:
    return isinstance(value, list) and len(value) == size and all(_finite_vector(row, size) for row in value)


def _transform_errors(matrix) -> list[str]:
    if not _matrix(matrix, 4):
        return ["T_to_ref_COS must be a finite 4x4 matrix"]
    errors = []
    if any(abs(a-b) > 1e-6 for a, b in zip(matrix[3], [0, 0, 0, 1])):
        errors.append("Homogeneous transform bottom row must be [0, 0, 0, 1]")
    r = [row[:3] for row in matrix[:3]]
    if any(abs(sum(r[k][i]*r[k][j] for k in range(3)) - (1 if i == j else 0)) > 1e-3
           for i in range(3) for j in range(3)):
        errors.append("Rotation block is not orthonormal within 0.001")
    det = sum(r[0][i]*(r[1][(i+1)%3]*r[2][(i+2)%3]-r[1][(i+2)%3]*r[2][(i+1)%3]) for i in range(3))
    if abs(det - 1) > 1e-3:
        errors.append("Rotation determinant must be +1 within 0.001")
    return errors


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def json_metadata(path: Path) -> tuple[dict, dict | None]:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("JSON exceeds the 8 MiB parser limit")
    def invalid_constant(value):
        raise ValueError(f"Nonfinite JSON value: {value}")
    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object,
                      parse_constant=invalid_constant)
    # Reject overflowed literals such as 1e999, which parse_constant does not see.
    json.dumps(data, allow_nan=False)
    summary = {"parser": "json-structure-v1", "root_type": type(data).__name__,
               "top_level_keys": sorted(data) if isinstance(data, dict) else None,
               "top_level_count": len(data) if isinstance(data, (dict, list)) else None,
               "recognized_schema": None}
    if not isinstance(data, dict):
        return summary, None
    if {"vehicle", "cameras", "lidars"} <= set(data):
        from .a2d2_metadata import full_calibration
        summary["recognized_schema"] = "a2d2-cams-lidars"
        return summary, full_calibration(data)
    if set(data) == {f"radar_{i}" for i in range(1, 5)}:
        from .radarscenes import sensor_calibration
        summary["recognized_schema"] = "radarscenes-sensors"
        return summary, sensor_calibration(data)
    sensors = data.get("sensors")
    if isinstance(sensors, list) and sensors and all(isinstance(s, dict) and "sensor_uid" in s and "calib_data" in s for s in sensors):
        errors, extracted, ids = [], [], set()
        for sensor in sensors:
            uid, calib = sensor["sensor_uid"], sensor["calib_data"]
            if not isinstance(uid, str) or not uid or not isinstance(calib, dict):
                raise ValueError("Astyx sensors need nonempty string sensor_uid and object calib_data")
            if uid in ids:
                errors.append(f"Duplicate sensor UID: {uid}")
            ids.add(uid)
            errors.extend(f"{uid}: {e}" for e in _transform_errors(calib.get("T_to_ref_COS")))
            if "K" in calib:
                k = calib["K"]
                if not _matrix(k, 3) or k[0][0] <= 0 or k[1][1] <= 0 or any(abs(a-b)>1e-6 for a,b in zip(k[2], [0,0,1])):
                    errors.append(f"{uid}: K must be a finite 3x3 intrinsic matrix with positive focal terms and bottom row [0,0,1]")
            extracted.append({"sensor_uid": uid, "T_to_ref_COS": calib.get("T_to_ref_COS"), "K": calib.get("K")})
        summary["recognized_schema"] = "astyx-calibration"
        return summary, {"schema": "astyx-calibration", "status": "partial" if errors else "ok",
                         "sensor_count": len(extracted), "sensors": extracted, "validation_errors": errors,
                         "validation_scope": "matrix structure and numerical consistency; not physical accuracy",
                         "transforms_applied": False, "field_convention": "Original T_to_ref_COS and K retained without inversion or unit conversion."}
    if {"cam_name", "cam_tstamp", "image_png", "pcld_view"} <= set(data):
        view = data["pcld_view"]
        errors = []
        if not isinstance(view, dict) or not all(_finite_vector(view.get(k), 3) for k in ("origin", "x-axis", "y-axis")):
            errors.append("pcld_view needs finite origin, x-axis and y-axis vectors of length 3")
        else:
            x, y = view["x-axis"], view["y-axis"]
            if abs(sum(v*v for v in x)-1)>1e-3 or abs(sum(v*v for v in y)-1)>1e-3 or abs(sum(a*b for a,b in zip(x,y)))>1e-3:
                errors.append("pcld_view axes must be unit length and orthogonal within 0.001")
        summary["recognized_schema"] = "a2d2-frame-sidecar"
        return summary, {"schema": "a2d2-frame-sidecar", "status": "partial" if errors else "ok",
                         "pcld_view": view, "camera_name": data["cam_name"], "image_file": data["image_png"],
                         "point_cloud_file": data.get("pcld_npz"), "source_timestamp_raw": data["cam_tstamp"],
                         "timestamp_interpretation": "Preserved source value; no epoch/unit conversion applied.",
                         "validation_errors": errors, "transforms_applied": False,
                         "validation_scope": "pose-vector structure; this sidecar is not the full camera intrinsic calibration"}
    return summary, None
