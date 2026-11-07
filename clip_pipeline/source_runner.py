"""Reusable acquisition/ASR checkpoint without a throwaway manual render."""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .artifacts import ArtifactError, ArtifactStore, atomic_write, json_bytes, read_json_model
from .config import PipelineConfig
from .contracts import RunManifest, SourceArtifact, TranscriptArtifact
from .execution import _worker, termination
from .media_common import disk_check, reference
from .transcript import validate_transcript


def validate_source_checkpoint(manifest, directory, config):
    required = {'source', 'source_media', 'ingest'}
    if manifest.stage == 'transcribed':
        required |= {'transcript', 'transcription'}
    if not required.issubset(manifest.artifacts):
        raise ArtifactError('incomplete source checkpoint')
    source = read_json_model(directory / manifest.artifacts['source'].path, SourceArtifact)
    if source.media_sha256 != manifest.artifacts['source_media'].sha256:
        raise ArtifactError('source media binding mismatch')
    if manifest.stage == 'transcribed':
        transcript = read_json_model(directory / manifest.artifacts['transcript'].path, TranscriptArtifact)
        validate_transcript(transcript, source, config.media)


def execute_source(workspace, run_id, *, worker=_worker):
    from .runner import inspect_run
    store = ArtifactStore(Path(workspace))
    with store.lock(), termination():
        manifest = inspect_run(store.workspace, run_id)
        if manifest.execution_kind != 'source':
            raise ArtifactError('not a source run')
        if manifest.state == 'checkpointed':
            return manifest
        directory = store.run_dir(run_id)
        def path(key):
            return directory / manifest.artifacts[key].path if key in manifest.artifacts else None
        config = read_json_model(path('config'), PipelineConfig)
        def save(**changes):
            nonlocal manifest
            manifest = RunManifest.model_validate(manifest.model_dump() | changes |
                {'updated_at': datetime.now(timezone.utc)})
            atomic_write(directory / 'manifest.json', json_bytes(manifest))
        def stage(action, required, **inputs):
            disk_check(directory, config.limits.min_free_disk_bytes)
            files = worker(action, directory / 'stages' / (action + '-' + uuid4().hex), path('config'), config, **inputs)
            if not required.issubset(files):
                raise ArtifactError('incomplete source worker response')
            return manifest.artifacts | {k: reference(p, directory) for k, p in files.items()}
        save(state='running', reason_code='source_started')
        try:
            if manifest.stage == 'configured':
                refs = stage('ingest', {'source', 'source_media', 'ingest'}, submission=path('submission'))
                validate_source_checkpoint(manifest.model_copy(update={'artifacts':refs,'stage':'ingested'}),directory,config)
                save(artifacts=refs, stage='ingested')
            if manifest.stage == 'ingested':
                refs = stage('transcribe', {'transcript', 'transcription'}, source=path('source'),
                    media=path('source_media'), supplied=path('supplied_transcript'))
                validate_source_checkpoint(manifest.model_copy(update={'artifacts':refs,'stage':'transcribed'}),directory,config)
                save(artifacts=refs, stage='transcribed')
            save(state='checkpointed', reason_code='source_ready')
            return inspect_run(store.workspace, run_id)
        except KeyboardInterrupt:
            save(state='interrupted', reason_code='source_interrupted')
            raise
        except Exception:
            save(state='retryable', reason_code='source_failed')
            raise
