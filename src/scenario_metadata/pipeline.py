"""Persist annotations and carry mined scenario provenance into metadata sidecars."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .metadata import annotate_file, metadata_to_xml


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # allow_nan=False ensures JSON is portable (no NaN/Infinity extension).
    content = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def save_annotation(source: Path, destination_stem: Path, provenance: dict | None = None,
                    scenario: dict | None = None, *, csv_delimiter: str = ",", csv_header: bool = True) -> dict:
    metadata = annotate_file(source, provenance=provenance, csv_delimiter=csv_delimiter, csv_header=csv_header)
    if scenario is not None:
        metadata["scenario"] = scenario
        metadata["scenario_metadata_notice"] = (
            "Project extension from Chat2Scenario's mining result; not inferred by the NPL paper's file annotator."
        )
    destination_stem.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(str(destination_stem) + ".metadata.json")
    xml_path = Path(str(destination_stem) + ".metadata.xml")
    write_json(json_path, metadata)
    xml_path.write_text(metadata_to_xml(metadata), encoding="utf-8")
    return {"metadata": metadata, "json": str(json_path.resolve()), "xml": str(xml_path.resolve())}


def annotate_mining_result(manifest: dict, output: Path) -> dict:
    annotations = []
    scenarios = {str(item["scenario_id"]): item for item in manifest.get("scenarios", [])}
    for index, artifact in enumerate(manifest.get("artifacts", [])):
        source = Path(artifact["path"])
        scenario_id = str(artifact.get("scenario_id", ""))
        scenario = scenarios.get(scenario_id)
        if scenario is None:
            raise ValueError(f"Artifact has no matching scenario: {scenario_id}")
        result = save_annotation(
            source, output / "metadata" / f"{index:04d}_{source.name}",
            provenance={"source": "Chat2Scenario", "upstream_commit": manifest.get("upstream_commit"),
                        "input_tracks": manifest.get("input", {}).get("path"),
                        "input_sha256": manifest.get("input", {}).get("sha256"),
                        "synthetic_input": manifest.get("input", {}).get("synthetic"),
                        "bypassed_llm": manifest.get("bypassed_llm"),
                        "classification": manifest.get("classification"),
                        "role": artifact.get("role"), "scenario_id": scenario_id},
            scenario=scenario,
        )
        annotations.append({"artifact": str(source.resolve()), "json": result["json"], "xml": result["xml"]})
    report = {"created_at": datetime.now(timezone.utc).isoformat(),
              "metadata_backend": "paper-inspired-local", "original_npl_tool_tested": False,
              "mining": manifest, "annotations": annotations,
              "status": "completed" if annotations else "no_matches"}
    write_json(output / "pipeline_report.json", report)
    return report
