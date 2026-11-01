"""File and FFmpeg boundaries shared by the isolated media stages."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess

from .contracts import ArtifactRef


class MediaError(RuntimeError):
    """Stable public reason code; diagnostic subprocess output stays in run logs."""


def file_hash(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def reference(path: Path, root: Path) -> ArtifactRef:
    return ArtifactRef(path=path.resolve().relative_to(root.resolve()).as_posix(),
                       sha256=file_hash(path), size_bytes=path.stat().st_size)


def disk_check(path: Path, minimum: int):
    if shutil.disk_usage(path).free < minimum:
        raise MediaError("insufficient_scratch_space")


def command(argv, *, timeout=1800):
    result = subprocess.run([str(x) for x in argv], capture_output=True, text=True,
                            errors="replace", timeout=timeout)
    if result.returncode:
        import sys
        print(result.stderr[-8000:], file=sys.stderr)
        raise MediaError("media_command_failed")
    return result.stdout


def probe(path: Path) -> dict:
    raw = json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                              "-of", "json", path], timeout=60))
    videos = [s for s in raw.get("streams", []) if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in raw.get("streams", []) if s.get("codec_type") == "audio"]
    if not videos:
        raise MediaError("source_has_no_video")
    video = videos[0]
    duration = float(raw.get("format", {}).get("duration", video.get("duration", 0)))
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError("invalid_media_duration")
    return {"duration": duration, "width": int(video["width"]), "height": int(video["height"]),
            "video_codec": video.get("codec_name"), "sar": video.get("sample_aspect_ratio"),
            "audio_codec": audios[0].get("codec_name") if audios else None,
            "audio_channels": int(audios[0].get("channels", 0)) if audios else 0}


def validate_output(path: Path, expected_duration: float) -> dict:
    info = probe(path)
    if (info["width"], info["height"], info["sar"], info["video_codec"], info["audio_codec"]) != (
            1080, 1920, "1:1", "h264", "aac") or info["audio_channels"] < 1:
        raise MediaError("invalid_delivery_format")
    if abs(info["duration"] - expected_duration) > 0.2:
        raise MediaError("render_duration_mismatch")
    command(["ffmpeg", "-v", "error", "-xerror", "-i", path,
             "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], timeout=600)
    return info
