"""Bounded source acquisition; uses the repository's yt-dlp/SSRF policy helpers."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import os
from pathlib import Path

from .artifacts import atomic_write, digest, json_bytes, read_json_model
from .contracts import SourceArtifact, SourceInput
from .media_common import MediaError, disk_check, file_hash, probe


def _check_source(path, config):
    if path.stat().st_size > config.limits.max_source_bytes:
        raise MediaError("source_size_limit")
    info = probe(path)
    if info["duration"] > config.limits.max_source_seconds:
        raise MediaError("source_duration_limit")
    if not info["audio_channels"]:
        raise MediaError("source_has_no_audio")
    return info


def download_youtube(source: SourceInput, directory: Path, config):
    import yt_dlp
    import ssrf_guard
    from security_utils import assert_public_url
    from yt_clients import fallback_extractor_args, youtube_non_video_reason

    if youtube_non_video_reason(source.locator):
        raise MediaError("not_a_single_video")
    completed_bytes = {}
    def progress(event):
        name = event.get("filename", "stream")
        completed_bytes[name] = max(completed_bytes.get(name, 0), event.get("downloaded_bytes") or 0)
        if sum(completed_bytes.values()) > config.limits.max_source_bytes:
            raise MediaError("source_size_limit")
        disk_check(directory, config.limits.min_free_disk_bytes)

    def match(info, *, incomplete=False):
        duration = info.get("duration")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise MediaError("live_source_unsupported")
        if duration is not None and (not math.isfinite(float(duration)) or
                                     float(duration) > config.limits.max_source_seconds):
            raise MediaError("source_duration_limit")
        if duration is None and not incomplete:
            raise MediaError("unknown_source_duration")
        return None

    options = {
        "format": "bv*[protocol=https][height<=1080]+ba[protocol=https]/b[protocol=https][height<=1080]",
        "outtmpl": str(directory / "download.%(ext)s"), "merge_output_format": "mp4",
        "noplaylist": True, "playlist_items": "1", "cachedir": False,
        "proxy": "", "socket_timeout": 30, "retries": 2, "fragment_retries": 2,
        "concurrent_fragment_downloads": 1, "max_filesize": config.limits.max_source_bytes,
        "progress_hooks": [progress], "match_filter": match,
        "extractor_args": fallback_extractor_args(None, None),
        "quiet": True, "no_warnings": True,
    }
    with ssrf_guard.guarded():
        assert_public_url(source.locator)
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(source.locator, download=True)
    if not info or info.get("_type") in {"playlist", "multi_video"}:
        raise MediaError("download_failed")
    files = [p for p in directory.glob("download.*") if p.suffix in {".mp4", ".mkv", ".webm"}]
    if len(files) != 1:
        raise MediaError("download_output_ambiguous")
    published = None
    if info.get("upload_date"):
        try:
            published = datetime.strptime(info["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return files[0], published


def ingest(source: SourceInput, directory: Path, config) -> tuple[SourceArtifact, Path, bool]:
    """Called under the workspace lock. A cache entry is valid only after hashing."""
    cache = Path(config.workspace) / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    directory.mkdir(parents=True, exist_ok=True)
    disk_check(directory, config.limits.min_free_disk_bytes)
    locator_path = cache / "locators" / (digest(source) + ".json")
    destination = directory / "source.media"
    if source.kind == "youtube" and locator_path.exists():
        artifact = read_json_model(locator_path, SourceArtifact)
        blob = cache / "sources" / artifact.media_sha256 / "source.media"
        if artifact.origin != source or not blob.is_file() or file_hash(blob) != artifact.media_sha256:
            raise MediaError("source_cache_corrupt")
        info = _check_source(blob, config)
        if abs(info["duration"] - artifact.duration_seconds) > 0.001:
            raise MediaError("source_cache_corrupt")
        os.link(blob, destination)
        return artifact, destination, True
    published = None
    if source.kind == "local":
        path = Path(source.locator)
        if not path.is_file() or path.stat().st_size > config.limits.max_source_bytes:
            raise MediaError("invalid_local_source")
        before = path.stat()
        with path.open("rb") as src, destination.open("xb") as dst:
            size = 0
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                size += len(chunk)
                if size > config.limits.max_source_bytes:
                    raise MediaError("source_size_limit")
                disk_check(directory, config.limits.min_free_disk_bytes)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise MediaError("source_changed_during_ingest")
    elif source.kind == "youtube":
        downloaded, published = download_youtube(source, directory, config)
        downloaded.rename(destination)
    else:
        raise MediaError("synthetic_source_not_renderable")
    info = _check_source(destination, config)
    sha = file_hash(destination)
    artifact = SourceArtifact(source_id=(source.locator.rsplit("=", 1)[-1] if source.kind == "youtube" else sha),
                              origin=source, media_sha256=sha, duration_seconds=info["duration"],
                              published_at=published)
    blob = cache / "sources" / sha / "source.media"
    reused = blob.exists()
    if reused:
        if file_hash(blob) != sha:
            raise MediaError("source_cache_corrupt")
        destination.unlink()
        os.link(blob, destination)
    else:
        blob.parent.mkdir(parents=True, exist_ok=True)
        # A hard link publishes an already closed complete file atomically.
        os.link(destination, blob)
    if source.kind == "youtube":
        locator_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(locator_path, json_bytes(artifact))
    return artifact, destination, reused
