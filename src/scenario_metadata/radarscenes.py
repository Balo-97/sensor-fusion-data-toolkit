"""RadarScenes detection/odometry metadata and validated scene selection.

Schema and units: https://radar-scenes.com/dataset/structure/
These are labelled radar points, not highD object trajectories or raw ADC data.
"""
from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from pathlib import Path

from .downloads import safe_child, sha256_file
from .radarscenes_download import SOURCE, sequence_name

RADAR_UNITS = {"timestamp": "microseconds, arbitrary origin (not Unix)", "sensor_id": "identifier",
    "range_sc": "m", "azimuth_sc": "rad", "rcs": "dBsm", "vr": "m/s", "vr_compensated": "m/s",
    "x_cc": "m", "y_cc": "m", "x_seq": "m", "y_seq": "m", "uuid": "identifier",
    "track_id": "identifier; empty means no dynamic-object association", "label_id": "class identifier"}
ODOMETRY_UNITS = {"timestamp": RADAR_UNITS["timestamp"], "x_seq": "m", "y_seq": "m",
                  "yaw_seq": "rad", "vx": "m/s", "yaw_rate": "rad/s"}
LABELS = dict(enumerate(["car", "large_vehicle", "truck", "bus", "train", "bicycle",
    "motorized_two_wheeler", "pedestrian", "pedestrian_group", "animal", "other", "static"]))
COORDINATES = {"sc": "sensor coordinates", "cc": "car coordinates, origin at rear-axle center",
               "seq": "sequence coordinates, arbitrary origin"}


def dependencies():
    try:
        import h5py
        import numpy as np
    except ImportError as exc:
        raise ImportError('RadarScenes HDF5 support requires: python -m pip install -e ".[radarscenes]"') from exc
    return h5py, np


def read_json(path: Path) -> dict:
    from .structured import _unique_object
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("RadarScenes JSON exceeds 64 MiB")
    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
    json.dumps(data, allow_nan=False)
    if not isinstance(data, dict):
        raise ValueError("Expected a RadarScenes JSON object")
    return data


def sensor_calibration(data: dict) -> dict:
    if set(data) != {f"radar_{i}" for i in range(1, 5)}:
        raise ValueError("RadarScenes sensors.json must describe radar_1 through radar_4")
    sensors = []
    for i in range(1, 5):
        value = data[f"radar_{i}"]
        if not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] != i:
            raise ValueError("RadarScenes sensor name/id mismatch")
        if any(type(value.get(key)) not in (float, int) or not math.isfinite(value[key]) for key in ("x", "y", "yaw")):
            raise ValueError("RadarScenes mounting poses must be finite x, y and yaw")
        sensors.append({"name": f"radar_{i}", **value})
    return {"schema": "radarscenes-sensors", "status": "ok", "sensor_count": 4, "sensors": sensors,
            "units": {"x": "m", "y": "m", "yaw": "rad"}, "coordinate_frame": COORDINATES["cc"],
            "validation_errors": [], "validation_scope": "2D pose structure, finite values and sensor IDs",
            "transforms_applied": False, "elevation_available": False}


def _dataset(handle, name: str, fields: dict):
    h5py, _ = dependencies()
    if not isinstance(handle.get(name, getlink=True), h5py.HardLink):
        raise ValueError(f"{name} must be an internal HDF5 dataset")
    dataset = handle[name]
    if not isinstance(dataset, h5py.Dataset) or dataset.ndim != 1 or set(dataset.dtype.names or ()) != set(fields):
        raise ValueError(f"Invalid RadarScenes {name} compound-array schema")
    if dataset.is_virtual or dataset.external:
        raise ValueError("External/virtual HDF5 storage is unsupported")
    for field in fields:
        dtype = dataset.dtype.fields[field][0]
        if field in ("uuid", "track_id"):
            valid = dtype.kind == "S" and 0 < dtype.itemsize <= 256
        elif field in ("timestamp", "sensor_id", "label_id"):
            valid = dtype.kind in "iu" and dtype.itemsize <= 8
        else:
            valid = dtype.kind == "f" and dtype.itemsize in (4, 8)
        if not valid:
            raise ValueError(f"Invalid type for {name}.{field}: {dtype}")
    return dataset


