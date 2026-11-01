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
            argv.extend(["--" + name.replace("_", "-"), str(path)])
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


def execute_stories(workspace: Path, run_id: str, *, stop_after_stories: int | None = None):
    """Resume only pinned inputs. A failed story is explicit and retryable on request."""
    from contextlib import contextmanager
    from .contracts import StoryPlan, StoryArtifact, SequenceManifest, StoryProgress, TranscriptArtifact
    from .artifacts import digest
    from .stories import all_stories, validate_plan_source, validate_plan_transcript
    from .report import story_preview

    if stop_after_stories is not None and stop_after_stories < 1:
        raise ArtifactError("stop-after-stories must be positive")

    @contextmanager
    def termination():
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt
        old = signal.signal(signal.SIGTERM, interrupted)
        try:
            yield
        finally:
            signal.signal(signal.SIGTERM, old)

    store = ArtifactStore(workspace)
    with store.lock(), termination():
        manifest = inspect_run(workspace, run_id)
        if manifest.execution_kind != "manual_stories" or manifest.state == "finished":
            raise ArtifactError("only nonterminal manual story runs can resume")
        if manifest.state == "checkpointed":
            return manifest
        directory = store.run_dir(run_id)
        def path(key):
            return directory / manifest.artifacts[key].path if key in manifest.artifacts else None
        config_path = path("config")
        config = read_json_model(config_path, PipelineConfig)
        plan = read_json_model(path("story_plan"), StoryPlan)

        def save(**changes):
            nonlocal manifest
            manifest = RunManifest.model_validate({**manifest.model_dump(), **changes,
                                                   "updated_at": datetime.now(timezone.utc)})
            _commit(store, manifest)

        def progress(story_id, **changes):
            entry = manifest.story_progress[story_id]
            save(story_progress={**manifest.story_progress,
                                 story_id: StoryProgress.model_validate({**entry.model_dump(), **changes})})

        def worker(action, prefix="", **inputs):
            disk_check(directory, config.limits.min_free_disk_bytes)
            outputs = _worker(action, directory / "stages" / f"{action}-{uuid4().hex}",
                              config_path, config, **inputs)
            required = {"ingest": {"source", "source_media", "ingest"},
                        "transcribe": {"transcript", "transcription"},
                        "render-story": {"story", "final", "final_media", "clean_media",
                                         "render_report", "layout_ranges", "timeline"}}[action]
            if not required.issubset(outputs):
                raise MediaError("incomplete_worker_result")
            return {prefix + k: reference(p, directory) for k, p in outputs.items()}

        active_story = None
        save(state="running", reason_code="manual_stories_started")
        try:
            if manifest.stage == "configured":
                refs = worker("ingest", submission=path("submission"))
                save(artifacts={**manifest.artifacts, **refs}, stage="ingested")
            source = read_json_model(path("source"), SourceArtifact)
            validate_plan_source(plan, source)
            if manifest.stage == "ingested":
                refs = worker("transcribe", source=path("source"), media=path("source_media"),
                              supplied=path("supplied_transcript"))
                save(artifacts={**manifest.artifacts, **refs}, stage="transcribed")
            transcript = read_json_model(path("transcript"), TranscriptArtifact)
            validate_plan_transcript(plan, source, transcript, config)
            if manifest.stage == "transcribed":
                save(stage="selected", reason_code="manual_story_plan_only")
            newly_rendered = 0
            for story in all_stories(plan):
                entry = manifest.story_progress[story.story_id]
                if entry.state == "rendered":
                    continue
                active_story = story.story_id
                progress(active_story, state="running", attempts=entry.attempts + 1, reason_code=None)
                prefix = f"story.{active_story}."
                refs = worker("render-story", prefix, source=path("source"), media=path("source_media"),
                              transcript=path("transcript"), request=path("story_plan"), story_id=active_story)
                result = read_json_model(directory / refs[prefix + "story"].path, StoryArtifact)
                if result.story != story or result.final.source_sha256 != source.media_sha256:
                    raise MediaError("story_result_mismatch")
                # Files and completed progress become visible in the same atomic commit.
                save(artifacts={**manifest.artifacts, **refs}, story_progress={**manifest.story_progress,
                    active_story: StoryProgress(state="rendered", attempts=entry.attempts + 1,
                                                artifact_key=prefix + "story")})
                # Validate every nested reference before trusting this checkpoint.
                manifest = inspect_run(workspace, run_id)
                active_story = None
                newly_rendered += 1
                if stop_after_stories and newly_rendered >= stop_after_stories and any(
                        p.state != "rendered" for p in manifest.story_progress.values()):
                    save(state="interrupted", reason_code="operator_story_checkpoint")
                    return manifest

            sequences, refs = [], {}
            for sequence in plan.sequences:
                stories = [read_json_model(path(f"story.{s.story_id}.story"), StoryArtifact) for s in sequence.stories]
                output = SequenceManifest(plan_sha256=digest(plan), source_sha256=source.media_sha256,
                    transcript_sha256=plan.transcript_sha256, sequence=sequence, stories=stories,
                    duration_seconds=sum(s.final.duration_seconds for s in stories))
                if output.duration_seconds > config.stories.max_sequence_seconds + 0.2 * len(stories):
                    raise MediaError("actual_sequence_duration_limit")
                target = directory / f"sequence-{sequence.sequence_id}-{uuid4().hex}.json"
                atomic_write(target, json_bytes(output))
                refs["sequence." + sequence.sequence_id] = reference(target, directory)
                sequences.append(output)
            preview = directory / f"preview-{uuid4().hex}.html"
            atomic_write(preview, story_preview(plan, sequences).encode())
            refs["preview"] = reference(preview, directory)
            index = directory / f"sequences-{uuid4().hex}.json"
            atomic_write(index, json_bytes({"schema_version": "1", "approved": False,
                "audit_performed": False, "plan_sha256": digest(plan), "artifact_root": "run_directory",
                "sequences": [{"sequence_id": s.sequence.sequence_id, "title": s.sequence.title,
                               "manifest": refs["sequence." + s.sequence.sequence_id].model_dump(),
                               "duration_seconds": s.duration_seconds,
                               "preferred_duration_review": not config.stories.preferred_sequence_min_seconds <=
                                   s.duration_seconds <= config.stories.preferred_sequence_max_seconds}
                              for s in sequences]}))
            refs["sequence_index"] = reference(index, directory)
            save(artifacts={**manifest.artifacts, **refs}, state="checkpointed", stage="rendered",
                 reason_code="manual_stories_unaudited")
            return inspect_run(workspace, run_id)
        except KeyboardInterrupt:
            if active_story:
                progress(active_story, state="interrupted", reason_code="operator_interrupted", artifact_key=None)
            save(state="interrupted", reason_code="operator_interrupted")
            raise
        except (MediaError, OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, MediaError) else "story_stage_failed"
            if active_story:
                progress(active_story, state="failed", reason_code=reason, artifact_key=None)
            save(state="retryable", reason_code=reason)
            raise
