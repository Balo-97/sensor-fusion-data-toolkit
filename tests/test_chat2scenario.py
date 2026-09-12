import copy
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from scenario_metadata.chat2scenario import (
    FOLLOWING_SCENARIO, _validate_tracks, create_demo_tracks,
    run_chat2scenario, validate_structured_scenario, verify_chat2scenario,
)
from scenario_metadata.pipeline import annotate_mining_result


def test_invalid_classification_and_missing_credentials(tmp_path, monkeypatch):
    invalid = copy.deepcopy(FOLLOWING_SCENARIO)
    invalid["Target Vehicle #2"] = invalid["Target Vehicle #1"]
    with pytest.raises(ValueError, match="exactly"):
        validate_structured_scenario(invalid)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        run_chat2scenario(tmp_path, tmp_path / "missing.csv", tmp_path / "out",
                          scenario_description="following", model="explicit-model")


def test_missing_source_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="missing or changed"):
        verify_chat2scenario(tmp_path)


def test_rejects_missing_frames_and_dangling_actors(tmp_path):
    pd = pytest.importorskip("pandas")
    path = create_demo_tracks(tmp_path / "tracks.csv")
    tracks = pd.read_csv(path)
    with pytest.raises(ValueError, match="contiguous"):
        _validate_tracks(tracks.drop(index=1))
    tracks.loc[0, "precedingId"] = 999
    with pytest.raises(ValueError, match="absent vehicle"):
        _validate_tracks(tracks)


@pytest.mark.integration
def test_actual_upstream_mining_and_metadata(tmp_path):
    pytest.importorskip("scenariogeneration")
    project = Path(__file__).resolve().parents[1]
    candidates = [project / "vendor" / "Chat2Scenario", project / "research" / "chat2scenario-upstream"]
    repo = next((p for p in candidates if (p / "scenario_mining").exists()), None)
    if repo is None:
        pytest.skip("Run scenario-metadata bootstrap to fetch pinned upstream code")
    source = create_demo_tracks(tmp_path / "synthetic.csv")
    manifest = run_chat2scenario(repo, source, tmp_path / "mining",
                                structured_scenario=FOLLOWING_SCENARIO, synthetic=True)
    assert manifest["scenario_count"] == 1
    scenario = manifest["scenarios"][0]
    assert (scenario["ego_id"], scenario["target_ids"]) == (1, [2])
    assert (scenario["start_frame"], scenario["end_frame"]) == (1, 101)
    assert scenario["duration_seconds"] == 4
    xml = ET.parse(scenario["xosc"]).getroot()
    assert len(xml.findall("./Entities/ScenarioObject")) == 2
    assert not xml.findall(".//CatalogReference")
    polylines = xml.findall(".//Polyline")
    assert len(polylines) == 2
    for trajectory in polylines:
        times = [float(v.attrib["time"]) for v in trajectory.findall("Vertex")]
        assert times[0] == 0 and times[-1] == 4
        assert len(times) == 101
    report = annotate_mining_result(manifest, tmp_path / "pipeline")
    assert len(report["annotations"]) == 2
    for annotation in report["annotations"]:
        metadata = json.loads(Path(annotation["json"]).read_text())
        assert metadata["scenario"]["ego_id"] == 1
        assert metadata["provenance"]["synthetic_input"] is True
        assert metadata["provenance"]["input_sha256"] == manifest["input"]["sha256"]
    request = copy.deepcopy(FOLLOWING_SCENARIO)
    request["Ego Vehicle"]["Ego longitudinal activity"] = ["acceleration"]
    unmatched = run_chat2scenario(repo, source, tmp_path / "no-match", structured_scenario=request, synthetic=True)
    assert unmatched["scenario_count"] == 0
    assert unmatched["artifacts"] == []
