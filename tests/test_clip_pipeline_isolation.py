"""Fresh-process isolation checks; heavy local detector imports are stubbed.

No media/model downloads: these verify the provider boundary, not rendering.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from clip_pipeline.config import MediaConfig
from clip_pipeline.profile import media_environment


ROOT = Path(__file__).resolve().parents[1]


def test_environment_is_explicit_and_parent_is_unchanged():
    inherited = {"GEMINI_API_KEY": "sentinel", "MANAGED_GEMINI_API_KEY": "sentinel",
                 "GOOGLE_API_KEY": "sentinel", "LLM_BASE_URL": "https://bad.invalid",
                 "AUTO_LAYOUT": "1", "PATH": os.environ["PATH"]}
    env = media_environment(MediaConfig(), inherited)
    assert inherited["AUTO_LAYOUT"] == "1" and inherited["GEMINI_API_KEY"] == "sentinel"
    assert env["AUTO_LAYOUT"] == "0" and env["PYTHON_DOTENV_DISABLED"] == "1"
    assert not any(k.startswith("GEMINI_") for k in env)
    assert "LLM_BASE_URL" not in env and "GOOGLE_API_KEY" not in env
    assert env["CLIP_WORKERS"] == "1" and env["FFMPEG_ENCODER"] == "x264"


def run_child(script):
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_neutral_imports_and_media_profile_without_google():
    run_child(r'''
import importlib.abc, sys, os
from unittest.mock import MagicMock
class NoGoogle(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "google.genai" or fullname.startswith("google.genai."):
            raise AssertionError("Google SDK import attempted: " + fullname)
sys.meta_path.insert(0, NoGoogle())
import llm_schemas, llm_json, llm_backend
assert "gemini_worker" not in sys.modules
from clip_pipeline.config import MediaConfig
from clip_pipeline.profile import activate_media_profile
os.environ.update(GEMINI_API_KEY="sentinel", AUTO_LAYOUT="1", SCREENCAST_LAYOUT="1")
activate_media_profile(MediaConfig())
# Even an accidental later key injection cannot enable a forbidden path.
os.environ["GEMINI_API_KEY"] = "sentinel"
# Keep this test free of local ML dependencies and weight downloads. Only the
# local detector imports are stubbed; provider modules and guards are real.
for name in ("scenedetect", "scenedetect.detectors", "ultralytics", "torch", "mediapipe", "yt_dlp"):
    sys.modules[name] = MagicMock()
import main, gemini_worker, reframe_v2, layout_picker, screencast_layout, burned_subtitles, hook_grounding
assert gemini_worker.ScoreResponse is llm_schemas.ScoreResponse
assert gemini_worker._parse_json_response_text is llm_json._parse_json_response_text
layout_picker.ENABLED = screencast_layout.ENABLED = burned_subtitles.ENABLED = True
assert layout_picker.pick("missing.mp4", 60) == "none"
assert screencast_layout.detect_content_ranges("missing.mp4", [(0,60)], 30) == []
assert burned_subtitles.detect("missing.mp4") is None
assert hook_grounding.reground("missing.mp4", {}, {}, 0, 60) is None
for call in (lambda: main.get_viral_clips({}, 60),
             lambda: main.get_visual_clips("missing.mp4", 60),
             lambda: main._run_gemini_stage(None, "x", "x", object),
             lambda: gemini_worker.genai.Client(api_key="sentinel")):
    try:
        call()
    except RuntimeError as exc:
        assert "clip-only" in str(exc)
    else:
        raise AssertionError("forbidden inference path was reachable")
assert "google.genai" not in sys.modules
try:
    activate_media_profile(MediaConfig())
except RuntimeError:
    pass
else:
    raise AssertionError("late profile activation allowed")
''')


def test_legacy_schema_parser_reexports_without_loading_sdk():
    run_child('''
import sys
import gemini_worker, llm_schemas, llm_json
assert "google.genai" not in sys.modules
assert gemini_worker.DetailResponse is llm_schemas.DetailResponse
assert gemini_worker._parse_json_response_text('```json\\n{"shorts": []}\\n```') == {"shorts": []}
''')


def test_lazy_google_handle_preserves_legacy_client_patching(monkeypatch):
    pytest.importorskip("google.genai")
    from llm_google import genai
    import google.genai
    marker = object()
    monkeypatch.setattr(genai, "Client", marker)
    assert google.genai.Client is marker
