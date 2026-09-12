"""Local file annotation inspired by Ebrahim et al., not the NPL application.

The paper (https://doi.org/10.1016/j.measen.2024.101458), Table 1 and
Figure 2, describes filesystem, Pillow image, and Velodyne packet metadata.
Its source, XSD and deployed API contract were not supplied. The JSON/XML
schema here is our own: urn:scenario-metadata:local:v1. Checksums, provenance,
parse diagnostics and scenario semantics supplied by the caller are extensions.

Packet facts: Velodyne VLP-16 User Manual, section 9.3, available from
https://data.ouster.io/downloads/velodyne/user-manual/vlp-16-user-manual-revf.pdf
Image decoding: https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html
"""

from __future__ import annotations

import copy
import csv
import hashlib
import ipaddress
import json
import math
import mimetypes
import os
import statistics
import struct
import warnings
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from PIL import Image, UnidentifiedImageError
from .structured import SampleLimit, astyx_radar_metadata, csv_metadata, json_metadata

SCHEMA_ID = "urn:scenario-metadata:local:v1"
IMPLEMENTATION = "paper-inspired-local"
MAX_PCAP_PACKETS = 4096
MAX_PCAP_BYTES = 16 * 1024 * 1024
MAX_PACKET_BYTES = 1024 * 1024
PRODUCT_IDS = {0x21: "HDL-32E", 0x22: "VLP-16 / Puck LITE", 0x24: "Puck Hi-Res", 0x28: "VLP-32C"}
RETURN_MODES = {0x37: "Strongest", 0x38: "Last", 0x39: "Dual"}


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _creation_time(stat: os.stat_result) -> str | None:
    # POSIX st_ctime is inode change time, not birth/capture time.
    birth = getattr(stat, "st_birthtime", None)
    if birth is None and os.name == "nt":
        birth = stat.st_ctime
    return _utc(birth) if birth is not None else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _image_metadata(path: Path) -> dict[str, Any]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as im:
            if im.format not in {"PNG", "JPEG"}:
                raise ValueError(f"File extension indicates PNG/JPEG but content is {im.format}")
            expected = "PNG" if path.suffix.lower() == ".png" else "JPEG"
            if im.format != expected:
                raise ValueError(f"Extension indicates {expected}, content is {im.format}")
            # Decode first frame to detect truncation and to read PNG EXIF chunks.
            im.load()
            exif = im.getexif()
            nested = exif.get_ifd(34665) if 34665 in exif else {}
            exposure = nested.get(33434, exif.get(33434))
            exposure = float(exposure) if exposure is not None else None
            if exposure is not None and (not math.isfinite(exposure) or exposure < 0):
                exposure = None
            version = nested.get(36864, exif.get(36864))
            if isinstance(version, bytes):
                version = version.decode("ascii", errors="replace")
            result: dict[str, Any] = {
                "format": im.format,
                "width": im.width,
                "height": im.height,
                "mode": im.mode,
                "bands": list(im.getbands()),
                "number_of_frames": getattr(im, "n_frames", 1),
                "exposure_time_seconds": exposure,
                "exif_version": str(version) if version is not None else None,
                "unavailable_fields": {},
                "validation": "first_frame_decoded",
            }
            if exposure is None:
                result["unavailable_fields"]["exposure_time_seconds"] = "No usable EXIF ExposureTime tag; not inferred from the image."
            if version is None:
                result["unavailable_fields"]["exif_version"] = "No EXIF version tag. PNG has no general image-version field."
            if im.format == "PNG":
                with path.open("rb") as stream:
                    header = stream.read(33)
                if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
                    raise ValueError("Missing PNG IHDR")
                result["bit_depth"] = header[24]
                result["bit_depth_semantics"] = "IHDR bits per encoded sample (palette index for indexed PNG)."
                result["png_color_type"] = header[25]
                result["encoded_channels"] = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[header[25]]
                result["number_of_frames_semantics"] = "Pillow n_frames includes a separate default image when present in APNG."
            else:
                # Pillow reads JPEG SOF sample precision and component count.
                result["bit_depth"] = getattr(im, "bits", None)
                result["bit_depth_semantics"] = "JPEG SOF sample precision, in bits per component."
                result["number_of_layers"] = getattr(im, "layers", None)
                result["number_of_layers_semantics"] = "JPEG SOF component count; not editable image layers."
                result["jfif_version"] = list(im.info["jfif_version"]) if "jfif_version" in im.info else None
            return result


