"""Media-worker environment boundary. This module has no optional dependencies."""
import os
import sys


def clip_only_mode():
    return os.environ.get("CLIP_PIPELINE_MODE") == "1"


def media_environment(media, inherited=None):
    """Build a child environment without changing the controller's environment."""
    env = dict(os.environ if inherited is None else inherited)
    for key in list(env):
        if key.startswith(("GEMINI_", "LLM_")) or key in {
            "MANAGED_GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
            "SSRF_ALLOW_HOSTS", "PROXY_URL", "STATIC_PROXY_URLS", "YOUTUBE_COOKIES",
            "BGUTIL_HTTP_BASE_URL", "BGUTIL_BASE_URL", "BGUTIL_SCRIPT_PATH", "DOWNLOAD_SKIP_STATICS"
        }:
            env.pop(key)
    env.update({
        "CLIP_PIPELINE_MODE": "1", "PYTHON_DOTENV_DISABLED": "1",
        "EARLY_AUDIO": "0", "AUTO_LAYOUT": "0", "SCREENCAST_LAYOUT": "0",
        "SUBTITLE_BAND_CHECK": "0", "HOOK_GROUNDING": "0", "SPLIT_LAYOUT": "0",
        "SPEAKER_SIGNAL": "0", "SPEAKER_CUT": "0", "PUNCH_IN": "0",
        "AUTO_HOOK": "1" if media.hook_overlay else "0",
        "AUTO_CAPTIONS": "1" if media.captions else "0",
        "CLIP_WORKERS": "1", "FFMPEG_ENCODER": media.encoder,
        "WHISPER_DEVICE": media.whisper_device, "WHISPER_MODEL": media.whisper_model,
        "WHISPER_COMPUTE": media.whisper_compute, "LLM_PROVIDER": "foundry",
        "TRANSCRIBE_BACKEND": "whisper", "SCENE_ENGINE": "pyscenedetect",
        "REFRAME_ENGINE": "v2", "AUDIO_NORMALIZE": "0",
        "GPU_DECODE": "0", "GPU_DECODE_RENDER": "0", "GPU_RENDER": "0",
    })
    return env


def activate_media_profile(media):
    """Call only in a new media subprocess, before importing legacy renderers."""
    if any(name in sys.modules for name in (
        "main", "reframe_v2", "layout_picker", "screencast_layout", "gemini_worker"
    )):
        raise RuntimeError("media profile must be applied before importing media modules")
    env = media_environment(media)
    os.environ.clear()
    os.environ.update(env)


def reject_legacy_selection():
    if clip_only_mode():
        raise RuntimeError("legacy clip selection is disabled in the clip-only profile")
