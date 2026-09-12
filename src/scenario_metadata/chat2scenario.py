"""A pinned, isolated bridge to the authors' Chat2Scenario implementation.

Structured classification bypasses only the paid LLM step. Activity recognition,
scenario identification and the initial OpenSCENARIO export are upstream code.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

UPSTREAM_URL = "https://github.com/ftgTUGraz/Chat2Scenario"
UPSTREAM_COMMIT = "a396c0d86fcf551a3901f299e0654754ba87918f"
# SHA-256 of official source after CRLF -> LF normalization (Git on Windows).
SOURCE_HASHES = {
    "scenario_mining/activity_identification.py": "e8966d8a5b4f16d0c3daad926f0320ce317eed49c1b285b8151f3bce03472389",
    "scenario_mining/scenario_identification.py": "fd76b40e354d4cdccfa227c1d7fd765e3cfe692376c2039f3a6f2ce3ba7906dd",
    "utils/helper_original_scenario.py": "bd04a9dcca7bb460fe3ba1217503725b24334fc8cf96c7cffcac06fbd32ae6f4",
    "Metric/Acceleration_Scale.py": "fb0dc800caf054d0fadda65fdb4be697bf5865f9f0af8aa2db572208e2a187c1",
    "Metric/Distance_Scale.py": "eb68c7c3425ececf7c7fb08fb4be63d32332f6c3bcb3a593185af72a29241a57",
    "Metric/Jerk_Scale.py": "35cc399d7334ff404b16ef550da0c5a2e0ce04eb5d41716e78236164a3122862",
    "Metric/Time_Scale.py": "9d55b0280d3bc5f57ef6fd881a07ed6ca3fe0d989d5f08eff2bafe97f60f2f14",
    "NLP/Scenario_Description_Understand.py": "582c1ebcc93814944f6c80858173fe106323a4c9ff2e4602a03bb8c09166e3a8",
}
INTERACTION_COLUMNS = (
    "precedingId", "followingId", "leftPrecedingId", "leftAlongsideId",
    "leftFollowingId", "rightPrecedingId", "rightAlongsideId", "rightFollowingId",
)
REQUIRED_COLUMNS = (
    "frame", "id", "x", "y", "width", "height", "xVelocity", "yVelocity",
    "xAcceleration", "yAcceleration", "laneId", *INTERACTION_COLUMNS,
)
FOLLOWING_SCENARIO = {
    "Ego Vehicle": {
        "Ego longitudinal activity": ["keep velocity"],
        "Ego lateral activity": ["follow lane"],
    },
    "Target Vehicle #1": {
        "Target start position": {"same lane": ["front"]},
        "Target end position": {"same lane": ["front"]},
        "Target behavior": {
            "target longitudinal activity": ["keep velocity"],
            "target lateral activity": ["follow lane"],
        },
    },
}


def create_demo_tracks(output: Path, frames: int = 101) -> Path:
    """Generate synthetic following trajectories; never counted as a public dataset."""
    import csv
    if frames < 3:
        raise ValueError("At least three frames are required")
    columns = list(REQUIRED_COLUMNS) + ["frontSightDistance", "backSightDistance", "dhw", "thw", "ttc", "precedingXVelocity"]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for actor in (1, 2):
            for frame in range(1, frames + 1):
                row = dict.fromkeys(columns, 0)
                row.update(frame=frame, id=actor, x=10 * (frame - 1) / 25 + (actor - 1) * 25,
                           y=8, width=4.5, height=1.8, xVelocity=10, laneId=3,
                           frontSightDistance=100, backSightDistance=100)
                if actor == 1:
                    row.update(precedingId=2, precedingXVelocity=10, dhw=20.5, thw=2.05)
                else:
                    row.update(followingId=1)
                writer.writerow(row)
    return output


def _source_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def verify_chat2scenario(repo: Path) -> dict[str, str]:
    """Fail closed if any code imported by this bridge differs from the pin."""
    repo = Path(repo)
    for relative, expected in SOURCE_HASHES.items():
        path = repo / relative
        if not path.is_file() or _source_digest(path) != expected:
            raise ValueError(f"Chat2Scenario source missing or changed: {relative}; run bootstrap_chat2scenario into a clean directory")
    return dict(SOURCE_HASHES)


def bootstrap_chat2scenario(destination: Path) -> Path:
    """Download the minimal official source tree, checking each pinned hash."""
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for relative, expected in SOURCE_HASHES.items():
        path = destination / relative
        if path.exists():
            if _source_digest(path) != expected:
                raise ValueError(f"Refusing to replace modified upstream file: {path}")
            continue
        url = f"https://raw.githubusercontent.com/ftgTUGraz/Chat2Scenario/{UPSTREAM_COMMIT}/{relative}"
        request = urllib.request.Request(url, headers={"User-Agent": "scenario-metadata-research/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(2_000_001).replace(b"\r\n", b"\n")
        if len(data) > 2_000_000 or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Upstream download failed integrity check: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
    verify_chat2scenario(destination)
    (destination / "UPSTREAM_PROVENANCE.json").write_text(json.dumps({
        "repository": UPSTREAM_URL, "commit": UPSTREAM_COMMIT,
        "sha256_normalized_lf": SOURCE_HASHES,
    }, indent=2), encoding="utf-8")
    return destination


def validate_structured_scenario(scenario: dict) -> dict:
    """Restrict input to what this pinned single-target miner actually reads."""
    if not isinstance(scenario, dict) or set(scenario) != {"Ego Vehicle", "Target Vehicle #1"}:
        raise ValueError("A scenario must contain exactly Ego Vehicle and Target Vehicle #1; the pinned miner ignores additional target descriptions")
    longitudinal = {"keep velocity", "acceleration", "deceleration"}
    lateral = {"follow lane", "lane change left", "lane change right"}

    def activity(container: dict, key: str, allowed: set[str]) -> None:
        value = container.get(key)
        if not isinstance(value, list) or len(value) != 1 or value[0] not in allowed:
            raise ValueError(f"{key} must contain exactly one of {sorted(allowed)}")

    ego = scenario["Ego Vehicle"]
    target = scenario["Target Vehicle #1"]
    if not isinstance(ego, dict) or not isinstance(target, dict):
        raise ValueError("Vehicle descriptions must be objects")
    activity(ego, "Ego longitudinal activity", longitudinal)
    activity(ego, "Ego lateral activity", lateral)
    behavior = target.get("Target behavior")
    if not isinstance(behavior, dict):
        raise ValueError("Target behavior must be an object")
    activity(behavior, "target longitudinal activity", longitudinal)
    activity(behavior, "target lateral activity", lateral)
    positions = {"same lane": {"front", "behind"},
                 "adjacent lane": {"left adjacent lane", "right adjacent lane"}}
    for key in ("Target start position", "Target end position"):
        position = target.get(key)
        if not isinstance(position, dict) or len(position) != 1:
            raise ValueError(f"{key} must specify exactly one lane relationship")
        lane = next(iter(position))
        if lane not in positions:
            raise ValueError(f"{key}: pinned miner supports same lane or adjacent lane")
        activity(position, lane, positions[lane])
    return copy.deepcopy(scenario)


def run_chat2scenario(
    repo: Path, tracks: Path, output: Path,
    scenario_description: str | None = None,
    structured_scenario: dict | None = None,
    *, model: str | None = None, frame_rate: float = 25.0,
    max_scenarios: int = 20, python_executable: str | Path | None = None,
    road_file: Path | None = None, timeout: float = 600,
    synthetic: bool = False,
) -> dict:
    """Mine highD-format tracks with the real upstream core in a subprocess.

    Output must be empty. Natural-language mode needs an explicit model and an
    OPENAI_API_KEY environment variable; secrets never enter argv or JSON files.
    The adapter supports highD straight-road geometry and one target description.
    ``synthetic`` is provenance only and never alters the mining algorithm.
    """
    if bool(scenario_description) == (structured_scenario is not None):
        raise ValueError("Provide exactly one of scenario_description or structured_scenario")
    if structured_scenario is not None:
        structured_scenario = validate_structured_scenario(structured_scenario)
    elif not model or not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("Natural-language mining requires an explicit model and OPENAI_API_KEY in the environment")
    if frame_rate != 25:
        raise ValueError("This bridge validates highD at 25 Hz only; adapt and validate other datasets before mining")
    if isinstance(max_scenarios, bool) or not isinstance(max_scenarios, int) or max_scenarios < 1:
        raise ValueError("max_scenarios must be a positive integer")
    repo, tracks, output = Path(repo).resolve(), Path(tracks).resolve(), Path(output).resolve()
    verify_chat2scenario(repo)
    if not tracks.is_file():
        raise FileNotFoundError(tracks)
    if road_file is not None and not Path(road_file).is_file():
        raise FileNotFoundError(road_file)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Output directory must be empty to avoid stale artifacts: {output}")
    request = {
        "repo": str(repo), "tracks": str(tracks), "output": str(output),
        "scenario_description": scenario_description, "structured_scenario": structured_scenario,
        "model": model, "frame_rate": frame_rate, "max_scenarios": max_scenarios,
        "road_file": str(Path(road_file).resolve()) if road_file else None,
        "synthetic": bool(synthetic),
    }
    env = os.environ.copy()
    # Make the source checkout usable as well as an installed package.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    env["MPLBACKEND"] = "Agg"
    with tempfile.TemporaryDirectory(prefix="chat2scenario-request-") as temp:
        request_path = Path(temp) / "request.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        result = subprocess.run(
            [str(python_executable or sys.executable), "-m", "scenario_metadata.chat2scenario", "--worker", str(request_path)],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    key = env.get("OPENAI_API_KEY", "")
    log = result.stdout + result.stderr
    if key:
        log = log.replace(key, "[REDACTED]")
    if result.returncode:
        raise RuntimeError(f"Chat2Scenario worker failed (exit {result.returncode}):\n{log[-6000:]}")
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("Chat2Scenario worker produced no manifest")
    (output / "chat2scenario.log").write_text(log, encoding="utf-8")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _validate_tracks(tracks):
    import numpy as np
    import pandas as pd

    missing = sorted(set(REQUIRED_COLUMNS) - set(tracks.columns))
    if missing:
        raise ValueError(f"Missing highD columns: {', '.join(missing)}")
    if tracks.empty:
        raise ValueError("The track file is empty")
    for column in REQUIRED_COLUMNS:
        tracks[column] = pd.to_numeric(tracks[column], errors="raise")
        if not np.isfinite(tracks[column].to_numpy()).all():
            raise ValueError(f"Track column contains missing or nonfinite values: {column}")
    for column in ("frame", "id", "laneId", *INTERACTION_COLUMNS):
        if not (tracks[column] == np.floor(tracks[column])).all():
            raise ValueError(f"Track column must contain integers: {column}")
        tracks[column] = tracks[column].astype("int64")
    if (tracks["id"] <= 0).any() or (tracks["frame"] < 0).any() or (tracks[["width", "height"]] <= 0).any().any():
        raise ValueError("Track IDs and dimensions must be positive, and frame numbers nonnegative")
    if tracks.duplicated(["id", "frame"]).any():
        raise ValueError("Duplicate vehicle/frame rows")
    known_ids = set(tracks["id"])
    for column in INTERACTION_COLUMNS:
        absent = set(tracks[column]) - known_ids - {0}
        if absent:
            raise ValueError(f"{column} refers to absent vehicle IDs {sorted(absent)[:10]}; provide the full recording or a closed subset")
    tracks = tracks.sort_values(["id", "frame"]).reset_index(drop=True)
    fixed_lane_ids = set()
    changed_lane_ids = set()
    for actor, group in tracks.groupby("id"):
        if len(group) < 3 or not (group["frame"].diff().dropna() == 1).all():
            raise ValueError(f"Vehicle {actor} needs at least three contiguous frames")
        lanes = set(group["laneId"])
        if len(lanes) == 1:
            fixed_lane_ids |= lanes
        else:
            if (group["laneId"].diff().fillna(0) != 0).sum() > 1:
                raise ValueError(f"Vehicle {actor} has multiple lane changes; this upstream implementation handles only the first")
            changed_lane_ids |= lanes
    if changed_lane_ids - fixed_lane_ids:
        raise ValueError("Each lane involved in a lane change needs a lane-following reference vehicle for upstream lateral activity estimation")
    return tracks


def _portable_xosc(xml: str, actor_tracks: list, road_file: str | None, output: Path) -> str:
    """Resolve upstream catalog/road placeholders and preserve measured geometry."""
    root = ET.fromstring(xml)
    root.find("CatalogLocations").clear()
    road = root.find("RoadNetwork")
    road.clear()
    if road_file:
        road_target = output / "road.xodr"
        if not road_target.exists():
            shutil.copyfile(road_file, road_target)
        ET.SubElement(road, "LogicFile", {"filepath": "road.xodr"})
    for entity, actor in zip(root.findall("./Entities/ScenarioObject"), actor_tracks):
        for child in list(entity):
            entity.remove(child)
        # highD width is length along the road; height is vehicle lateral width.
        length, width = float(actor["width"].median()), float(actor["height"].median())
        vehicle = ET.SubElement(entity, "Vehicle", {"name": "tracked_vehicle", "vehicleCategory": "car"})
        ET.SubElement(vehicle, "ParameterDeclarations")
        box = ET.SubElement(vehicle, "BoundingBox")
        ET.SubElement(box, "Center", {"x": "0", "y": "0", "z": "0.75"})
        ET.SubElement(box, "Dimensions", {"width": str(width), "length": str(length), "height": "1.5"})
        ET.SubElement(vehicle, "Performance", {"maxSpeed": "100", "maxAcceleration": "20", "maxDeceleration": "20"})
        axles = ET.SubElement(vehicle, "Axles")
        for tag, xpos in (("FrontAxle", length * .3), ("RearAxle", -length * .3)):
            ET.SubElement(axles, tag, {"maxSteering": "0.5", "wheelDiameter": "0.6", "trackWidth": str(width * .8), "positionX": str(xpos), "positionZ": "0.3"})
        ET.SubElement(vehicle, "Properties")
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _worker_run(request: dict) -> dict:
    import pandas as pd

    repo, source, output = (Path(request[key]) for key in ("repo", "tracks", "output"))
    verify_chat2scenario(repo)
    # Upstream calls DataFrame.append(dict) twice. A worker-local compatibility
    # shim preserves those operations on pandas 2.x without editing upstream.
    append_shim = not hasattr(pd.DataFrame, "append")
    if append_shim:
        def dataframe_append(frame, other, ignore_index=False, **kwargs):
            addition = pd.DataFrame([other]) if isinstance(other, dict) else other
            return pd.concat([frame, addition], ignore_index=ignore_index, **kwargs)
        pd.DataFrame.append = dataframe_append
    sys.path.insert(0, str(repo))
    from scenario_mining.activity_identification import main_fcn_veh_activity
    from scenario_mining.scenario_identification import mainFunctionScenarioIdentification
    from utils.helper_original_scenario import xosc_generation

    tracks = _validate_tracks(pd.read_csv(source))
    scenario = request["structured_scenario"]
    if scenario is None:
        from NLP.Scenario_Description_Understand import get_scenario_classification_via_LLM
        # Upstream prints a masked key suffix; suppress all output for this call.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            scenario = get_scenario_classification_via_LLM(
                openai_key=os.environ["OPENAI_API_KEY"],
                scenario_description=request["scenario_description"], model=request["model"],
            )
        if scenario is None:
            raise RuntimeError("The upstream LLM classifier failed; check the API key, model access, quota, and network")
    scenario = validate_structured_scenario(scenario)
    longitudinal, lateral, interactions = main_fcn_veh_activity(tracks)
    candidates = mainFunctionScenarioIdentification(tracks, scenario, lateral, longitudinal, interactions)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0", "tool": "Chat2Scenario", "repository": UPSTREAM_URL,
        "upstream_commit": UPSTREAM_COMMIT, "source_sha256_normalized_lf": SOURCE_HASHES,
        "input": {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                  "format": "highD", "frame_rate": 25, "synthetic": request["synthetic"]},
        "bypassed_llm": request["structured_scenario"] is not None,
        "scenario_description": request["scenario_description"], "classification": scenario,
        "model": request["model"] if request["structured_scenario"] is None else None,
        "candidate_count": len(candidates), "max_scenarios": request["max_scenarios"],
        "truncated": len(candidates) > request["max_scenarios"], "scenarios": [], "artifacts": [],
        "compatibility": {"pandas_append_shim": append_shim},
        "versions": {package: importlib.metadata.version(package) for package in (
            "pandas", "numpy", "scipy", "scenariogeneration", "streamlit", "openai")},
        "limitations": [
            "Only highD straight-road geometry, 25 Hz and one target description are validated.",
            "Upstream finds only the first matching activity interval per vehicle and requires a target to be precedingId at least once.",
            "No criticality threshold is applied; results are functional scenario matches.",
            "CSV preserves original highD coordinates. XOSC centers bounding boxes, flips y and uses ego direction for constant heading.",
            "Vehicle length and width are measured; car category, 1.5 m height, axle geometry and performance are export assumptions.",
            "No road geometry is inferred; supply road_file for simulation with a matching OpenDRIVE map.",
        ],
    }
    for index, candidate in enumerate(candidates[:request["max_scenarios"]], start=1):
        ego, targets, start, end = candidate
        ego, targets, start, end = int(ego), [int(x) for x in targets], int(start), int(end)
        if end <= start:
            continue
        ids = [ego, *targets]
        segment = tracks[tracks["id"].isin(ids) & tracks["frame"].between(start, end)].copy()
        for actor in ids:
            if len(segment[segment["id"] == actor]) != end - start + 1:
                raise ValueError(f"Mined actor {actor} does not cover the complete scenario interval")
        identifier = f"scenario_{index:04d}"
        csv_path, xosc_path = output / f"{identifier}_tracks.csv", output / f"{identifier}.xosc"
        # The complete source recording remains linked by path and checksum;
        # this CSV contains only selected actors in the mined interval.
        segment.to_csv(csv_path, index=False)
        actor_tracks = []
        for actor in ids:
            track = segment[segment["id"] == actor].copy().reset_index(drop=True)
            track["time"] = (track["frame"] - start) / 25.0
            track["x"] += track["width"] / 2.0
            track["y"] = -(track["y"] + track["height"] / 2.0)
            actor_tracks.append(track)
        direction = float(actor_tracks[0]["x"].iloc[-1] - actor_tracks[0]["x"].iloc[0])
        heading = math.pi if direction < 0 else 0.0
        duration = (end - start) / 25.0
        xml = xosc_generation(duration, actor_tracks[0], actor_tracks[1:], 2, heading)
        xml = _portable_xosc(xml, actor_tracks, request["road_file"], output)
        xosc_path.write_text(xml, encoding="utf-8")
        manifest["scenarios"].append({
            "scenario_id": identifier, "ego_id": ego, "target_ids": targets,
            "start_frame": start, "end_frame": end, "duration_seconds": duration,
            "trajectories": str(csv_path), "xosc": str(xosc_path),
            "artifacts": [{"path": str(path), "format": fmt,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                          for path, fmt in ((csv_path, "highD_csv"), (xosc_path, "OpenSCENARIO_1.2"))],
        })
        manifest["artifacts"].extend([
            {"path": str(csv_path), "role": "mined_trajectories", "scenario_id": identifier},
            {"path": str(xosc_path), "role": "openscenario", "scenario_id": identifier},
        ])
    manifest["scenario_count"] = len(manifest["scenarios"])
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def worker_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, required=True)
    args = parser.parse_args()
    try:
        _worker_run(json.loads(args.worker.read_text(encoding="utf-8")))
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        key = os.environ.get("OPENAI_API_KEY", "")
        if key:
            message = message.replace(key, "[REDACTED]")
        print(message, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    worker_main()
