"""Opt-in Docker test: real models and FFmpeg, no mocks, network, or Google SDK."""
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, "/app")
from clip_pipeline.media_common import file_hash, probe, validate_output


def run(argv):
    result = subprocess.run([str(x) for x in argv], capture_output=True, text=True, timeout=900)
    if result.returncode:
        print(result.stdout[-4000:], result.stderr[-8000:], file=sys.stderr)
        for log in root.glob("workspace/runs/*/stages/*/worker.log"):
            print(str(log), log.read_text(errors="replace")[-5000:], file=sys.stderr)
        raise RuntimeError("media smoke command failed")
    return result.stdout


root = Path(sys.argv[1] if len(sys.argv) > 1 else "/data/smoke")
root.mkdir(parents=True, exist_ok=True)
assert not {"google-genai", "elevenlabs"}.intersection({d.metadata["Name"].lower() for d in distributions()})
assert Path("/app/fonts/Anton-Regular.ttf").is_file(), "legacy caption font must ship with the image"
os.environ.update(GEMINI_API_KEY="sentinel-must-never-be-used", AUTO_LAYOUT="1", SCREENCAST_LAYOUT="1",
                  SUBTITLE_BAND_CHECK="1", HOOK_GROUNDING="1", HF_HUB_OFFLINE="1")
# These are actual imports and actual detector construction, not mocked modules.
run([sys.executable, "-c", """
from clip_pipeline.config import MediaConfig
from clip_pipeline.profile import activate_media_profile
activate_media_profile(MediaConfig())
import sys, main, reframe_v2, gemini_worker
assert 'google.genai' not in sys.modules and 'app' not in sys.modules and 'cloud' not in sys.modules
try:
    main.get_visual_clips('unused.mp4', 8)
except RuntimeError:
    pass
else:
    raise AssertionError('legacy selection reached')
print('real media imports succeeded without Google')
"""])
wav = root / "speech.wav"
run(["espeak-ng", "-s", "135", "-w", wav,
     "Higher borrowing costs can reduce corporate investment. Investors also watch company earnings and inflation. "
     "These are economic factors that can affect the stock market."])
source = root / "source.mp4"
run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15", "-i", wav,
     "-shortest", "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", source])
(root / "markets.json").write_text(json.dumps({"schema_version": "1", "version": "synthetic-media-test", "markets": []}))
config = {
    "workspace": str(root / "workspace"), "policy_path": "/app/configs/market-policy.example.json",
    "foundry": {"endpoint": "https://unused.openai.azure.com/openai/v1/", "auth_mode": "developer_identity",
                "relevance_deployment": "unused", "selection_deployment": "unused", "audit_deployment": "unused"},
    "markets": {"mode": "snapshot", "path": str(root / "markets.json")},
    "media": {"whisper_model": "tiny", "layout": "track_general"},
    "selection": {"min_seconds": 5.0, "max_seconds": 15.0},
    "limits": {"min_free_disk_bytes": 1, "max_source_seconds": 60, "max_source_bytes": 20000000,
               "media_timeout_seconds": 900},
}
config_path = root / "config.json"
config_path.write_text(json.dumps(config))


def render(path, start=0, supplied=None):
    argv = [sys.executable, "-m", "clip_pipeline", "render", "--config", config_path,
            "--input", path, "--start", start, "--end", start + 8]
    if supplied:
        argv.extend(["--transcript", supplied])
    response = json.loads(run(argv))
    assert response["state"] == "checkpointed" and response["stage"] == "rendered"
    assert response["outcome"] is None and response["approved"] is False
    directory = Path(response["manifest"]).parent
    manifest = json.loads(Path(response["manifest"]).read_text())
    def artifact(name):
        return directory / manifest["artifacts"][name]["path"]
    assert artifact("final_media") != artifact("clean_media")
    assert artifact("captions_ass").stat().st_size and artifact("captions_srt").stat().st_size
    validate_output(artifact("final_media"), 8)
    assert file_hash(artifact("final_media")) == json.loads(artifact("final").read_text())["media"]["sha256"]
    return response, artifact, json.loads(artifact("transcription").read_text())


first, first_artifact, first_asr = render(source)
assert first_asr["reused"] is False
second, second_artifact, second_asr = render(source, 1)
assert second_asr["reused"] is True and second_asr["cache_key"] == first_asr["cache_key"]
assert file_hash(first_artifact("transcript")) == file_hash(second_artifact("transcript"))
resumed = json.loads(run([sys.executable, "-m", "clip_pipeline", "resume", "--workspace", root / "workspace",
                         "--run-id", first["run_id"], "--render-only"]))
assert resumed["final_media"] == first["final_media"]
# Same filename and duration, different bytes: cached ASR must be invalidated.
changed = root / "changed.mp4"
run(["ffmpeg", "-y", "-i", source, "-c", "copy", "-metadata", "comment=changed-source", changed])
os.replace(changed, source)
config["media"]["layout"] = "preserve_full_width"
config_path.write_text(json.dumps(config))
third, _, third_asr = render(source)
assert third_asr["reused"] is False and third_asr["cache_key"] != first_asr["cache_key"]
# Real portrait source plus a source-bound transcript import (same copied audio).
portrait = root / "portrait.mp4"
run(["ffmpeg", "-y", "-i", source, "-vf", "scale=360:640", "-c:v", "libx264", "-threads", "2",
     "-c:a", "copy", portrait])
transcript = json.loads(first_artifact("transcript").read_text())
transcript.update(source_sha256=file_hash(portrait), duration_seconds=probe(portrait)["duration"])
supplied = root / "portrait-transcript.json"
supplied.write_text(json.dumps(transcript))
fourth, _, _ = render(portrait, supplied=supplied)
report = {"ok": True, "synthetic_source": True, "google_sdk_installed": False, "real_models": True,
          "javascript_runtime": run(["deno", "--version"]).strip(),
          "asr_cache_reused": True, "changed_source_invalidated_asr": True,
          "layouts": ["track_general", "preserve_full_width", "already_vertical"],
          "runs": [first, second, third, fourth]}
(root / "smoke-report.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
