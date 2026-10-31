import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from clip_pipeline.artifacts import ArtifactError, ArtifactStore, atomic_write, read_json_model
from clip_pipeline.contracts import AuditResult, ArtifactRef, FinalArtifact, FixtureBundle, TranscriptArtifact
from clip_pipeline.runner import inspect_run, prepare_run, resume_preparation, transition
from test_clip_pipeline_config import ROOT, FIXTURES, config_data, config_path


def test_preparation_is_reproducible_but_run_ids_are_unique(config_path):
    first, folder = prepare_run(config_path, fixture_path=FIXTURES / "job.json")
    second, _ = prepare_run(config_path, fixture_path=FIXTURES / "job.json")
    assert first.run_id != second.run_id
    assert first.input_fingerprint == second.input_fingerprint
    assert first.state == "prepared" and first.stage == "configured" and first.outcome is None
    saved = inspect_run(folder.parent.parent, first.run_id)
    assert saved == first
    assert not list(folder.glob("*.mp4"))


def test_resume_uses_pinned_inputs(config_path, config_data, tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text((ROOT / "configs/market-policy.example.json").read_text())
    config_data["policy_path"] = str(policy)
    config_path.write_text(json.dumps(config_data))
    original, folder = prepare_run(config_path, fixture_path=FIXTURES / "job.json")
    before = (folder / "manifest.json").read_bytes()
    policy.write_text("no longer valid")
    config_path.unlink()
    resumed = resume_preparation(Path(config_data["workspace"]), original.run_id)
    assert original == resumed
    assert (folder / "manifest.json").read_bytes() == before


def test_policy_change_changes_fingerprint(config_path, config_data, tmp_path):
    original, _ = prepare_run(config_path, fixture_path=FIXTURES / "job.json")
    policy = json.loads((ROOT / "configs/market-policy.example.json").read_text())
    policy["version"] = "changed"
    path = tmp_path / "new-policy.json"
    path.write_text(json.dumps(policy))
    config_data["policy_path"] = str(path)
    config_path.write_text(json.dumps(config_data))
    changed, _ = prepare_run(config_path, fixture_path=FIXTURES / "job.json")
    assert changed.input_fingerprint != original.input_fingerprint


def test_corrupt_checkpoint_refused(config_path):
    run, folder = prepare_run(config_path, fixture_path=FIXTURES / "job.json")
    (folder / "policy.json").write_text("{}")
    with pytest.raises(ArtifactError, match="checksum"):
        resume_preparation(folder.parent.parent, run.run_id)


def test_atomic_failure_preserves_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "manifest.json"
    atomic_write(target, b"old")
    def fail(*args):
        raise OSError("interrupted rename")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        atomic_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_workspace_lock_contends_across_processes_and_releases(tmp_path):
    script = """
import sys
from pathlib import Path
from clip_pipeline.artifacts import ArtifactStore, WorkspaceBusy
try:
    with ArtifactStore(Path(sys.argv[1])).lock():
        pass
except WorkspaceBusy:
    sys.exit(5)
"""
    store = ArtifactStore(tmp_path / "workspace")
    with store.lock():
        result = subprocess.run([sys.executable, "-c", script, str(store.workspace)], cwd=ROOT)
        assert result.returncode == 5
    result = subprocess.run([sys.executable, "-c", script, str(store.workspace)], cwd=ROOT)
    assert result.returncode == 0


def test_killed_process_does_not_leave_stale_lock(tmp_path):
    script = """
import sys, time
from pathlib import Path
from clip_pipeline.artifacts import ArtifactStore
with ArtifactStore(Path(sys.argv[1])).lock():
    print('locked', flush=True)
    time.sleep(30)
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], cwd=ROOT,
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdout.close()
    with ArtifactStore(tmp_path).lock():
        pass


def test_paths_and_immutable_artifacts(config_path):
    run, folder = prepare_run(config_path, fixture_path=FIXTURES / "job.json")
    store = ArtifactStore(folder.parent.parent)
    for name in ("../outside.json", "/tmp/outside.json", "a/../outside.json", "a\\b", "."):
        with pytest.raises(ValidationError):
            ArtifactRef(path=name, sha256="a" * 64, size_bytes=0)
    with pytest.raises(ValidationError):
        store.run_dir("../escape")
    with pytest.raises(ArtifactError, match="immutable"):
        store.write_artifact(run.run_id, "policy.json", {})


def test_lifecycle_never_reports_preparation_as_complete(config_path):
    prepared, _ = prepare_run(config_path, fixture_path=FIXTURES / "job.json")
    with pytest.raises(ValidationError):
        transition(prepared, outcome="completed", reason_code="incorrect")
    with pytest.raises(ValidationError):
        transition(prepared, outcome="completed_no_matches", reason_code="incorrect")
    with pytest.raises(ArtifactError):
        transition(prepared, stage="ingested")
    running = transition(prepared)
    with pytest.raises(ArtifactError):
        transition(running, stage="selected")
    transcribed = transition(transition(running, stage="ingested"), stage="transcribed")
    empty = transition(transcribed, outcome="completed_no_matches", reason_code="no_speech")
    assert empty.state == "finished"
    with pytest.raises(ArtifactError):
        transition(empty)


def test_transcript_rejects_invalid_timing():
    fixture = read_json_model(FIXTURES / "job.json", FixtureBundle)
    for change in (
        lambda d: d["segments"][0].update(end=31.0),
        lambda d: d["segments"][0]["words"][0].update(start=-1.0),
        lambda d: d["segments"][0]["words"][1].update(start=1.5),
        lambda d: d["segments"][0].update(words=[]),
        lambda d: d.update(duration_seconds=float("inf")),
    ):
        data = fixture.transcript.model_dump(mode="json")
        change(data)
        with pytest.raises(ValidationError):
            TranscriptArtifact.model_validate_json(json.dumps(data))


def test_final_artifact_and_audit_contracts():
    final = FinalArtifact(clip_id="c1", source_sha256="a" * 64,
                          source_span={"start": 0.0, "end": 30.0},
                          media={"path": "c1/final.mp4", "sha256": "b" * 64, "size_bytes": 100},
                          audio_channels=2, duration_seconds=30.0)
    assert final.width == 1080 and final.media.sha256 == "b" * 64
    data = final.model_dump()
    data["height"] = 1080
    with pytest.raises(ValidationError):
        FinalArtifact.model_validate(data)
    with pytest.raises(ValidationError):
        AuditResult(clip_id="c1", final_sha256="b" * 64, policy_version="v1",
                    market_snapshot_version="v1", decision="pass",
                    rules=[{"rule_id": "context", "result": "reject", "reason": "lost condition"}])
