"""Independent format fixtures test extraction and rejection paths."""

import hashlib
import json
import struct
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from scenario_metadata.metadata import annotate_file, metadata_to_xml, SCHEMA_ID


def _pcap(path: Path, *, endian="<", nanoseconds=False, count=25, vlan=False, malformed=False):
    magic = (b"\x4d\x3c\xb2\xa1" if endian == "<" else b"\xa1\xb2\x3c\x4d") if nanoseconds else (b"\xd4\xc3\xb2\xa1" if endian == "<" else b"\xa1\xb2\xc3\xd4")
    data = bytearray(magic + struct.pack(endian + "HHIIII", 2, 4, 0, 0, 65535, 1))
    for n in range(count):
        payload = bytearray(1206)
        # 3.6 degrees / 1ms = 600 RPM. Start near 360 to test wraparound.
        for block in range(12):
            offset = block * 100
            payload[offset:offset + 2] = b"\xff\xee"
            payload[offset + 2:offset + 4] = struct.pack("<H", (35900 + n * 360 + block * 10) % 36000)
        payload[1204:1206] = bytes([0x37, 0x22])
        if malformed:
            payload[0] = 0
        udp = struct.pack("!HHHH", 2368, 2368, 1214, 0) + payload
        ip = bytearray(20)
        ip[0], ip[9] = 0x45, 17
        ip[2:4] = struct.pack("!H", len(ip) + len(udp))
        ip[12:16], ip[16:20] = bytes([192, 168, 1, 201]), bytes([192, 168, 1, 2])
        eth = b"\0" * 12 + (b"\x81\x00\x00\x01\x08\x00" if vlan else b"\x08\x00")
        packet = eth + ip + udp
        data += struct.pack(endian + "IIII", 100, n * (1000000 if nanoseconds else 1000), len(packet), len(packet)) + packet
    path.write_bytes(data)


def test_generic_hash_and_provenance(tmp_path):
    path = tmp_path / "tracks.csv"
    content = b"id,frame,x\n1,10,23\n"
    path.write_bytes(content)
    provenance = {"dataset": "example", "scenario": {"id": "lane-change-1"}}
    metadata = annotate_file(path, provenance)
    provenance["scenario"]["id"] = "changed"
    assert metadata["implementation"] == "paper-inspired-local"
    assert metadata["file"]["sha256"] == hashlib.sha256(content).hexdigest()
    assert metadata["file"]["size_bytes"] == len(content)
    assert metadata["sensor_metadata_status"] == "structured_extension"
    assert metadata["csv"]["row_count"] == 1
    assert metadata["provenance"]["scenario"]["id"] == "lane-change-1"
    assert "not sensor capture times" in metadata["file"]["timestamp_semantics"]
    assert "image" not in metadata and "lidar" not in metadata
    json.dumps(metadata, allow_nan=False)


@pytest.mark.parametrize("mode,depth", [("RGB", 8), ("1", 1), ("I;16", 16)])
def test_png_real_encoded_bit_depth(tmp_path, mode, depth):
    path = tmp_path / "image.PNG"
    Image.new(mode, (13, 7)).save(path)
    image = annotate_file(path)["image"]
    assert (image["width"], image["height"]) == (13, 7)
    assert image["bit_depth"] == depth
    assert image["format"] == "PNG"
    assert image["exposure_time_seconds"] is None
    assert image["exif_version"] is None
    assert "exposure_time_seconds" in image["unavailable_fields"]


def test_palette_png_reports_encoded_index_depth(tmp_path):
    path = tmp_path / "palette.png"
    Image.new("P", (11, 8)).save(path, bits=2)
    result = annotate_file(path)["image"]
    assert result["bit_depth"] == 2
    assert result["encoded_channels"] == 1


def test_jpeg_components_and_exif_exposure(tmp_path):
    path = tmp_path / "image.jpeg"
    exif = Image.Exif()
    exif[34665] = {33434: 0.002, 36864: b"0231"}
    Image.new("RGB", (17, 9)).save(path, exif=exif)
    image = annotate_file(path)["image"]
    assert image["bit_depth"] == 8
    assert image["number_of_layers"] == 3
    assert image["exposure_time_seconds"] == pytest.approx(0.002)
    assert image["exif_version"] == "0231"


