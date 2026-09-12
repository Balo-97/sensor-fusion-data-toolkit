"""Bounded, checksum-verified downloads. Dataset URLs are pinned in the manifest."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.request import Request, urlopen


def load_manifest(path: Path | None = None) -> dict:
    path = path or Path(__file__).with_name("datasets.json")
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_child(root: Path, relative: str) -> Path:
    name = PurePosixPath(relative)
    if not relative or name.is_absolute() or ".." in name.parts or "\\" in relative or ":" in relative:
        raise ValueError(f"Unsafe relative path: {relative!r}")
    resolved_root = root.resolve()
    target = (resolved_root / relative).resolve()
    if not target.is_relative_to(resolved_root) or target == resolved_root:
        raise ValueError(f"Path escapes destination: {relative!r}")
    return target


def download_file(url: str, destination: Path, expected_sha256: str, max_bytes: int,
                  expected_size: int | None = None, retries: int = 3) -> dict:
    if not url.startswith("https://"):
        raise ValueError("Downloads require HTTPS URLs")
    if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
        raise ValueError("A lowercase SHA-256 digest is required")
    if max_bytes <= 0 or retries < 1:
        raise ValueError("max_bytes and retries must be positive")
    if destination.is_file() and destination.stat().st_size <= max_bytes:
        if sha256_file(destination) == expected_sha256:
            return {"path": str(destination.resolve()), "sha256": expected_sha256,
                    "size_bytes": destination.stat().st_size, "cached": True, "url": url}
    destination.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    last_error = None
    for attempt in range(retries):
        temporary = None
        try:
            request = Request(url, headers={"User-Agent": "scenario-metadata-pipeline/0.1"})
            with urlopen(request, timeout=60) as response:
                if not response.geturl().startswith("https://"):
                    raise ValueError("Refusing an HTTPS to HTTP redirect")
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise ValueError(f"Download exceeds limit of {max_bytes} bytes")
                digest = hashlib.sha256()
                size = 0
                with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as stream:
                    temporary = Path(stream.name)
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError(f"Download exceeds limit of {max_bytes} bytes")
                        stream.write(chunk)
                        digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise ValueError(f"Checksum mismatch for {destination.name}; received content is not the pinned sample")
            if expected_size is not None and size != expected_size:
                raise ValueError(f"Size mismatch: expected {expected_size}, received {size}")
            temporary.replace(destination)
            return {"path": str(destination.resolve()), "sha256": digest.hexdigest(),
                    "size_bytes": size, "cached": False, "url": url}
        except (OSError, URLError, ValueError) as exc:
            last_error = exc
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if isinstance(exc, ValueError):
                break
            if attempt + 1 < retries:
                time.sleep(0.5 * 2 ** attempt)
    raise RuntimeError(f"Download failed for {url}: {last_error}") from last_error


def selected_datasets(manifest: dict, dataset_ids: list[str] | None = None) -> list[dict]:
    datasets = manifest["datasets"]
    known = {entry["id"] for entry in datasets}
    unknown = set(dataset_ids or []) - known
    if unknown:
        raise ValueError(f"Unknown datasets: {', '.join(sorted(unknown))}")
    return [entry for entry in datasets if not dataset_ids or entry["id"] in dataset_ids]


def download_dataset(dataset: dict, data_dir: Path, max_bytes: int = 100_000_000) -> list[dict]:
    root = safe_child(data_dir, dataset["id"])
    return [download_file(item["url"], safe_child(root, item["path"]), item["sha256"], max_bytes,
                          item.get("size_bytes")) for item in dataset["files"]]
