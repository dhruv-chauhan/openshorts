"""Phase A controller: prepare immutable inputs; do not simulate later stages."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import __version__
from .artifacts import ArtifactError, ArtifactStore, atomic_write, digest, json_bytes, read_json_model
from .config import PipelineConfig, load_config, load_inputs
from .contracts import FixtureBundle, ManualRenderRequest, RunManifest, SourceInput, STAGES, TranscriptArtifact


def transition(manifest: RunManifest, *, stage=None, outcome=None,
               reason_code: str | None = None) -> RunManifest:
    """Validate lifecycle transitions without writing any state.

    Prepared -> running starts the configured stage; progress advances one stage
    at a time. Terminal results are immutable and may not be silently retried.
    """
    if manifest.state == "finished":
        raise ArtifactError("terminal runs cannot transition; create a new run")
    data = manifest.model_dump()
    if stage is not None:
        if stage not in STAGES or STAGES.index(stage) != STAGES.index(manifest.stage) + 1:
            raise ArtifactError("stages must advance exactly one step")
        if manifest.state != "running":
            raise ArtifactError("start the run before advancing stages")
        data["stage"] = stage
    data["state"] = "finished" if outcome is not None else "running"
    data["outcome"] = outcome
    data["reason_code"] = reason_code or ("stage_advanced" if stage else "run_started")
    if outcome is not None and not reason_code:
        raise ArtifactError("terminal outcomes require a reason code")
    data["updated_at"] = datetime.now(timezone.utc)
    return RunManifest.model_validate(data)


def prepare_run(config_path: Path, source: SourceInput | None = None, *,
                fixture_path: Path | None = None,
                transcript_path: Path | None = None,
                render_request: ManualRenderRequest | None = None) -> tuple[RunManifest, Path]:
    """Validate all input JSON before creating or locking a workspace.

    Media content is not opened, downloaded, or hashed here. Source binding and
    accurate timing checks against real media belong to Phase C.
    """
    config = load_config(config_path)
    policy, snapshot = load_inputs(config)
    if render_request is not None:
        duration = render_request.span.end - render_request.span.start
        if fixture_path or not config.selection.min_seconds <= duration <= config.selection.max_seconds:
            raise ArtifactError("manual render requires real media and a span within the configured duration band")
        if config.media.hook_overlay:
            raise ArtifactError("manual rendering has no audited hook copy; disable hook_overlay")
    if (source is None) == (fixture_path is None):
        raise ArtifactError("provide exactly one source or synthetic fixture")
    fixture = None
    if fixture_path is not None:
        if transcript_path is not None:
            raise ArtifactError("fixtures already contain a synthetic transcript")
        fixture = read_json_model(fixture_path, FixtureBundle)
        source = fixture.source.origin
        if snapshot is None:
            raise ArtifactError("fixture runs require an explicit development market snapshot")
        enabled = {m.market_id for m in snapshot.markets if m.enabled}
        if any(set(c.matched_market_ids) - enabled for c in fixture.candidates):
            raise ArtifactError("fixture candidates reference disabled or unknown markets")
    elif source.kind == "fixture":
        raise ArtifactError("synthetic sources require an explicit fixture bundle")
    elif source.kind == "local":
        path = Path(source.locator).expanduser().resolve()
        if not path.is_file():
            raise ArtifactError("local source must be an existing file")
        source = SourceInput(kind="local", locator=str(path), permission_status=source.permission_status)

    transcript = (read_json_model(transcript_path, TranscriptArtifact)
                  if transcript_path is not None else None)
    # Secret values are never read. Config accepts only environment-variable
    # references and credential-free service URLs, including in saved snapshots.
    inputs = {"config": ("resolved-config.json", config),
              "policy": ("policy.json", policy),
              "submission": ("submission.json", source)}
    if snapshot is not None:
        inputs["markets"] = ("market-snapshot.json", snapshot)
    if fixture is not None:
        inputs["fixture"] = ("fixture.json", fixture)
    if transcript is not None:
        inputs["supplied_transcript"] = ("supplied-transcript.json", transcript)
    if render_request is not None:
        inputs["render_request"] = ("render-request.json", render_request)

    store = ArtifactStore(Path(config.workspace))
    with store.lock():
        run_id = uuid4().hex
        directory = store.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        refs = {key: store.write_artifact(run_id, name, value)
                for key, (name, value) in inputs.items()}
        now = datetime.now(timezone.utc)
        manifest = RunManifest(
            implementation_version=__version__, run_id=run_id,
            created_at=now, updated_at=now,
            execution_kind=("manual_render" if render_request is not None else
                            "fixture" if fixture is not None else "preparation"),
            source=source, input_fingerprint=digest({k: v.sha256 for k, v in refs.items()}),
            artifacts=refs,
            input_artifact_keys=list(refs),
        )
        # A run only becomes visible as prepared when all its snapshots exist.
        atomic_write(directory / "manifest.json", json_bytes(manifest))
        return manifest, directory


def inspect_run(workspace: Path, run_id: str) -> RunManifest:
    store = ArtifactStore(workspace)
    manifest = store.read_manifest(run_id)
    if not {"config", "policy", "submission"}.issubset(manifest.artifacts):
        raise ArtifactError("run is missing required input snapshots")
    keys = manifest.input_artifact_keys if manifest.input_artifact_keys is not None else manifest.artifacts
    expected = digest({k: manifest.artifacts[k].sha256 for k in keys})
    if manifest.input_fingerprint != expected:
        raise ArtifactError("input fingerprint does not match the pinned artifacts")
    config = read_json_model(store.run_dir(run_id) / manifest.artifacts["config"].path, PipelineConfig)
    if Path(config.workspace).resolve() != store.workspace:
        raise ArtifactError("run belongs to another workspace")
    return manifest


def resume_preparation(workspace: Path, run_id: str) -> RunManifest:
    """Verify Phase A checkpoints without rereading mutable config/policy files."""
    store = ArtifactStore(workspace)
    with store.lock():
        manifest = inspect_run(workspace, run_id)
        if manifest.state != "prepared" or manifest.stage != "configured":
            raise ArtifactError("only a configured preparation can be resumed in Phase A")
        return manifest