def test_apng_frame_count(tmp_path):
    path = tmp_path / "animated.png"
    Image.new("RGB", (6, 6), "red").save(path, save_all=True, append_images=[Image.new("RGB", (6, 6), "blue")])
    assert annotate_file(path)["image"]["number_of_frames"] == 2


def test_invalid_image_retains_generic_metadata(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not an image")
    result = annotate_file(path)
    assert result["annotation_status"] == "partial"
    assert result["image"]["status"] == "error"
    assert result["file"]["size_bytes"] == 12


def test_mislabeled_image_is_not_silently_accepted(tmp_path):
    path = tmp_path / "wrong.jpg"
    Image.new("RGB", (3, 3)).save(path, format="PNG")
    assert annotate_file(path)["annotation_status"] == "partial"


@pytest.mark.parametrize("endian,nanoseconds,vlan", [("<", False, False), (">", False, True), ("<", True, True), (">", True, False)])
def test_velodyne_network_and_rotation(tmp_path, endian, nanoseconds, vlan):
    path = tmp_path / "scan.pcap"
    _pcap(path, endian=endian, nanoseconds=nanoseconds, vlan=vlan)
    result = annotate_file(path)
    assert result["annotation_status"] == "ok"
    lidar = result["lidar"]
    assert lidar["velodyne_packets_sampled"] == 25
    assert lidar["rpm_estimate"] == pytest.approx(600, abs=0.01)
    flow = lidar["flows"][0]
    assert flow["source_address"] == "192.168.1.201"
    assert flow["destination_port"] == 2368
    assert flow["udp_length"] == 1214
    assert flow["return_mode"] == "Strongest"
    assert flow["product_id"] == 0x22
    assert lidar["azimuth_resolution_degrees"] is None
    assert lidar["azimuth_encoding_quantization_degrees"] == 0.01


def test_one_packet_does_not_invent_rpm(tmp_path):
    path = tmp_path / "single.pcap"
    _pcap(path, count=1)
    lidar = annotate_file(path)["lidar"]
    assert lidar["rpm_estimate"] is None
    assert "rpm_estimate" in lidar["unavailable_fields"]


def test_malformed_payload_does_not_claim_sensor_metadata(tmp_path):
    path = tmp_path / "bad.pcap"
    _pcap(path, malformed=True)
    result = annotate_file(path)
    assert result["annotation_status"] == "partial"
    assert result["lidar"]["velodyne_packets_sampled"] == 0


def test_truncated_pcap_is_reported(tmp_path):
    path = tmp_path / "truncated.pcap"
    _pcap(path, count=1)
    path.write_bytes(path.read_bytes()[:-10])
    result = annotate_file(path)
    assert result["annotation_status"] == "partial"
    assert "Truncated PCAP packet" in result["lidar"]["reason"]


def test_pcapng_is_not_parsed_as_classic(tmp_path):
    path = tmp_path / "new.pcapng"
    path.write_bytes(b"\x0a\x0d\x0d\x0a" + b"\0" * 30)
    assert "Only classic PCAP" in annotate_file(path)["lidar"]["reason"]


def test_xml_is_well_formed_with_arbitrary_provenance_keys(tmp_path):
    path = tmp_path / "source.txt"
    path.write_text("example", encoding="utf-8")
    data = annotate_file(path, {"a b & c": "<text> & \"quoted\"", "items": [None, True, 4]})
    root = ET.fromstring(metadata_to_xml(data))
    assert root.tag == f"{{{SCHEMA_ID}}}metadata"
    assert root.attrib["implementation"] == "paper-inspired-local"
    entries = list(root.iter(f"{{{SCHEMA_ID}}}entry"))
    assert next(e for e in entries if e.attrib["key"] == "a b & c").text == "<text> & \"quoted\""
    assert any(e.attrib.get("type") == "null" for e in entries)


def test_missing_file_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        annotate_file(tmp_path / "missing.png")
