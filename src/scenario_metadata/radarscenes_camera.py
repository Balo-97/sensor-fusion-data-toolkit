"""Preserve publisher radar/camera associations for fusion data-quality studies."""
from __future__ import annotations

import csv
import shutil
from collections import Counter
from pathlib import Path

from .downloads import safe_child
from .metadata import metadata_to_xml
from .pipeline import save_annotation, write_json
from .radarscenes import camera_timestamp

FORMAT_SOURCE = "https://radar-scenes.com/dataset/structure/"


def annotate_camera_pairs(root: Path, sequence: str, output: Path, scenes: list[dict],
                          provenance: dict, max_offset_ms: float | None):
    images, annotations, warnings, pairs = {}, [], [], []
    for scene in scenes:
        name = scene["image_name"]
        timestamp = camera_timestamp(name)
        if name in images:
            continue
        source = safe_child(root, f"data/{sequence}/camera/{name}")
        record = {"image_name": name, "source_path": str(source), "timestamp_us": timestamp,
                  "timestamp_source": "publisher timestamp-named JPEG; arbitrary recording clock",
                  "file": None, "metadata_json": None, "metadata_xml": None,
                  "sha256": None, "status": "missing"}
        if source.is_file():
            destination = safe_child(output, f"camera/{name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            result = save_annotation(destination, output / "metadata" / "camera" / name,
                provenance={**provenance, "sensor_modality": "camera", "original_path": str(source),
                    "capture_timestamp_us": timestamp, "capture_timestamp_source": "JPEG filename, not filesystem time",
                    "timestamp_epoch": "arbitrary origin", "image_role": "documentary camera",
                    "privacy_processing": "Publisher describes repainted regions; masks are not supplied by this pipeline."})
            annotations.append({"json": result["json"], "xml": result["xml"]})
            record.update({"file": f"camera/{name}", "metadata_json": f"metadata/camera/{name}.metadata.json",
                "metadata_xml": f"metadata/camera/{name}.metadata.xml",
                "sha256": result["metadata"]["file"]["sha256"],
                "status": "ok" if result["metadata"]["annotation_status"] == "ok" else "invalid_image"})
        images[name] = record
    for scene in scenes:
        image = images[scene["image_name"]]
        radar_time = scene["timestamp"]
        delta = image["timestamp_us"] - radar_time
        within = abs(delta) <= max_offset_ms * 1000 if max_offset_ms is not None else None
        status = image["status"] if image["status"] != "ok" else "outside_tolerance" if within is False else "available"
        pair = {"radar_timestamp_us": radar_time, "radar_sensor_id": scene["sensor_id"],
            "source_radar_indices": scene["source_radar_indices"],
            "exported_radar_indices": scene["exported_radar_indices"],
            "radar_metadata_json": "metadata/radar_data.h5.metadata.json",
            "radar_nonfinite_counts": scene["radar_nonfinite_counts"],
            "radar_calibration_json": "sensors.json", "radar_pose_key": f"radar_{scene['sensor_id']}",
            "camera_image_name": scene["image_name"], "camera_file": image["file"],
            "camera_metadata_json": image["metadata_json"], "camera_sha256": image["sha256"],
            "camera_status": image["status"], "camera_timestamp_us": image["timestamp_us"],
            "camera_minus_radar_us": delta, "abs_camera_offset_us": abs(delta),
            "within_camera_time_tolerance": within,
            "odometry_timestamp_us": scene["odometry_timestamp"],
            "odometry_minus_radar_us": scene["odometry_timestamp"] - radar_time,
            "exported_odometry_row": scene["exported_odometry_row"],
            "lidar_status": "not_provided_by_dataset", "pair_status": status}
        pairs.append(pair)
        scene.update({"camera_image_included": image["file"] is not None,
            "camera_file": image["file"], "camera_metadata_json": image["metadata_json"],
            "camera_timestamp_us": image["timestamp_us"], "camera_minus_radar_us": delta,
            "camera_pair_status": status})
    uses = Counter(pair["camera_image_name"] for pair in pairs)
    for pair in pairs:
        pair["scenes_sharing_camera_image"] = uses[pair["camera_image_name"]]
    missing = sum(item["status"] == "missing" for item in images.values())
    invalid = sum(item["status"] == "invalid_image" for item in images.values())
    outside = sum(pair["within_camera_time_tolerance"] is False for pair in pairs)
    if missing:
        warnings.append(f"{missing} referenced camera images are missing locally; run the same selection with 'radarscenes run' to download them")
    if invalid:
        warnings.append(f"{invalid} camera images failed decoding; inspect their image metadata diagnostics")
    if outside:
        warnings.append(f"{outside} radar/camera associations exceed the requested {max_offset_ms:g} ms tolerance; pairs retained")
    summary = {"requested": True, "unique_images_referenced": len(images),
        "unique_images_present": len(images) - missing, "unique_images_valid": len(images) - missing - invalid,
        "missing_images": missing, "invalid_images": invalid,
        "scenes_with_valid_images": sum(pair["camera_status"] == "ok" for pair in pairs),
        "shared_images": sum(count > 1 for count in uses.values()),
        "max_absolute_time_offset_us": max(pair["abs_camera_offset_us"] for pair in pairs),
        "max_camera_offset_ms": max_offset_ms, "outside_tolerance_scenes": outside,
        "warnings": warnings, "fusion_manifest": "fusion_manifest.json", "pair_table": "fusion_pairs.csv"}
    manifest = {"schema_id": "urn:scenario-metadata:fusion-pairs:v1", "schema_version": "1.0",
        "implementation": "paper-inspired-local", "dataset": "RadarScenes", "sequence": sequence,
        "provenance": provenance, "format_source": FORMAT_SOURCE,
        "modalities": {"radar": "present", "camera": summary,
                       "lidar": {"available": False, "reason": "Not included in the RadarScenes release", "source": FORMAT_SOURCE}},
        "association_method": "Publisher scenes.json image_name, described as nearest camera timestamp; not recomputed from all images.",
        "time_semantics": "Microseconds on the dataset's arbitrary recording clock; positive camera_minus_radar_us means camera occurred later. Offsets do not prove hardware synchronization.",
        "camera_time_tolerance_ms": max_offset_ms, "camera_time_tolerance_evaluated": max_offset_ms is not None,
        "calibration_availability": {"radar_2d_mounting_poses": "sensors.json",
            "camera_intrinsics": None, "radar_to_camera_extrinsics": None,
            "measurement_covariances": None, "clock_uncertainty": None},
        "research_limitations": ["Camera is documentary and publisher reports repainted privacy regions.",
            "Radar/image field-of-view overlap and geometric projection have not been validated.",
            "Source track IDs are radar object labels, not cross-modal object correspondences.",
            "No sensor fusion or error propagation is computed; projection calibration, uncertainty models and evaluation ground truth are still required."],
        "images": list(images.values()), "pairs": pairs}
    write_json(output / "fusion_manifest.json", manifest)
    (output / "fusion_manifest.xml").write_text(metadata_to_xml(manifest), encoding="utf-8")
    fields = ["radar_timestamp_us", "radar_sensor_id", "camera_image_name", "camera_file", "camera_status",
        "camera_timestamp_us", "camera_minus_radar_us", "abs_camera_offset_us", "within_camera_time_tolerance",
        "odometry_timestamp_us", "odometry_minus_radar_us", "scenes_sharing_camera_image", "pair_status", "lidar_status"]
    with (output / "fusion_pairs.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(pairs)
    return summary, annotations