def _udp_packet(frame: bytes, linktype: int) -> tuple[dict[str, Any], bytes] | None:
    """Read Ethernet (including VLAN), raw IPv4 or Linux cooked IPv4 UDP."""
    if linktype == 1:
        if len(frame) < 14:
            return None
        offset, ethertype = 14, int.from_bytes(frame[12:14], "big")
        while ethertype in {0x8100, 0x88A8}:
            if len(frame) < offset + 4:
                return None
            ethertype = int.from_bytes(frame[offset + 2:offset + 4], "big")
            offset += 4
        if ethertype != 0x0800:
            return None
    elif linktype == 113:
        if len(frame) < 16 or frame[14:16] != b"\x08\x00":
            return None
        offset = 16
    elif linktype in {101, 228}:
        offset = 0
    else:
        return None
    packet = frame[offset:]
    if len(packet) < 20 or packet[0] >> 4 != 4 or packet[9] != 17:
        return None
    header_len = (packet[0] & 15) * 4
    total_len = int.from_bytes(packet[2:4], "big")
    # Do not reinterpret fragments as complete UDP datagrams.
    if header_len < 20 or total_len > len(packet) or total_len < header_len + 8 or int.from_bytes(packet[6:8], "big") & 0x3FFF:
        return None
    udp = packet[header_len:total_len]
    source_port, destination_port, udp_length = struct.unpack("!HHH", udp[:6])
    if udp_length < 8 or udp_length > len(udp):
        return None
    return {
        "source_address": str(ipaddress.ip_address(packet[12:16])),
        "destination_address": str(ipaddress.ip_address(packet[16:20])),
        "source_port": source_port,
        "destination_port": destination_port,
        "udp_length": udp_length,
    }, udp[8:udp_length]


def _pcap_metadata(path: Path) -> dict[str, Any]:
    """Bounded sample, not a full scan validator or general LiDAR decoder.

    Only classic PCAP and known Velodyne 1206-byte payload layouts are read.
    RPM is measured from consecutive first-block azimuths and capture times,
    with intervals >10ms discarded to avoid ambiguity from unseen rotations.
    No configurable sensor RPM or physical angular resolution is invented.
    """
    result: dict[str, Any] = {
        "parser": "classic-pcap-velodyne-sample-v1",
        "max_packets": MAX_PCAP_PACKETS,
        "max_sample_bytes": MAX_PCAP_BYTES,
        "packets_scanned": 0,
        "velodyne_packets_sampled": 0,
        "sample_limited": False,
        "rpm_estimate": None,
        "azimuth_resolution_degrees": None,
        "unavailable_fields": {
            "azimuth_resolution_degrees": "Not a stored packet field; encoded azimuth quantization is reported separately.",
        },
        "warnings": [],
    }
    flows: dict[tuple, dict[str, Any]] = {}
    previous: dict[tuple, tuple[float, float]] = {}
    rates: list[float] = []
    with path.open("rb") as stream:
        header = stream.read(24)
        if len(header) < 24:
            raise ValueError("Truncated classic PCAP global header")
        formats = {b"\xd4\xc3\xb2\xa1": ("<", 1e6), b"\xa1\xb2\xc3\xd4": (">", 1e6), b"\x4d\x3c\xb2\xa1": ("<", 1e9), b"\xa1\xb2\x3c\x4d": (">", 1e9)}
        if header[:4] not in formats:
            raise ValueError("Only classic PCAP supported; PCAPNG or unknown magic found")
        endian, precision = formats[header[:4]]
        major, minor, _, _, snaplen, network = struct.unpack(endian + "HHiiII", header[4:])
        linktype = network & 0xFFFF
        if (major, minor) != (2, 4):
            raise ValueError(f"Unsupported PCAP version {major}.{minor}")
        if linktype not in {1, 101, 113, 228}:
            raise ValueError(f"Unsupported PCAP link type {linktype}")
        result.update({"capture_format": "classic-pcap", "link_type": linktype, "timestamp_resolution_seconds": 1 / precision})
        while result["packets_scanned"] < MAX_PCAP_PACKETS and stream.tell() < MAX_PCAP_BYTES:
            record = stream.read(16)
            if not record:
                break
            if len(record) != 16:
                raise ValueError("Truncated PCAP record header")
            sec, fraction, captured, original = struct.unpack(endian + "IIII", record)
            if captured > MAX_PACKET_BYTES or captured > snaplen or captured > original or fraction >= precision:
                raise ValueError("Invalid or excessive PCAP packet length/timestamp")
            if stream.tell() + captured > MAX_PCAP_BYTES:
                result["sample_limited"] = True
                break
            frame = stream.read(captured)
            if len(frame) != captured:
                raise ValueError("Truncated PCAP packet")
            result["packets_scanned"] += 1
            parsed = _udp_packet(frame, linktype)
            if parsed is None:
                continue
            network_info, payload = parsed
            if len(payload) != 1206 or payload[1205] not in PRODUCT_IDS:
                continue
            azimuths = [int.from_bytes(payload[i + 2:i + 4], "little") for i in range(0, 1200, 100)]
            if any(payload[i:i + 2] != b"\xff\xee" for i in range(0, 1200, 100)) or any(a >= 36000 for a in azimuths):
                continue
            product, return_mode = payload[1205], payload[1204]
            if return_mode not in RETURN_MODES:
                continue
            timestamp = sec + fraction / precision
            key = tuple(network_info.values()) + (product, return_mode)
            if key not in flows:
                flows[key] = {**network_info, "product_id": product, "product_name": PRODUCT_IDS[product], "return_mode_code": return_mode, "return_mode": RETURN_MODES[return_mode], "packets_sampled": 0}
            flows[key]["packets_sampled"] += 1
            result["velodyne_packets_sampled"] += 1
            if key in previous:
                earlier_time, earlier_azimuth = previous[key]
                dt = timestamp - earlier_time
                advance = (azimuths[0] / 100 - earlier_azimuth) % 360
                if 0 < dt <= 0.01 and 0 < advance < 180:
                    rates.append(advance / dt / 6)
            previous[key] = (timestamp, azimuths[0] / 100)
        if stream.tell() < path.stat().st_size:
            result["sample_limited"] = True
    result["flows"] = list(flows.values())
    result["status"] = "ok" if flows else "no_supported_velodyne_packets"
    if flows:
        result["azimuth_encoding_quantization_degrees"] = 0.01
    if len(flows) == 1 and len(rates) >= 10:
        median_rpm = statistics.median(rates)
        # Sensor spec is 300-1200 RPM; tolerate packet timestamp jitter, but
        # do not emit a nonsensical value or blend multiple sensor streams.
        if 200 <= median_rpm <= 1500:
            result["rpm_estimate"] = round(median_rpm, 3)
            result["rpm_estimate_method"] = "median consecutive first-block azimuth change / PCAP time interval; observed speed, not configured RPM"
            result["rpm_estimate_intervals"] = len(rates)
    if result["rpm_estimate"] is None:
        result["unavailable_fields"]["rpm_estimate"] = "Requires one stream and >=10 short positive timestamp/azimuth intervals yielding a plausible rotation speed."
    if not flows:
        result["warnings"].append("No supported complete Velodyne UDP data packets were found in the bounded sample.")
    if result["sample_limited"]:
        result["warnings"].append("Metadata describes a bounded prefix; later packets were not inspected.")
    return result