def _summary(rows, units: dict) -> dict:
    _, np = dependencies()
    stats, errors, fatal_errors = {}, [], []
    for field in rows.dtype.names:
        column = rows[field]
        if column.dtype.kind in "iuf":
            finite = np.isfinite(column)
            valid = column[finite]
            count = int((~finite).sum())
            stats[field] = {"unit": units[field], "min": valid.min().item() if len(valid) else None,
                            "max": valid.max().item() if len(valid) else None, "nonfinite_count": count}
            if count:
                errors.append(f"Nonfinite {field} values: {count}")
    result = {"rows_sampled": len(rows), "numeric_fields": stats, "validation_errors": errors}
    if "label_id" in rows.dtype.names:
        counts = Counter(int(value) for value in rows["label_id"])
        result["label_counts"] = {LABELS.get(key, f"unknown_{key}"): count for key, count in sorted(counts.items())}
        result["sensor_ids"] = sorted(set(int(value) for value in rows["sensor_id"]))
        tracks = set(bytes(value) for value in rows["track_id"] if value)
        result["distinct_nonempty_track_ids"] = len(tracks)
        if set(counts) - set(LABELS) or set(result["sensor_ids"]) - {1, 2, 3, 4}:
            fatal_errors.append("Unknown label or sensor ID")
        if len(rows) and bool((rows["range_sc"] < 0).any()):
            fatal_errors.append("Negative radar ranges")
    errors.extend(fatal_errors)
    result["fatal_errors"] = fatal_errors
    result["status"] = "partial" if errors else "ok"
    return result


def hdf5_metadata(path: Path, max_rows: int = 100_000) -> dict:
    if max_rows <= 0:
        raise ValueError("max_rows must be positive")
    h5py, _ = dependencies()
    result = {"parser": "radarscenes-hdf5-v1", "representation": "processed radar detections and ego odometry",
              "coordinate_frames": COORDINATES, "field_units": {"radar_data": RADAR_UNITS, "odometry": ODOMETRY_UNITS},
              "calibration_applied": False, "max_rows_per_dataset": max_rows, "datasets": {}, "status": "ok"}
    with h5py.File(path, "r") as handle:
        for name, units in (("radar_data", RADAR_UNITS), ("odometry", ODOMETRY_UNITS)):
            dataset = _dataset(handle, name, units)
            result["datasets"][name] = summary = _summary(dataset[:max_rows], units)
            summary.update({"total_rows": len(dataset), "fields": list(dataset.dtype.names),
                            "sample_limited": len(dataset) > max_rows})
            if summary["status"] != "ok":
                result["status"] = "partial"
    return result


