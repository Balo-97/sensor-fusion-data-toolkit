"""Select sequence members from the official, version-pinned RadarScenes ZIP."""
from __future__ import annotations

import json
import re
import stat
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path

from .downloads import safe_child, sha256_file
from .remote_zip import HTTPRangeReader

SOURCE = "https://zenodo.org/records/4559821"
ARCHIVE_URL = SOURCE + "/files/RadarScenes.zip?download=1"
ARCHIVE_SIZE = 11_128_859_838
ARCHIVE_MD5 = "db4874b43e45faa0d635bafc58d56579"
PREFIX = "RadarScenes/"


def sequence_name(value: str) -> str:
    if not re.fullmatch(r"sequence_([1-9][0-9]{0,2})", value) or not 1 <= int(value[9:]) <= 158:
        raise ValueError("Expected sequence_1 through sequence_158")
    return value


@contextmanager
def open_archive(archive: Path | None = None, budget: int = 250_000_000):
    reader = archive.open("rb") if archive else HTTPRangeReader(ARCHIVE_URL, ARCHIVE_SIZE, budget)
    with reader:
        with zipfile.ZipFile(reader) as zipped:
            names = zipped.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Duplicate archive member names")
            yield zipped, reader


def _index(zipped: zipfile.ZipFile) -> dict:
    info = zipped.getinfo(PREFIX + "data/sequences.json")
    if info.file_size > 1_000_000:
        raise ValueError("Sequence index is too large")
    value = json.loads(zipped.read(info))
    if not isinstance(value.get("sequences"), dict):
        raise ValueError("Missing RadarScenes sequence index")
    return value


def list_sequences(archive: Path | None = None, budget: int = 250_000_000) -> list[dict]:
    with open_archive(archive, budget) as (zipped, _):
        result = []
        for name, entry in _index(zipped)["sequences"].items():
            sequence_name(name)
            info = zipped.getinfo(PREFIX + f"data/{name}/radar_data.h5")
            result.append({"sequence": name, **entry, "radar_bytes": info.file_size,
                           "compressed_radar_bytes": info.compress_size})
        return sorted(result, key=lambda item: item["compressed_radar_bytes"])


def _crc(path: Path) -> int:
    value = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value = zlib.crc32(block, value)
    return value


def extract_member(zipped: zipfile.ZipFile, member: str, root: Path, max_bytes: int) -> dict:
    if not member.startswith(PREFIX):
        raise ValueError("Unexpected archive prefix")
    destination = safe_child(root, member[len(PREFIX):])
    info = zipped.getinfo(member)
    if info.is_dir() or stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
        raise ValueError("Only regular archive files are supported")
    if info.file_size > max_bytes or info.flag_bits & 1:
        raise ValueError("Archive member exceeds extraction limit or is encrypted")
    cached = (destination.is_file() and destination.stat().st_size == info.file_size
              and _crc(destination) == info.CRC)
    if not cached:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        # Resolve the temporary path too: existing symlinks must not escape root.
        safe_child(root, partial.relative_to(root.resolve()).as_posix())
        try:
            count = 0
            with zipped.open(info) as source, partial.open("wb") as target:
                while block := source.read(1024 * 1024):
                    count += len(block)
                    if count > min(max_bytes, info.file_size):
                        raise ValueError("Extracted member exceeds declared size")
                    target.write(block)
            if count != info.file_size or _crc(partial) != info.CRC:
                raise ValueError("ZIP member integrity check failed")
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)
    return {"member": member, "path": str(destination), "size_bytes": info.file_size,
            "compressed_bytes": info.compress_size, "zip_crc32": f"{info.CRC:08x}",
            "sha256": sha256_file(destination), "cached": cached}


def download_sequences(root: Path, sequences: list[str], *, archive: Path | None = None,
                       budget: int = 250_000_000, max_extracted_bytes: int = 1_000_000_000,
                       include_camera: bool = True, start_scene: int = 0, max_scenes: int = 100,
                       sensor_id: int | None = None) -> dict:
    from .pipeline import write_json
    if not sequences or budget <= 0 or max_extracted_bytes <= 0:
        raise ValueError("Select sequences and positive download/extraction limits")
    if include_camera and (start_scene < 0 or max_scenes <= 0 or sensor_id not in (None, 1, 2, 3, 4)):
        raise ValueError("Invalid camera scene selection")
    sequences = list(dict.fromkeys(sequence_name(value) for value in sequences))
    root = root.resolve()
    with open_archive(archive, budget) as (zipped, reader):
        index = _index(zipped)
        for name in sequences:
            if name not in index["sequences"]:
                raise ValueError(f"Sequence absent from archive: {name}")
        members = [PREFIX + "Readme.md", PREFIX + "data/sensors.json", PREFIX + "data/sequences.json"]
        for name in sequences:
            members.extend(PREFIX + f"data/{name}/{filename}" for filename in ("radar_data.h5", "scenes.json"))
        if sum(zipped.getinfo(member).file_size for member in members) > max_extracted_bytes:
            raise ValueError("Selection exceeds --max-extracted-mb; choose fewer sequences or raise the limit")
        files = [extract_member(zipped, member, root, max_extracted_bytes) for member in members]
        camera_selections, camera_members = [], []
        if include_camera:
            from .radarscenes import read_json, select_scenes, camera_timestamp
            for name in sequences:
                source = read_json(safe_child(root, f"data/{name}/scenes.json"))
                if source.get("sequence_name") != name:
                    raise ValueError("Sequence name mismatch in camera selection")
                _, _, selected = select_scenes(source, start_scene, max_scenes, sensor_id)
                for _, scene in selected:
                    camera_timestamp(scene.get("image_name"))
                names = list(dict.fromkeys(scene["image_name"] for _, scene in selected))
                for image_name in names:
                    camera_timestamp(image_name)
                    camera_members.append(PREFIX + f"data/{name}/camera/{image_name}")
                camera_selections.append({"sequence": name, "selected_scenes": len(selected),
                    "start_scene": start_scene, "max_scenes": max_scenes, "sensor_id": sensor_id,
                    "unique_images": len(names)})
            try:
                total = sum(zipped.getinfo(member).file_size for member in members + camera_members)
            except KeyError as exc:
                raise ValueError(f"Referenced camera image absent from archive: {exc}") from exc
            if total > max_extracted_bytes:
                raise ValueError("Radar plus camera selection exceeds --max-extracted-mb")
            files.extend(extract_member(zipped, member, root, max_extracted_bytes) for member in camera_members)
        report = {"status": "passed", "dataset": "RadarScenes", "source": SOURCE,
            "archive_url": ARCHIVE_URL if archive is None else None,
            "local_archive": str(archive.resolve()) if archive else None,
            "publisher_archive_md5": ARCHIVE_MD5, "publisher_archive_md5_verified": False,
            "integrity": "Each member checked against ZIP CRC32; SHA256 recorded locally, not publisher-authenticated.",
            "sequences": sequences, "files": files,
            "transferred_bytes": getattr(reader, "transferred", 0), "http_requests": getattr(reader, "requests", 0),
            "camera_images_downloaded": bool(camera_members), "camera_selections": camera_selections,
            "unique_camera_images": len(camera_members), "lidar_available": False}
    write_json(root / "download_report.json", report)
    return report
