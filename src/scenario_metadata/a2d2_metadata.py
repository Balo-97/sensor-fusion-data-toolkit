"""A2D2 NPZ and full camera/LiDAR configuration metadata.

Original and prefixed NPZ keys are kept distinct in source_fields. Point clouds
are already registered to a camera view, not raw individual VLP-16 packets.
"""
from __future__ import annotations

import math
import zipfile
from pathlib import Path

TUTORIAL = "https://audi-autonomous-driving-dataset.s3.eu-central-1.amazonaws.com/tutorial.ipynb"
REQUIRED = {"points", "reflectance", "timestamp", "row", "col", "distance", "depth", "lidar_id"}
OPTIONAL = {"rectime", "valid", "boundary", "azimuth"}
MAX_NPZ_BYTES = 128 * 1024 * 1024
MAX_POINTS = 1_000_000


def numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise ImportError('A2D2 point clouds require: python -m pip install -e ".[a2d2]"') from exc
    return np


def load_cloud(path: Path) -> tuple[dict, dict]:
    np = numpy()
    if path.stat().st_size > MAX_NPZ_BYTES:
        raise ValueError("A2D2 NPZ exceeds 128 MiB")
    mapping, shapes = {}, {}
    with zipfile.ZipFile(path) as zipped:
        infos = zipped.infolist()
        if len(infos) > 16 or sum(info.file_size for info in infos) > MAX_NPZ_BYTES:
            raise ValueError("A2D2 NPZ exceeds array/member budget")
        for info in infos:
            name = info.filename
            if not name.endswith(".npy") or "/" in name or "\\" in name or info.flag_bits & 1:
                raise ValueError("Unexpected NPZ member")
            key = name[:-4]
            canonical = "points" if key == "pcloud_points" else key.removeprefix("pcloud_attr.")
            if canonical in mapping or canonical not in REQUIRED | OPTIONAL:
                raise ValueError("Duplicate or unsupported A2D2 NPZ field")
            with zipped.open(info) as stream:
                version = np.lib.format.read_magic(stream)
                if version == (1, 0):
                    shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
                elif version == (2, 0):
                    shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
                else:
                    raise ValueError("Unsupported NPY header version")
                if dtype.kind not in "biuf" or dtype.itemsize > 8 or dtype.hasobject:
                    raise ValueError("Only plain numeric A2D2 arrays are supported; pickle is disabled")
                valid_shape = len(shape) == 2 and shape[1] == 3 if canonical == "points" else len(shape) == 1
                if not valid_shape:
                    raise ValueError(f"Wrong shape for A2D2 {canonical}")
                if not 0 <= shape[0] <= MAX_POINTS or math.prod(shape) * dtype.itemsize != info.file_size - stream.tell():
                    raise ValueError("NPZ array size/header mismatch or point limit exceeded")
                shapes[canonical] = shape
            mapping[canonical] = key
        if not REQUIRED <= mapping.keys() or len({shape[0] for shape in shapes.values()}) != 1:
            raise ValueError("Missing A2D2 fields or inconsistent point-array lengths")
    with np.load(path, allow_pickle=False) as source:
        arrays = {canonical: source[key] for canonical, key in mapping.items()}
    for key in ("timestamp", "rectime", "lidar_id"):
        if key in arrays and (arrays[key].dtype.kind not in "iu" or (arrays[key] < 0).any()
                              or (arrays[key] > np.iinfo(np.int64).max).any()):
            raise ValueError(f"{key} must contain nonnegative signed-int64-compatible integers")
    if "valid" in arrays and arrays["valid"].dtype.kind != "b":
        raise ValueError("A2D2 valid field must be boolean")
    return arrays, mapping


def statistics(values) -> dict:
    np = numpy()
    finite = np.isfinite(values)
    valid = values[finite]
    return {"count": int(values.size), "nonfinite_count": int((~finite).sum()),
            "min": valid.min().item() if valid.size else None,
            "max": valid.max().item() if valid.size else None}


