"""Sequential manual-span controller; only committed manifests are checkpoints."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from uuid import uuid4

from .artifacts import ArtifactError, ArtifactStore, atomic_write, json_bytes, read_json_model
from .config import PipelineConfig
from .contracts import RunManifest, SourceArtifact, ManualRenderRequest
from .media_common import MediaError, disk_check, reference
from .profile import media_environment
from .runner import inspect_run, transition


def _commit(store, manifest):
    atomic_write(store.run_dir(manifest.run_id) / "manifest.json", json_bytes(manifest))
    return manifest


def _worker(action, directory, config_path, config, **inputs):
    directory.mkdir(parents=True, exist_ok=False)
    argv = [sys.executable, "-m", "clip_pipeline.media_worker", action,
            "--config", str(config_path), "--directory", str(directory)]
    for name, path in inputs.items():
        if path is not None:
            argv.extend(["--" + name, str(path)])
    with (directory / "worker.log").open("wb") as log:
        child = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
            env=media_environment(config.media), start_new_session=True,
            cwd=Path(__file__).resolve().parents[1])
        try:
            code = child.wait(timeout=(config.limits.download_timeout_seconds if action == "ingest"
                                       else config.limits.media_timeout_seconds))
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise MediaError("media_stage_timeout") from None
    if code:
        error = directory / "error.json"
        reason = json.loads(error.read_text()).get("error") if error.is_file() else "media_worker_failed"
        raise MediaError(reason)
    files = json.loads((directory / "result.json").read_text())
    for path in files.values():
        if not Path(path).resolve().is_relative_to(directory.resolve()) or not Path(path).is_file():
            raise ArtifactError("worker output escapes its stage directory")
    return {k: Path(v) for k, v in files.items()}


def execute_manual(workspace: Path, run_id: str):
    store = ArtifactStore(workspace)
    with store.lock():
        manifest = inspect_run(workspace, run_id)
        if manifest.execution_kind != "manual_render":
            raise ArtifactError("only explicitly requested manual renders can execute")
        if manifest.state == "finished":
            raise ArtifactError("failed terminal runs are immutable; submit a new run to reuse valid caches")
        if manifest.state == "checkpointed":
            return manifest
        directory = store.run_dir(run_id)
        config_path = directory / manifest.artifacts["config"].path
        config = read_json_model(config_path, PipelineConfig)
        def path(key):
            return directory / manifest.artifacts[key].path if key in manifest.artifacts else None
        def stage(action, **inputs):
            nonlocal manifest
            disk_check(directory, config.limits.min_free_disk_bytes)
            work = directory / "stages" / f"{action}-{uuid4().hex}"
            outputs = _worker(action, work, config_path, config, **inputs)
            required = {"ingest": {"source", "source_media", "ingest"},
                        "transcribe": {"transcript", "transcription"},
                        "render": {"final", "final_media", "clean_media", "render_report", "layout_ranges"}}[action]
            if not required.issubset(outputs):
                raise MediaError("incomplete_worker_result")
            refs = {**manifest.artifacts, **{k: reference(p, directory) for k, p in outputs.items()}}
            manifest = RunManifest.model_validate({**manifest.model_dump(), "artifacts": refs})
        manifest = _commit(store, transition(manifest))
        try:
            if manifest.stage == "configured":
                stage("ingest", submission=path("submission"))
                manifest = _commit(store, transition(manifest, stage="ingested"))
            source = read_json_model(path("source"), SourceArtifact)
            request = read_json_model(path("render_request"), ManualRenderRequest)
            if request.span.end > source.duration_seconds:
                raise MediaError("render_span_exceeds_source")
            if manifest.stage == "ingested":
                stage("transcribe", source=path("source"), media=path("source_media"), supplied=path("supplied_transcript"))
                manifest = _commit(store, transition(manifest, stage="transcribed"))
            if manifest.stage == "transcribed":
                manifest = _commit(store, transition(manifest, stage="selected", reason_code="manual_span_only"))
            if manifest.stage == "selected":
                stage("render", source=path("source"), media=path("source_media"),
                      transcript=path("transcript"), request=path("render_request"))
                manifest = transition(manifest, stage="rendered", reason_code="manual_render_unaudited")
            manifest = RunManifest.model_validate({**manifest.model_dump(), "state": "checkpointed",
                                                   "updated_at": datetime.now(timezone.utc)})
            return _commit(store, manifest)
        except (MediaError, OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, MediaError) else "media_stage_failed"
            _commit(store, transition(manifest, outcome="failed", reason_code=reason))
            raise