def annotate_file(path: Path, provenance: dict | None = None, *, csv_delimiter: str = ",", csv_header: bool = True) -> dict:
    """Annotate one file, preserving generic metadata on sensor parse failure.

    Missing files/directories raise errors. Unsupported sensor formats retain
    generic metadata and an explicit reason. All timestamps are filesystem
    timestamps; they must never be treated as the recording's capture time.
    ``provenance`` is caller-supplied context, not extracted sensor evidence.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Expected a readable file: {path}")
    stat = path.stat()
    result: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "schema_version": "1.1",
        "implementation": IMPLEMENTATION,
        "reference_paper_doi": "10.1016/j.measen.2024.101458",
        "annotation_status": "ok",
        "file": {
            "name": path.name,
            "folder_name": path.parent.name,
            "extension": path.suffix.lower(),
            "size_bytes": stat.st_size,
            "sha256": _sha256(path),
            "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "filesystem_created_at_utc": _creation_time(stat),
            "filesystem_modified_at_utc": _utc(stat.st_mtime),
            "folder_filesystem_created_at_utc": _creation_time(path.parent.stat()),
            "timestamp_semantics": "Local filesystem timestamps; downloads/copies can reset them. These are not sensor capture times.",
        },
        "provenance": copy.deepcopy(provenance) if provenance is not None else {},
        "warnings": [],
    }
    if result["file"]["filesystem_created_at_utc"] is None:
        result["warnings"].append("Filesystem birth time is unavailable; inode change time was not substituted.")
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg"}:
        try:
            result["image"] = _image_metadata(path)
        except (OSError, ValueError, SyntaxError, EOFError, KeyError, struct.error, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            result["image"] = {"status": "error", "reason": str(exc)}
            result["annotation_status"] = "partial"
            result["warnings"].append(f"Image metadata could not be extracted: {exc}")
    elif suffix in {".pcap", ".pcapng"}:
        try:
            result["lidar"] = _pcap_metadata(path)
            if result["lidar"]["status"] != "ok":
                result["annotation_status"] = "partial"
            result["warnings"].extend(result["lidar"]["warnings"])
        except (OSError, ValueError, struct.error) as exc:
            result["lidar"] = {"status": "unsupported_or_invalid", "reason": str(exc)}
            result["annotation_status"] = "partial"
            result["warnings"].append(f"LiDAR metadata could not be extracted: {exc}")
    elif suffix == ".npz":
        try:
            from .a2d2_metadata import lidar_metadata
            result["lidar"] = lidar_metadata(path)
            result["sensor_metadata_status"] = "structured_extension"
            if result["lidar"]["status"] != "ok":
                result["annotation_status"] = "partial"
                result["warnings"].extend(result["lidar"]["validation_errors"])
            if result["lidar"]["sample_limited"]:
                result["warnings"].append("LiDAR statistics cover at most 100,000 points; point_count is the complete shape.")
        except (ImportError, OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile) as exc:
            result["annotation_status"] = "partial"
            result["lidar"] = {"status": "unsupported_or_invalid", "reason": str(exc)}
    elif suffix in {".h5", ".hdf5"}:
        try:
            from .radarscenes import hdf5_metadata
            result["radar"] = hdf5_metadata(path)
            result["sensor_metadata_status"] = "structured_extension"
            if result["radar"]["status"] != "ok":
                result["annotation_status"] = "partial"
            if any(item["sample_limited"] for item in result["radar"]["datasets"].values()):
                result["warnings"].append("HDF5 statistics cover at most 100,000 rows per dataset; total row counts come from dataset shapes.")
        except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
            result["annotation_status"] = "partial"
            result["radar"] = {"status": "unsupported_or_invalid", "reason": str(exc)}
            result["warnings"].append(f"RadarScenes HDF5 metadata could not be extracted: {exc}")
    elif suffix in {".csv", ".tsv", ".json", ".txt"}:
        result["extension_notice"] = "Structured-data parsing is a project extension beyond the NPL paper's generic file metadata."
        try:
            if suffix in {".csv", ".tsv"}:
                result["csv"] = csv_metadata(path, delimiter="\t" if suffix == ".tsv" else csv_delimiter, has_header=csv_header)
                if result["csv"]["status"] != "ok":
                    result["annotation_status"] = "partial"
                    result["warnings"].append("CSV contains records with an unexpected number of fields.")
                if result["csv"]["sample_limited"]:
                    result["warnings"].append("CSV summary covers only a bounded prefix; row_count is unknown.")
            elif suffix == ".json":
                result["json"], calibration = json_metadata(path)
                if calibration is not None:
                    result["calibration"] = calibration
                    if calibration["status"] != "ok":
                        result["annotation_status"] = "partial"
                        result["warnings"].extend(calibration["validation_errors"])
            else:
                # Require dataset layout as well as its distinctive header.
                radar = astyx_radar_metadata(path) if path.parent.name == "radar_6455" else None
                if radar is not None:
                    result["radar"] = radar
                    if radar["status"] != "ok":
                        result["annotation_status"] = "partial"
                        result["warnings"].append("Radar table has malformed or nonfinite detections.")
                    if radar["sample_limited"]:
                        result["warnings"].append("Radar summary covers a bounded prefix; total detection count is unknown.")
            result["sensor_metadata_status"] = "structured_extension" if any(k in result for k in ("csv", "calibration", "radar")) else "generic_only"
        except (ValueError, OSError, UnicodeError, csv.Error, SampleLimit, RecursionError, OverflowError) as exc:
            result["annotation_status"] = "partial"
            result["warnings"].append(f"Structured metadata could not be extracted: {exc or type(exc).__name__}")
    else:
        result["sensor_metadata_status"] = "generic_only"
        result["sensor_metadata_reason"] = "No specialized parser is configured for this format; only generic file properties are extracted."
    # Fail clearly for non-JSON provenance rather than silently stringify it.
    json.dumps(result, allow_nan=False)
    return result


def metadata_to_xml(metadata: dict) -> str:
    """Serialize our schema; no claim of conformance to the unpublished NPL XSD.

    Mapping keys are attributes on entries, not XML names, so arbitrary JSON
    provenance keys remain valid. Types preserve null, list and scalar meaning.
    """
    ET.register_namespace("sm", SCHEMA_ID)
    root = ET.Element(f"{{{SCHEMA_ID}}}metadata", {"schema_version": metadata.get("schema_version", "1.0"), "implementation": IMPLEMENTATION})

    def add(parent: ET.Element, key: str, value: Any) -> None:
        element = ET.SubElement(parent, f"{{{SCHEMA_ID}}}entry", {"key": key})
        if value is None:
            element.set("type", "null")
        elif isinstance(value, dict):
            element.set("type", "object")
            for name, item in value.items():
                add(element, str(name), item)
        elif isinstance(value, (list, tuple)):
            element.set("type", "array")
            for index, item in enumerate(value):
                add(element, str(index), item)
        elif isinstance(value, bool):
            element.set("type", "boolean")
            element.text = "true" if value else "false"
        else:
            element.set("type", "number" if isinstance(value, (int, float)) else "string")
            element.text = str(value)

    for key, value in metadata.items():
        add(root, str(key), value)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
