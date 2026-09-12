"""Paired camera/LiDAR metadata for the A2D2 sequential camera_lidar split."""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

from .a2d2_download import SOURCE, selection
from .a2d2_metadata import full_calibration, load_cloud, numpy, statistics, summarize_cloud
from .downloads import safe_child, sha256_file
from .metadata import metadata_to_xml
from .pipeline import save_annotation, write_json


def read_json(path: Path) -> dict:
    from .structured import _unique_object
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("A2D2 JSON exceeds 8 MiB")
    value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
    json.dumps(value, allow_nan=False)
    if not isinstance(value, dict):
        raise ValueError("Expected an A2D2 JSON object")
    return value


def annotate_a2d2(root: Path, output: Path, *, sequence: str = "20180810_150607",
                    camera: str = "front_center", start_frame: int = 60, max_frames: int = 3,
                    export_points: bool = True) -> dict:
    np = numpy()
    requested = selection(sequence, camera, start_frame, max_frames)
    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root) or output.exists() and any(output.iterdir()):
        raise ValueError("Choose an empty output directory outside the source dataset")
    config_path = safe_child(root, "cams_lidars.json")
    config = read_json(config_path)
    calibration = full_calibration(config)
    if calibration["status"] != "ok" or camera not in config["cameras"]:
        raise ValueError(f"Invalid A2D2 calibration or unknown camera: {calibration['validation_errors']}")
    # Check associations before creating exports; no cross-view matching by frame number.
    inputs = []
    for pair in requested:
        paths = {role: safe_child(root, pair[role]) for role in ("image", "sidecar", "lidar")}
        for path in paths.values():
            if not path.is_file():
                raise FileNotFoundError(f"Missing paired A2D2 input: {path}; use 'a2d2 run' for this selection")
        sidecar = read_json(paths["sidecar"])
        if sidecar.get("cam_name") != camera or sidecar.get("image_png") != paths["image"].name or sidecar.get("pcld_npz") != paths["lidar"].name:
            raise ValueError("Camera JSON does not match the requested image, LiDAR file and view")
        timestamp = sidecar.get("cam_tstamp")
        if type(timestamp) is not int or not 0 <= timestamp <= np.iinfo(np.int64).max:
            raise ValueError("Invalid camera source timestamp")
        lidar_ids = sidecar.get("lidar_ids")
        if not isinstance(lidar_ids, dict) or not lidar_ids or any(key not in {str(i) for i in range(5)} or not isinstance(name, str) or name not in config["lidars"] for key, name in lidar_ids.items()) or len(set(lidar_ids.values())) != len(lidar_ids):
            raise ValueError("Invalid sidecar LiDAR ID-to-calibration mapping")
        view = sidecar.get("pcld_view")
        from .structured import _finite_vector
        if not isinstance(view, dict) or any(not _finite_vector(view.get(key), 3) or not np.allclose(view[key], config["cameras"][camera]["view"][key], atol=1e-6, rtol=0)
            for key in ("origin", "x-axis", "y-axis")):
            raise ValueError("Point-cloud view and selected camera calibration disagree")
        inputs.append((pair, paths, sidecar))
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config_path, output / "cams_lidars.json")
    provenance = {"dataset": "A2D2", "source": SOURCE, "sequence": sequence, "camera_view": camera,
                  "calibration_sha256": sha256_file(config_path), "split": "camera_lidar (sequential, unlabelled)"}
    calibration_annotation = save_annotation(output / "cams_lidars.json", output / "metadata/cams_lidars.json", provenance=provenance)
    annotations = [{"json": calibration_annotation["json"], "xml": calibration_annotation["xml"]}]
    pairs, warnings, observed_lidars = [], [], set()
    for pair, paths, sidecar in inputs:
        frame_id = f"{pair['frame']:09d}"
        arrays, mapping = load_cloud(paths["lidar"])
        cloud = summarize_cloud(arrays, mapping, max_rows=max(1, len(arrays["points"])))
        if any(key not in sidecar["lidar_ids"] for key in cloud["lidar_counts"]):
            raise ValueError("Observed LiDAR ID absent from sidecar mapping")
        observed_lidars.update(sidecar["lidar_ids"][key] for key in cloud["lidar_counts"])
        quality = list(cloud["validation_errors"])
        artifacts = {}
        for role, path in paths.items():
            relative = f"inputs/{frame_id}/{path.name}"
            destination = safe_child(output, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            result = save_annotation(destination, output / "metadata" / frame_id / path.name,
                provenance={**provenance, "frame_id": pair["frame"], "modality": "camera" if role == "image" else "lidar" if role == "lidar" else "association_metadata",
                    "original_path": str(path), "camera_timestamp_raw_us": sidecar["cam_tstamp"],
                    "related_camera": pair["image"], "related_lidar": pair["lidar"]})
            annotations.append({"json": result["json"], "xml": result["xml"]})
            artifacts[role] = {"file": relative, "sha256": result["metadata"]["file"]["sha256"],
                "metadata_json": Path(result["json"]).relative_to(output).as_posix(),
                "metadata_xml": Path(result["xml"]).relative_to(output).as_posix(), "annotation_status": result["metadata"]["annotation_status"]}
            if result["metadata"]["annotation_status"] != "ok":
                quality.append(f"{role} metadata has diagnostics")
            if role == "image":
                image_info = result["metadata"].get("image", {})
                if [image_info.get("width"), image_info.get("height")] != config["cameras"][camera]["Resolution"]:
                    quality.append("Image dimensions disagree with calibration Resolution")
        camera_time = sidecar["cam_tstamp"]
        timing = {"camera_timestamp_raw_us": camera_time,
            "lidar_timestamp_raw_us": statistics(arrays["timestamp"]),
            "lidar_rectime_raw_us": statistics(arrays["rectime"]) if "rectime" in arrays else None,
            "rectime_minus_camera_us": statistics(arrays["rectime"].astype("int64") - camera_time) if "rectime" in arrays else None,
            "rectime_minus_timestamp_us": statistics(arrays["rectime"].astype("int64") - arrays["timestamp"].astype("int64")) if "rectime" in arrays else None,
            "camera_tstamp_delay_us": config["cameras"][camera]["tstamp_delay"],
            "camera_delay_applied": False, "clock_conversion_applied": False,
            "interpretation": "rectime differences compare recorded/receive times, not calibrated acquisition-time latency. timestamp uses a distinct source clock; no assumed UTC/TAI correction or delay sign."}
        width, height = config["cameras"][camera]["Resolution"]
        valid_pixels = np.isfinite(arrays["row"]) & np.isfinite(arrays["col"])
        in_bounds = valid_pixels & (arrays["row"] >= 0) & (arrays["row"] < height) & (arrays["col"] >= 0) & (arrays["col"] < width)
        point_csv = None
        point_csv_preview = None
        point_csv_metadata = None
        if export_points:
            point_csv = f"points/{frame_id}.csv"
            point_csv_preview = f"points/{frame_id}.preview.csv"
            target = safe_child(output, point_csv)
            preview = safe_child(output, point_csv_preview)
            target.parent.mkdir(parents=True, exist_ok=True)
            columns = [key for key in arrays if key != "points"]
            with target.open("w", encoding="utf-8", newline="") as stream, preview.open("w", encoding="utf-8", newline="") as preview_stream:
                writer, preview_writer = csv.writer(stream), csv.writer(preview_stream)
                header = ["source_point_index", "x", "y", "z", *columns]
                writer.writerow(header)
                preview_writer.writerow(header)
                for i, point in enumerate(arrays["points"]):
                    row = [i, *point.tolist(), *[arrays[key][i].item() for key in columns]]
                    writer.writerow(row)
                    if i < 100:
                        preview_writer.writerow(row)
            result = save_annotation(target, output / "metadata/points" / target.name,
                provenance={**provenance, "frame_id": pair["frame"], "source_npz_sha256": artifacts["lidar"]["sha256"],
                    "role": "lossless numeric point-table export", "units": cloud["units"],
                    "source_fields": mapping, "nonfinite_policy": "CSV retains nan/inf text; JSON reports counts"})
            annotations.append({"json": result["json"], "xml": result["xml"]})
            point_csv_metadata = Path(result["json"]).relative_to(output).as_posix()
            if result["metadata"]["annotation_status"] != "ok":
                quality.append("Point CSV metadata has diagnostics")
        per_sensor = []
        for identifier, count in cloud["lidar_counts"].items():
            mask = arrays["lidar_id"] == int(identifier)
            per_sensor.append({"lidar_id": int(identifier), "sensor_name": sidecar["lidar_ids"][identifier], "point_count": count,
                "rectime_minus_camera_us": statistics(arrays["rectime"][mask].astype("int64") - camera_time) if "rectime" in arrays else None})
        pairs.append({"frame_id": pair["frame"], "camera_name": camera, "status": "partial" if quality else "ok",
            "quality_warnings": quality, "artifacts": artifacts, "point_csv": point_csv,
            "point_csv_sha256": sha256_file(output / point_csv) if point_csv else None,
            "point_csv_metadata_json": point_csv_metadata, "point_csv_preview": point_csv_preview,
            "preview_rows": min(100, len(arrays["points"])) if export_points else 0,
            "lidar": cloud, "physical_lidar_sensors": per_sensor, "timing": timing,
            "pcld_view": sidecar["pcld_view"], "calibration_file": "cams_lidars.json",
            "pixel_mapping": {"finite_points": int(valid_pixels.sum()), "in_bounds_points": int(in_bounds.sum()),
                "image_space": "Publisher row/col refer to undistorted images. Original PNGs are kept unchanged; no overlay or reprojection validation."}})
        warnings.extend(f"Frame {pair['frame']}: {warning}" for warning in quality)
    report = {"schema_id": "urn:scenario-metadata:a2d2-fusion:v1", "schema_version": "1.0", "implementation": "paper-inspired-local",
        "status": "completed_with_warnings" if warnings else "passed", "provenance": provenance,
        "paired_frames": len(pairs), "total_point_rows": sum(pair["lidar"]["point_count"] for pair in pairs),
        "selected_camera_count": 1, "observed_physical_lidar_sensors": sorted(observed_lidars),
        "modalities": {"camera": "available", "lidar": "available", "radar": "not_in_A2D2", "vehicle_bus": "not_downloaded"},
        "calibration": {"file": "cams_lidars.json", "camera_count": calibration["camera_count"], "lidar_count": calibration["lidar_count"],
            "camera_intrinsics": "available", "sensor_poses": "available", "measurement_covariances": None, "clock_uncertainty": None},
        "association_method": "Same view/sequence/frame plus explicit camera JSON image_png and pcld_npz references; physical LiDAR IDs resolved through sidecar lidar_ids.",
        "data_quality_warnings": warnings, "pairs": pairs, "annotations": annotations,
        "limitations": ["Point clouds are processed and registered to camera views; not independent raw LiDAR scans.",
            "Points may recur across adjacent frames or views; point rows are not a count of unique physical returns.",
            "Acquisition clocks, camera delay sign and processing delays are not calibrated by this pipeline.",
            "Publisher blurs faces and number plates. No independent reprojection accuracy, object association or uncertainty propagation is evaluated.",
            "Chat2Scenario is not run on these sensor inputs."]}
    write_json(output / "fusion_manifest.json", report)
    (output / "fusion_manifest.xml").write_text(metadata_to_xml(report), encoding="utf-8")
    with (output / "fusion_pairs.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["frame_id", "camera", "camera_timestamp_raw_us", "lidar_points", "lidar_sensor_count", "rectime_minus_camera_min_us", "rectime_minus_camera_max_us", "camera_delay_unapplied_us", "status"])
        for pair in pairs:
            offset = pair["timing"]["rectime_minus_camera_us"] or {}
            writer.writerow([pair["frame_id"], camera, pair["timing"]["camera_timestamp_raw_us"], pair["lidar"]["point_count"],
                len(pair["physical_lidar_sensors"]), offset.get("min"), offset.get("max"), pair["timing"]["camera_tstamp_delay_us"], pair["status"]])
    write_json(output / "a2d2_report.json", {key: value for key, value in report.items() if key not in ("pairs", "annotations")})
    return report
