import hashlib
import io
from pathlib import Path
from unittest.mock import patch

import pytest

from scenario_metadata.downloads import download_file, safe_child, selected_datasets


class Response(io.BytesIO):
    headers = {}

    def geturl(self):
        return "https://example.org/sample"


def test_verified_cache_and_corruption_recovery(tmp_path):
    raw = b"a real download substitute"
    digest = hashlib.sha256(raw).hexdigest()
    target = tmp_path / "sample.csv"
    with patch("scenario_metadata.downloads.urlopen", return_value=Response(raw)) as request:
        assert not download_file("https://example.org/sample", target, digest, 100)["cached"]
        assert download_file("https://example.org/sample", target, digest, 100)["cached"]
        assert request.call_count == 1
    target.write_bytes(b"corruption")
    with patch("scenario_metadata.downloads.urlopen", return_value=Response(raw)):
        assert not download_file("https://example.org/sample", target, digest, 100)["cached"]


@pytest.mark.parametrize("kind", ["hash", "limit", "size"])
def test_bad_download_never_replaces_file(tmp_path, kind):
    target = tmp_path / "sample"
    target.write_bytes(b"keep me")
    content = b"download bytes"
    digest = hashlib.sha256(content).hexdigest() if kind != "hash" else "0" * 64
    with patch("scenario_metadata.downloads.urlopen", return_value=Response(content)):
        with pytest.raises(RuntimeError):
            download_file("https://example.org/sample", target, digest,
                          3 if kind == "limit" else 100, 99 if kind == "size" else None)
    assert target.read_bytes() == b"keep me"
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/absolute", "a\\b", ".", ""])
def test_rejects_path_escape(tmp_path, name):
    with pytest.raises(ValueError):
        safe_child(tmp_path, name)


def test_unknown_dataset_is_an_error():
    with pytest.raises(ValueError, match="Unknown datasets"):
        selected_datasets({"datasets": [{"id": "one"}]}, ["typo"])