def _integer(value, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def select_scenes(source: dict, start_scene: int, max_scenes: int, sensor_id: int | None):
    """Shared ordering/filtering for image acquisition and radar annotation."""
    if start_scene < 0 or max_scenes <= 0 or sensor_id not in (None, 1, 2, 3, 4):
        raise ValueError("Use nonnegative --start-scene and positive --max-scenes; sensor IDs are 1–4")
    if not isinstance(source.get("scenes"), dict):
        raise ValueError("Missing scenes mapping")
    scenes = []
    for key, scene in source["scenes"].items():
        if not key.isascii() or not key.isdecimal() or str(int(key)) != key or not isinstance(scene, dict):
            raise ValueError("Invalid scene timestamp or scene object")
        if type(scene.get("sensor_id")) is not int or scene["sensor_id"] not in (1, 2, 3, 4):
            raise ValueError("Invalid scene sensor ID")
        scenes.append((int(key), scene))
    scenes.sort(key=lambda item: item[0])
    if not scenes or source.get("first_timestamp") != scenes[0][0] or source.get("last_timestamp") != scenes[-1][0]:
        raise ValueError("Sequence timestamp bounds disagree")
    eligible = [item for item in scenes if sensor_id is None or item[1]["sensor_id"] == sensor_id]
    selected = eligible[start_scene:start_scene + max_scenes]
    if not selected:
        raise ValueError("Scene selection is empty; lower --start-scene or change sensor")
    return scenes, eligible, selected


def camera_timestamp(image_name: str) -> int:
    if not isinstance(image_name, str) or not re.fullmatch(r"[0-9]+\.jpg", image_name):
        raise ValueError("RadarScenes camera reference must be a timestamp-named JPEG")
    return int(image_name[:-4])


def annotate_sequence(root: Path, sequence: str, output: Path, *, start_scene: int = 0,
                      max_scenes: int = 100, sensor_id: int | None = None,
                      max_detections: int = 1_000_000, include_camera: bool = True,
                      max_camera_offset_ms: float | None = None) -> dict:
    from .pipeline import save_annotation, write_json
    h5py, np = dependencies()
    sequence_name(sequence)
    if max_camera_offset_ms is not None and (not math.isfinite(max_camera_offset_ms) or max_camera_offset_ms < 0):
        raise ValueError("--max-camera-offset-ms must be finite and nonnegative")
    if start_scene < 0 or max_scenes <= 0 or max_detections <= 0 or sensor_id not in (None, 1, 2, 3, 4):
        raise ValueError("Use nonnegative --start-scene and positive scene/detection limits; sensor IDs are 1–4")
    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root):
        raise ValueError("Output must be outside the source dataset")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Choose a new empty output directory")
    paths = {"sensors": safe_child(root, "data/sensors.json"), "index": safe_child(root, "data/sequences.json"),
             "scenes": safe_child(root, f"data/{sequence}/scenes.json"),
             "radar": safe_child(root, f"data/{sequence}/radar_data.h5")}
    calibration = sensor_calibration(read_json(paths["sensors"]))
    index = read_json(paths["index"])
    source = read_json(paths["scenes"])
    if source.get("sequence_name") != sequence or not isinstance(source.get("scenes"), dict):
        raise ValueError("Sequence name or scenes mapping is invalid")
    entry = index.get("sequences", {}).get(sequence)
    if not isinstance(entry, dict) or entry.get("n_scenes") != len(source["scenes"]) or entry.get("category") != source.get("category"):
        raise ValueError("Sequence index and scene metadata disagree")
    scenes, eligible, selected = select_scenes(source, start_scene, max_scenes, sensor_id)
    radar_parts, odometry_rows, exported_scenes = [], [], []
    total, previous_end = 0, 0
    with h5py.File(paths["radar"], "r") as handle:
        radar = _dataset(handle, "radar_data", RADAR_UNITS)
        odometry = _dataset(handle, "odometry", ODOMETRY_UNITS)
        for timestamp, scene in selected:
            bounds = scene.get("radar_indices")
            if not isinstance(bounds, list) or len(bounds) != 2:
                raise ValueError("radar_indices must be [start, end)")
            start, end = [_integer(value, "radar_indices") for value in bounds]
            oi = _integer(scene.get("odometry_index"), "odometry_index")
            if not 0 <= previous_end <= start <= end <= len(radar) or oi >= len(odometry):
                raise ValueError("Scene indices overlap, are reversed, or lie outside HDF5 arrays")
            if total + end - start > max_detections:
                raise ValueError("Selection exceeds --max-detections; reduce --max-scenes or raise the limit")
            rows, odo = radar[start:end], odometry[oi]
            if not bool(np.all(rows["timestamp"] == timestamp)) or not bool(np.all(rows["sensor_id"] == scene["sensor_id"])):
                raise ValueError(f"Radar timestamp/sensor mismatch in scene {timestamp}")
            if int(odo["timestamp"]) != _integer(scene.get("odometry_timestamp"), "odometry_timestamp"):
                raise ValueError(f"Odometry timestamp/index mismatch in scene {timestamp}")
            image_name = scene.get("image_name")
            if not isinstance(image_name, str):
                raise ValueError("Missing camera image reference")
            safe_child(root, f"data/{sequence}/camera/{image_name}")
            if include_camera:
                camera_timestamp(image_name)
            exported_scenes.append({"timestamp": timestamp, **scene, "source_radar_indices": bounds,
                "exported_radar_indices": [total, total + len(rows)], "exported_odometry_row": len(odometry_rows),
                "radar_nonfinite_counts": {field: int((~np.isfinite(rows[field])).sum())
                    for field in rows.dtype.names if rows.dtype[field].kind == "f"},
                "camera_image_included": False})
            radar_parts.append(rows)
            odometry_rows.append(odo)
            total += len(rows)
            previous_end = end
        detections = np.concatenate(radar_parts)
        ego = np.array(odometry_rows, dtype=odometry.dtype)
    radar_summary, odometry_summary = _summary(detections, RADAR_UNITS), _summary(ego, ODOMETRY_UNITS)
    if radar_summary["fatal_errors"] or odometry_summary["fatal_errors"]:
        raise ValueError(f"Invalid selected HDF5 data: {radar_summary['fatal_errors'] + odometry_summary['fatal_errors']}")
    quality_warnings = radar_summary["validation_errors"] + odometry_summary["validation_errors"]
    # Decode identifiers before creating any output so invalid text fails cleanly.
    for field in ("uuid", "track_id"):
        for value in detections[field]:
            bytes(value).decode("ascii")
    provenance = {"dataset": "RadarScenes", "source": SOURCE, "sequence": sequence,
                  "category": entry["category"], "source_files": [
                      {"role": key, "path": str(path), "sha256": sha256_file(path)} for key, path in paths.items()]}
    output.mkdir(parents=True, exist_ok=True)
    camera_report = None
    camera_annotations = []
    if include_camera:
        from .radarscenes_camera import annotate_camera_pairs
        camera_report, camera_annotations = annotate_camera_pairs(
            root, sequence, output, exported_scenes, provenance, max_camera_offset_ms)
        quality_warnings.extend(camera_report["warnings"])
    for filename, rows in (("detections.csv", detections), ("odometry.csv", ego)):
        with (output / filename).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(rows.dtype.names)
            for row in rows:
                writer.writerow([bytes(row[field]).decode("ascii") if rows.dtype[field].kind == "S" else row[field].item()
                                 for field in rows.dtype.names])
    write_json(output / "scenes.json", {"sequence_name": sequence, "selection": exported_scenes,
        "index_semantics": "Source radar_indices refer to original HDF5; exported_radar_indices refer to detections.csv (header excluded).",
        "odometry_semantics": "One associated odometry row per selected scene; source rows can repeat."})
    # Keep the original schema so generic JSON annotation recognizes calibration.
    write_json(output / "sensors.json", read_json(paths["sensors"]))
    annotations = camera_annotations
    for path in (output / name for name in ("detections.csv", "odometry.csv", "scenes.json", "sensors.json")):
        result = save_annotation(path, output / "metadata" / path.name, provenance=provenance)
        if result["metadata"]["annotation_status"] != "ok":
            raise ValueError(f"Export annotation failed: {path.name}")
        annotations.append({"json": result["json"], "xml": result["xml"]})
    source_annotation = save_annotation(paths["radar"], output / "metadata" / "radar_data.h5", provenance=provenance)
    annotations.append({"json": source_annotation["json"], "xml": source_annotation["xml"]})
    report = {"status": "completed_with_warnings" if quality_warnings else "passed",
        "data_quality_warnings": quality_warnings, "association_validation": "passed",
        "nonfinite_export_policy": "Source NaN/Infinity values are retained as nan/inf/-inf text in CSV; JSON statistics exclude them and report counts. No imputation.",
        "implementation": "paper-inspired-local", "provenance": provenance,
        "sequence_total_scenes": len(scenes), "eligible_scenes": len(eligible), "start_scene": start_scene,
        "requested_max_scenes": max_scenes, "selected_scenes": len(selected), "sensor_filter": sensor_id,
        "remaining_scenes": max(0, len(eligible) - start_scene - len(selected)),
        "first_timestamp_us": selected[0][0], "last_timestamp_us": selected[-1][0],
        "timestamp_semantics": RADAR_UNITS["timestamp"], "radar": radar_summary, "odometry": odometry_summary,
        "field_units": {"detections.csv": RADAR_UNITS, "odometry.csv": ODOMETRY_UNITS},
        "coordinate_frames": COORDINATES, "calibration": calibration, "camera": camera_report,
        "modalities": {"radar": "available", "camera": "checked" if include_camera else "not_requested",
                       "lidar": "not_provided_by_radarscenes"},
        "validation_scope": "Selected rows: schema, finite values, IDs, bounds, nonoverlap, radar timestamp/sensor and odometry timestamp association. No physical-accuracy or scene-link-chain validation.",
        "source_hdf5_annotation_status": source_annotation["metadata"]["annotation_status"],
        "annotations": annotations, "chat2scenario_executed": False,
        "chat2scenario_limitation": "Radar detections need object trajectories, road/lane context and a validated adapter before highD mining."}
    write_json(output / "radarscenes_report.json", report)
    return report
