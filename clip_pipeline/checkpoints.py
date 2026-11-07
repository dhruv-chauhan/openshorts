"""Content-verified private run backups; restore preserves the canonical workspace."""
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

from .artifacts import ArtifactError, ArtifactStore, atomic_write, json_bytes, read_json_model
from .config import PipelineConfig
from .contracts import ArtifactRef, RunManifest
from .media_common import file_hash
from .storage import BlobSink


def backup_run(workspace, run_id, *, include_source=False, sink_factory=BlobSink):
    from .runner import inspect_run
    store = ArtifactStore(Path(workspace))
    with store.lock():
        manifest = inspect_run(store.workspace, run_id)
        directory = store.run_dir(run_id)
        config = read_json_model(directory / manifest.artifacts['config'].path, PipelineConfig)
        if config.blob is None:
            raise ArtifactError('checkpoint backup needs Blob configuration')
        raw = (directory / 'manifest.json').read_bytes()
        manifest_sha = hashlib.sha256(raw).hexdigest()
        prefix = 'checkpoints/' + run_id + '/' + manifest_sha
        files, omitted = {}, []
        with sink_factory(config) as sink:
            sink.ensure_private(config.blob.runs_container)
            for name, ref in manifest.artifacts.items():
                if name == 'source_media' and not include_source:
                    omitted.append(name)
                    continue
                key = prefix + '/objects/' + ref.sha256
                sink.put(config.blob.runs_container, key, path=directory / ref.path,
                         sha256=ref.sha256, size_bytes=ref.size_bytes)
                files[name] = {'key': key, **ref.model_dump()}
            key = prefix + '/manifest.json'
            sink.put(config.blob.runs_container, key, data=raw, sha256=manifest_sha, size_bytes=len(raw))
            checkpoint = {'schema_version': '1', 'run_id': run_id, 'workspace': str(store.workspace),
                'manifest': {'key': key, 'sha256': manifest_sha, 'size_bytes': len(raw)},
                'files': files, 'omitted': omitted, 'approved': False}
            # Recheck local bytes after upload; this is a checkpoint, never an approval index.
            if inspect_run(store.workspace, run_id) != manifest:
                raise ArtifactError('run changed during backup')
            raw = json_bytes(checkpoint)
            key = prefix + '/checkpoint.json'
            sha = hashlib.sha256(raw).hexdigest()
            sink.put(config.blob.runs_container, key, data=raw, sha256=sha, size_bytes=len(raw))
        return {'schema_version': '1', 'container': config.blob.runs_container, 'key': key,
                'sha256': sha, 'size_bytes': len(raw), 'run_id': run_id, 'approved': False}


def download_verified(client, container, key, path, sha256, size_bytes):
    if size_bytes < 0:
        raise ArtifactError('invalid download length')
    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    h = hashlib.sha256()
    with path.open('xb') as output:
        for chunk in client.get_blob_client(container, key).download_blob().chunks():
            total += len(chunk)
            if total > size_bytes:
                raise ArtifactError('checkpoint download exceeds declared size')
            h.update(chunk)
            output.write(chunk)
    if total != size_bytes or h.hexdigest() != sha256:
        raise ArtifactError('checkpoint remote checksum mismatch')


def restore_run(config, receipt, *, source_media=None, sink_factory=BlobSink):
    from .runner import inspect_run
    store = ArtifactStore(Path(config.workspace))
    if config.blob is None or receipt.get('container') != config.blob.runs_container:
        raise ArtifactError('restore requires the configured private run container')
    run_id = receipt['run_id']
    target = store.run_dir(run_id)
    with store.lock():
        if target.exists():
            raise ArtifactError('restore never overwrites an existing run')
        temporary = store.workspace / ('.restore-' + uuid4().hex)
        temporary.mkdir(mode=0o700)
        published=False
        try:
            with sink_factory(config) as sink:
                download_verified(sink.client, receipt['container'], receipt['key'], temporary / 'checkpoint.json',
                                  receipt['sha256'], receipt['size_bytes'])
                checkpoint = json.loads((temporary / 'checkpoint.json').read_text())
                if checkpoint['schema_version'] != '1' or checkpoint['run_id'] != run_id or checkpoint['workspace'] != str(store.workspace):
                    raise ArtifactError('restore must use the original canonical workspace')
                m = checkpoint['manifest']
                download_verified(sink.client, receipt['container'], m['key'], temporary / 'manifest.json',m['sha256'],m['size_bytes'])
                manifest = read_json_model(temporary / 'manifest.json', RunManifest)
                omitted = set(checkpoint['omitted'])
                if (manifest.run_id != run_id or omitted - {'source_media'} or
                        set(checkpoint['files']) | omitted != set(manifest.artifacts) or set(checkpoint['files']) & omitted):
                    raise ArtifactError('checkpoint coverage mismatch')
                restored = temporary / 'run'
                restored.mkdir()
                for name, ref in manifest.artifacts.items():
                    path = restored / ref.path
                    if not path.resolve().is_relative_to(restored.resolve()):
                        raise ArtifactError('invalid restore path')
                    if name in omitted:
                        if source_media is None or file_hash(Path(source_media)) != ref.sha256:
                            raise ArtifactError('restore requires exact source media omitted from backup')
                        path.parent.mkdir(parents=True,exist_ok=True)
                        shutil.copyfile(source_media,path)
                    else:
                        saved = checkpoint['files'][name]
                        if ArtifactRef.model_validate({k:saved[k] for k in ('path','sha256','size_bytes')}) != ref:
                            raise ArtifactError('checkpoint artifact binding mismatch')
                        if not path.exists():
                            download_verified(sink.client, receipt['container'], saved['key'], path,ref.sha256,ref.size_bytes)
                atomic_write(restored / 'manifest.json', (temporary / 'manifest.json').read_bytes())
                # Validate staged bytes/config before making the run visible.
                for ref in manifest.artifacts.values():
                    path = restored / ref.path
                    if path.stat().st_size != ref.size_bytes or file_hash(path) != ref.sha256:
                        raise ArtifactError('restored artifact checksum mismatch')
                pinned = read_json_model(restored / manifest.artifacts['config'].path,PipelineConfig)
                if Path(pinned.workspace).resolve() != store.workspace:
                    raise ArtifactError('pinned workspace cannot be relocated')
                target.parent.mkdir(parents=True,exist_ok=True)
                restored.rename(target)
                published=True
            return inspect_run(store.workspace,run_id)
        except Exception:
            if published:
                shutil.rmtree(target)
            raise
        finally:
            shutil.rmtree(temporary,ignore_errors=True)
