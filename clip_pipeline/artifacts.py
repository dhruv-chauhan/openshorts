"""Small local artifact store; commit manifests last and never execute payloads."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

from pydantic import BaseModel, TypeAdapter

from .contracts import ArtifactRef, RunId, RunManifest

MAX_JSON_BYTES = 8 * 1024 * 1024


class ArtifactError(ValueError):
    pass


class WorkspaceBusy(ArtifactError):
    pass


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError("duplicate JSON object keys are not allowed")
        result[key] = value
    return result


def read_json_model(path: Path, model):
    with path.open("rb") as file:
        raw = file.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ArtifactError("JSON artifact exceeds the size limit")
    # Reject duplicate keys before Pydantic's normal JSON validation.
    json.loads(raw, object_pairs_hook=_no_duplicates)
    return model.model_validate_json(raw)


def json_bytes(value) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
    """Rename within one filesystem; failed writes leave the old file intact."""
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class ArtifactStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    @contextmanager
    def lock(self):
        """Host/workspace-wide POSIX lock, released even if the process dies."""
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.workspace / ".worker.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorkspaceBusy("another job holds this workspace lock") from None
            yield
        finally:
            os.close(fd)

    def run_dir(self, run_id: str) -> Path:
        TypeAdapter(RunId).validate_python(run_id)
        path = self.workspace / "runs" / run_id
        if not path.resolve().is_relative_to(self.workspace):
            raise ArtifactError("run directory escapes the workspace")
        return path

    def write_artifact(self, run_id: str, name: str, value) -> ArtifactRef:
        # ArtifactRef also rejects absolute paths and parent traversal.
        content = json_bytes(value)
        ref = ArtifactRef(path=name, sha256=hashlib.sha256(content).hexdigest(),
                          size_bytes=len(content))
        path = self.run_dir(run_id) / name
        if not path.resolve().is_relative_to(self.run_dir(run_id).resolve()):
            raise ArtifactError("artifact escapes the run directory")
        if path.exists():
            raise ArtifactError("run input artifacts are immutable")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(path, content)
        return ref

    def read_manifest(self, run_id: str) -> RunManifest:
        directory = self.run_dir(run_id)
        manifest = read_json_model(directory / "manifest.json", RunManifest)
        if manifest.run_id != run_id:
            raise ArtifactError("manifest does not belong to this run")
        for ref in manifest.artifacts.values():
            path = directory / ref.path
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ArtifactError("artifact escapes the run directory")
            with path.open("rb") as file:
                sha = hashlib.sha256()
                size = 0
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    sha.update(chunk)
                    size += len(chunk)
            if size != ref.size_bytes or sha.hexdigest() != ref.sha256:
                raise ArtifactError("artifact checksum mismatch; run cannot be resumed")
        return manifest