def summarize_cloud(arrays: dict, mapping: dict, max_rows: int = 100_000) -> dict:
    np = numpy()
    if max_rows <= 0:
        raise ValueError("max_rows must be positive")
    n = len(arrays["points"])
    sampled = {key: value[:max_rows] for key, value in arrays.items()}
    fields = {key: statistics(value) for key, value in sampled.items() if key != "points"}
    fields.update({axis: statistics(sampled["points"][:, i]) for i, axis in enumerate(("x", "y", "z"))})
    errors = [f"Nonfinite {key}: {entry['nonfinite_count']}" for key, entry in fields.items() if entry["nonfinite_count"]]
    ids, counts = np.unique(sampled["lidar_id"], return_counts=True)
    if any(int(value) not in range(5) for value in ids):
        errors.append("Unknown LiDAR sensor IDs")
    if bool((sampled["distance"] < 0).any()):
        errors.append("Negative point distance")
    return {"parser": "a2d2-npz-v1", "status": "partial" if errors else "ok", "validation_errors": errors,
        "point_count": n, "points_sampled": min(n, max_rows), "sample_limited": n > max_rows,
        "source_fields": mapping, "array_shapes": {key: list(value.shape) for key, value in arrays.items()},
        "fields": fields, "lidar_counts": {str(int(i)): int(c) for i, c in zip(ids, counts)},
        "reported_invalid_points": int((~sampled["valid"]).sum()) if "valid" in sampled else None,
        "representation": "Processed points registered to a camera view; multiple physical LiDAR units may contribute",
        "coordinate_frame": "Matching camera sidecar pcld_view, not native LiDAR coordinates",
        "units": {"points": "m", "distance": "m", "depth": "m", "row": "undistorted image pixels",
            "col": "undistorted image pixels", "reflectance": "source intensity values; not radiometrically calibrated here",
            "timestamp": "microseconds; retained source clock", "rectime": "microseconds; retained recording/receive time"},
        "calibration_applied": False, "clock_conversion_applied": False,
        "scope": "Statistics use the bounded prefix. Counts per sensor refer to sampled rows; point_count is the full shape."}


def lidar_metadata(path: Path) -> dict:
    return summarize_cloud(*load_cloud(path))


def full_calibration(data: dict) -> dict:
    from .structured import _finite_vector, _matrix
    if not all(isinstance(data.get(key), dict) and data[key] for key in ("vehicle", "cameras", "lidars")):
        raise ValueError("A2D2 configuration requires vehicle, cameras and lidars objects")
    errors = []
    def check_view(view, label):
        if not isinstance(view, dict) or not all(_finite_vector(view.get(key), 3) for key in ("origin", "x-axis", "y-axis")):
            errors.append(f"{label}: invalid view vectors")
        else:
            x, y = view["x-axis"], view["y-axis"]
            if abs(sum(v*v for v in x)-1) > .001 or abs(sum(v*v for v in y)-1) > .001 or abs(sum(a*b for a,b in zip(x,y))) > .001:
                errors.append(f"{label}: view axes are not orthonormal")
    check_view(data["vehicle"].get("view"), "vehicle")
    for group in ("cameras", "lidars"):
        for name, sensor in data[group].items():
            if not isinstance(sensor, dict):
                raise ValueError("Invalid sensor configuration object")
            check_view(sensor.get("view"), name)
            if group == "cameras":
                for key in ("CamMatrix", "CamMatrixOriginal"):
                    matrix = sensor.get(key)
                    if not _matrix(matrix, 3) or matrix[0][0] <= 0 or matrix[1][1] <= 0 or any(abs(a-b) > 1e-6 for a,b in zip(matrix[2], [0,0,1])):
                        errors.append(f"{name}: invalid {key}")
                resolution = sensor.get("Resolution")
                if not isinstance(resolution, list) or len(resolution) != 2 or any(type(x) is not int or x <= 0 for x in resolution):
                    errors.append(f"{name}: invalid Resolution")
                distortion = sensor.get("Distortion")
                if not isinstance(distortion, list) or len(distortion) != 1 or not isinstance(distortion[0], list) or not _finite_vector(distortion[0], len(distortion[0])) or len(distortion[0]) not in (4, 5):
                    errors.append(f"{name}: invalid Distortion")
                delay = sensor.get("tstamp_delay")
                if type(delay) not in (float, int) or not math.isfinite(delay):
                    errors.append(f"{name}: invalid tstamp_delay")
    return {"schema": "a2d2-cams-lidars", "status": "partial" if errors else "ok", "validation_errors": errors,
        "camera_count": len(data["cameras"]), "lidar_count": len(data["lidars"]),
        "cameras": data["cameras"], "lidars": data["lidars"], "vehicle": data["vehicle"],
        "units": {"view.origin": "m", "view.axes": "unit vectors", "CamMatrix": "undistorted-image intrinsic matrix",
            "CamMatrixOriginal": "original-image intrinsic matrix", "Resolution": "[columns, rows] pixels", "tstamp_delay": "microseconds"},
        "transforms_applied": False, "timestamp_delay_applied": False,
        "validation_scope": "Finite values, matrix/vector dimensions and orthonormality; not physical calibration accuracy",
        "reference": TUTORIAL}
