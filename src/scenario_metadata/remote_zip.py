"""Bounded HTTPS Range reader used for selective access to the official ZIP.

Never falls back to downloading the entire archive when Range is ignored.
ZIP CRC validates extracted members; it is not the whole-archive MD5 check.
"""
from __future__ import annotations

import io
import re
import time
from collections import OrderedDict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class HTTPRangeReader(io.RawIOBase):
    def __init__(self, url: str, size: int, budget: int = 250_000_000):
        super().__init__()
        if not url.startswith("https://") or size <= 0 or budget <= 0:
            raise ValueError("An HTTPS URL and positive archive size/budget are required")
        self.url, self.size, self.budget = url, size, budget
        self.position = self.transferred = self.requests = 0
        self.cache = OrderedDict()

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        position = offset + (0 if whence == io.SEEK_SET else self.position if whence == io.SEEK_CUR else self.size)
        if whence not in (io.SEEK_SET, io.SEEK_CUR, io.SEEK_END) or position < 0:
            raise ValueError("Invalid seek")
        self.position = position
        return position

    def _fetch(self, start: int, end: int) -> bytes:
        expected = end - start + 1
        error = None
        for attempt in range(3):
            if self.transferred + expected > self.budget:
                raise RuntimeError("Download budget exceeded; raise --max-download-mb or select fewer sequences")
            request = Request(self.url, headers={"Range": f"bytes={start}-{end}",
                "Accept-Encoding": "identity", "User-Agent": "scenario-metadata-pipeline/0.1"})
            try:
                self.requests += 1
                with urlopen(request, timeout=90) as response:
                    if response.status != 206:
                        raise RuntimeError("Server did not honor Range; refusing a full-archive fallback. Download the official ZIP manually and use --archive.")
                    if not response.geturl().startswith("https://"):
                        raise RuntimeError("Refusing non-HTTPS redirect")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if not match or tuple(map(int, match.groups())) != (start, end, self.size):
                        raise RuntimeError("Unexpected Content-Range or changed archive size")
                    payload = response.read(expected + 1)
                    self.transferred += len(payload)
                    if len(payload) != expected:
                        raise OSError("Truncated or oversized range response")
                    return payload
            except (HTTPError, URLError, OSError) as exc:
                error = exc
                if isinstance(exc, HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                    break
                if attempt < 2:
                    delay = min(60, int(exc.headers.get("Retry-After", "2"))) if isinstance(exc, HTTPError) and exc.headers.get("Retry-After", "2").isdigit() else 2
                    time.sleep(max(1, delay))
        raise RuntimeError(f"Range download failed: {error}") from error

    def read(self, size=-1):
        if size is None or size < 0:
            size = self.size - self.position
        size = min(size, max(0, self.size-self.position))
        if size == 0:
            return b""
        start = self.position
        for offset, data in list(self.cache.items()):
            if offset <= start and start+size <= offset+len(data):
                self.cache.move_to_end(offset)
                self.position += size
                return data[start-offset:start-offset+size]
        # Small ZIP header reads share a 64 KiB block; payloads are read as asked.
        fetch_start = (start // 65536) * 65536 if size < 32768 else start
        fetch_end = min(self.size-1, max(start+size-1, fetch_start+65535) if size < 32768 else start+size-1)
        data = self._fetch(fetch_start, fetch_end)
        if len(data) <= 131072:
            self.cache[fetch_start] = data
            if len(self.cache) > 8:
                self.cache.popitem(last=False)
        self.position += size
        return data[start-fetch_start:start-fetch_start+size]
