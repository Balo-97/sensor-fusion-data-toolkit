"""Benchmark correctness of metadata extraction on identified public samples."""
from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from .downloads import download_file, load_manifest, safe_child, selected_datasets, sha256_file
from .pipeline import save_annotation, write_json


def check_expected(metadata: dict, expected: dict, source: Path) -> list[dict]:
    """Compare against manifest facts, including CSV row counts separately from metadata."""
    checks = []
    for key, wanted in expected.items():
        if key in {"rows", "columns", "headers"}:
            with source.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream)
                headers = next(reader, [])
                actual = {"headers": headers, "columns": len(headers)}.get(key)
                if key == "rows":
                    actual = sum(1 for _ in reader)
        elif key in {"width", "height", "format", "mode", "bit_depth"}:
            actual = metadata.get("image", {}).get(key)
        else:
            actual = metadata
            for part in key.split("."):
                actual = actual.get(part) if isinstance(actual, dict) else None
        checks.append({"field": key, "expected": wanted, "actual": actual, "passed": actual == wanted})
    return checks


def run_benchmark(data_dir: Path, output: Path, dataset_ids: list[str] | None = None,
                  manifest_path: Path | None = None, max_bytes: int = 100_000_000) -> dict:
    manifest = load_manifest(manifest_path)
    datasets = selected_datasets(manifest, dataset_ids)
    if not datasets:
        raise ValueError("No datasets selected")
    start = time.perf_counter()
    results = []
    for dataset in datasets:
        result = {"id": dataset["id"], "title": dataset["title"], "source_url": dataset["source_url"],
                  "license": dataset["license"], "scope": dataset["scope"], "files": []}
        for item in dataset["files"]:
            entry = {"path": item["path"], "url": item["url"]}
            file_start = time.perf_counter()
            try:
                source = safe_child(safe_child(data_dir, dataset["id"]), item["path"])
                entry["download"] = download_file(item["url"], source, item["sha256"], max_bytes,
                                                   item.get("size_bytes"))
                annotation_start = time.perf_counter()
                annotation = save_annotation(
                    source, safe_child(safe_child(output / "metadata", dataset["id"]), item["path"]),
                    provenance={"dataset_id": dataset["id"], "source_url": item["url"],
                                "dataset_page": dataset["source_url"], "license": dataset["license"],
                                "related_files": item.get("related_files", {}),
                                "scope": dataset["scope"], "download_sha256": item["sha256"]},
                )
                entry["annotation_seconds"] = round(time.perf_counter() - annotation_start, 6)
                metadata = annotation["metadata"]
                checks = check_expected(metadata, item.get("expected", {}), source)
                checks.extend([
                    {"field": "file.size_bytes", "expected": source.stat().st_size,
                     "actual": metadata["file"]["size_bytes"],
                     "passed": metadata["file"]["size_bytes"] == source.stat().st_size},
                    {"field": "file.sha256", "expected": item["sha256"],
                     "actual": metadata["file"]["sha256"],
                     "passed": metadata["file"]["sha256"] == item["sha256"]},
                    {"field": "annotation_status", "expected": "ok",
                     "actual": metadata["annotation_status"], "passed": metadata["annotation_status"] == "ok"},
                ])
                checks.append({"field": "download_sha256", "expected": item["sha256"],
                               "actual": sha256_file(source), "passed": sha256_file(source) == item["sha256"]})
                ET.parse(annotation["xml"])
                parsed_json = json.loads(Path(annotation["json"]).read_text(encoding="utf-8"))
                checks.append({"field": "json_roundtrip", "passed": parsed_json == metadata})
                checks.append({"field": "xml_well_formed", "passed": True})
                entry.update({"checks": checks, "json": annotation["json"], "xml": annotation["xml"],
                              "warnings": metadata.get("warnings", []),
                              "status": "passed" if all(c["passed"] for c in checks) else "failed"})
            except Exception as exc:
                entry.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            entry["elapsed_seconds"] = round(time.perf_counter() - file_start, 6)
            result["files"].append(entry)
        result["status"] = "passed" if result["files"] and all(f["status"] == "passed" for f in result["files"]) else "failed"
        results.append(result)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(), "backend": "paper-inspired-local",
        "original_npl_tool_tested": False,
        "scope": "Small public samples; metadata correctness and serialization, not full-dataset coverage or scenario-mining accuracy.",
        "limitations": ["No NPL source/API or HCP deployment was available.",
                        "These sensor samples are not highD trajectory inputs to Chat2Scenario.",
                        "CSV summaries, recognized calibration JSON and Astyx radar parsing are local extensions, not NPL features.",
                        "XML well-formedness is checked; conformance to an unavailable NPL XSD is not claimed."],
        "status": "passed" if all(d["status"] == "passed" for d in results) else "failed",
        "datasets_passed": sum(d["status"] == "passed" for d in results), "datasets_total": len(results),
        "files_passed": sum(f["status"] == "passed" for d in results for f in d["files"]),
        "files_total": sum(len(d["files"]) for d in results),
        "elapsed_seconds": round(time.perf_counter() - start, 3), "datasets": results,
    }
    write_json(output / "benchmark_report.json", report)
    lines = ["# Public dataset metadata benchmark", "", f"Result: **{report['status'].upper()}**", "",
             "Backend: **paper-inspired-local**. The original NPL application and HCP were not tested.", "",
             report["scope"], "", "| Dataset | Files passed | Result |", "|---|---:|---|"]
    for d in results:
        passed = sum(f["status"] == "passed" for f in d["files"])
        lines.append(f"| {d['title']} | {passed}/{len(d['files'])} | {d['status']} |")
    lines += ["", "Details, checks, source URLs, timings and errors are in `benchmark_report.json`.", "",
              "## Limitations", ""] + [f"- {item}" for item in report["limitations"]]
    (output / "benchmark_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
