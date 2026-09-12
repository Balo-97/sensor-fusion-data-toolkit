"""Small paired downloads from the A2D2 public per-file mirror."""
from __future__ import annotations

import hashlib
import re
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .downloads import safe_child, sha256_file

BASE = "https://audi-autonomous-driving-dataset.s3.eu-central-1.amazonaws.com/"
SOURCE = "https://registry.opendata.aws/aev-a2d2/"
CAMERAS = ("front_center", "front_left", "front_right", "side_left", "side_right", "rear_center")


def selection(sequence: str, camera: str, start_frame: int, max_frames: int) -> list[dict]:
    if not re.fullmatch(r"[0-9]{8}_[0-9]{6}", sequence) or camera not in CAMERAS:
        raise ValueError("Use an A2D2 YYYYMMDD_HHMMSS sequence and a supported camera view")
    if start_frame < 0 or not 1 <= max_frames <= 1000 or start_frame + max_frames > 1_000_000_000:
        raise ValueError("Use a nonnegative --start-frame and --max-frames between 1 and 1000")
    result = []
    for frame in range(start_frame, start_frame + max_frames):
        stem = f"{sequence.replace('_', '')}_camera_{camera.replace('_', '')}_{frame:09d}"
        lidar = stem.replace("_camera_", "_lidar_") + ".npz"
        folder = f"camera_lidar/{sequence}"
        result.append({"frame": frame, "camera": camera, "image": f"{folder}/camera/cam_{camera}/{stem}.png",
            "sidecar": f"{folder}/camera/cam_{camera}/{stem}.json", "lidar": f"{folder}/lidar/cam_{camera}/{lidar}"})
    return result


def md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_object(key: str) -> dict:
    # Keys come only from the fixed source layout, never caller-provided URLs.
    with urlopen(Request(BASE + key, method="HEAD"), timeout=60) as response:
        if response.status != 200 or not response.geturl().startswith(BASE):
            raise ValueError("Unexpected A2D2 object response")
        etag = response.headers.get("ETag", "").strip('"')
        if not re.fullmatch(r"[0-9a-f]{32}", etag):
            raise ValueError("A2D2 object has no supported single-part MD5 ETag; cannot verify this downloader's integrity contract")
        size = int(response.headers["Content-Length"])
        if size <= 0:
            raise ValueError("Empty A2D2 object")
        return {"key": key, "url": BASE + key, "size_bytes": size, "etag_md5": etag}


def fetch_object(item: dict, root: Path, counters: dict, budget: int) -> dict:
    destination = safe_child(root, item["key"])
    cached = destination.is_file() and destination.stat().st_size == item["size_bytes"] and md5_file(destination) == item["etag_md5"]
    if not cached:
        destination.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            if counters["transferred_bytes"] + item["size_bytes"] > budget:
                raise ValueError("A2D2 download budget exhausted; increase --max-download-mb")
            temporary = None
            try:
                request = Request(item["url"], headers={"If-Match": '"' + item["etag_md5"] + '"',
                    "User-Agent": "scenario-metadata-pipeline/0.1", "Accept-Encoding": "identity"})
                with urlopen(request, timeout=60) as response:
                    counters["get_requests"] += 1
                    if response.status != 200 or not response.geturl().startswith(BASE):
                        raise ValueError("Unexpected A2D2 download response")
                    if response.headers.get("ETag", "").strip('"') != item["etag_md5"]:
                        raise ValueError("A2D2 object changed after discovery")
                    digest, size = hashlib.md5(usedforsecurity=False), 0
                    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as target:
                        temporary = Path(target.name)
                        while block := response.read(min(1024 * 1024, item["size_bytes"] - size + 1)):
                            size += len(block)
                            counters["transferred_bytes"] += len(block)
                            if size > item["size_bytes"] or counters["transferred_bytes"] > budget:
                                raise ValueError("A2D2 response exceeded size or download budget")
                            target.write(block)
                            digest.update(block)
                if size != item["size_bytes"] or digest.hexdigest() != item["etag_md5"]:
                    raise ValueError("A2D2 size/MD5 mismatch")
                temporary.replace(destination)
                break
            except (OSError, URLError) as exc:
                if attempt == 2 or isinstance(exc, HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                    raise
                time.sleep(1 + attempt)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
    return {**item, "path": str(destination), "sha256": sha256_file(destination), "cached": cached}


def download_a2d2(root: Path, *, sequence: str = "20180810_150607", camera: str = "front_center",
                   start_frame: int = 60, max_frames: int = 3, budget: int = 100_000_000,
                   max_file_bytes: int = 25_000_000) -> dict:
    from .pipeline import write_json
    pairs = selection(sequence, camera, start_frame, max_frames)
    if budget <= 0 or max_file_bytes <= 0:
        raise ValueError("Download limits must be positive")
    keys = ["LICENSE.txt", "README.txt", "cams_lidars.json"]
    keys += [pair[role] for pair in pairs for role in ("image", "sidecar", "lidar")]
    objects = []
    for key in keys:
        try:
            item = describe_object(key)
        except HTTPError as exc:
            if exc.code == 404:
                raise ValueError(f"Requested A2D2 file is unavailable: {key}; frame IDs may have gaps or start at a different value for this view") from exc
            raise
        if item["size_bytes"] > max_file_bytes:
            raise ValueError(f"A2D2 file exceeds --max-file-mb: {key}")
        objects.append(item)
    if sum(item["size_bytes"] for item in objects) > budget:
        raise ValueError("Selected A2D2 files exceed --max-download-mb (selection-size guard includes cached files)")
    counters = {"transferred_bytes": 0, "get_requests": 0}
    files = [fetch_object(item, root, counters, budget) for item in objects]
    report = {"status": "passed", "dataset": "A2D2", "source": SOURCE, "sequence": sequence,
        "camera": camera, "pairs": pairs, "files": files, **counters,
        "head_requests": len(objects), "selected_bytes": sum(item["size_bytes"] for item in objects),
        "integrity": "Size and content MD5 verified against HTTPS S3 single-part ETag, with If-Match on GET. Local SHA256 recorded. ETags are not publisher-authenticated signatures.",
        "publisher_checksums_txt_verified": False,
        "scope": "Exact requested camera-view frame IDs and their paired LiDAR; no archives, bus streams or labels downloaded"}
    write_json(root / "download_report.json", report)
    return report
