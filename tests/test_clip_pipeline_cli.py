import json
from pathlib import Path
import subprocess
import sys

from clip_pipeline.__main__ import main
from clip_pipeline.artifacts import ArtifactStore
from test_clip_pipeline_config import ROOT, FIXTURES, config_data, config_path


def test_cli_prepare_status_and_resume(config_path, config_data, capsys):
    assert main(["run", "--config", str(config_path), "--fixture", str(FIXTURES / "job.json"),
                 "--prepare-only"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] is None and result["execution_kind"] == "fixture"
    for command in ("status", "resume"):
        args = [command, "--workspace", config_data["workspace"], "--run-id", result["run_id"]]
        if command == "resume":
            args.append("--prepare-only")
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out)["input_fingerprint"] == result["input_fingerprint"]


def test_real_execution_is_not_fake_success(config_path, config_data, capsys):
    assert main(["run", "--config", str(config_path), "--url", "https://youtu.be/abcdefghijk"]) == 6
    assert json.loads(capsys.readouterr().err)["error"] == "execution_not_implemented"
    assert not Path(config_data["workspace"]).exists()


def test_validation_does_not_create_workspace(config_path, config_data, capsys):
    assert main(["validate", "--config", str(config_path)]) == 0
    assert json.loads(capsys.readouterr().out)["remote_services_checked"] is False
    assert not Path(config_data["workspace"]).exists()


def test_secret_never_appears_in_error_or_snapshots(config_path, config_data, monkeypatch, capsys):
    secret = "TOP-SECRET-test-value"
    config_data["foundry"].update(auth_mode="api_key", api_key_env="CLIP_FOUNDRY_KEY")
    monkeypatch.setenv("CLIP_FOUNDRY_KEY", secret)
    config_path.write_text(json.dumps(config_data))
    assert main(["run", "--config", str(config_path), "--fixture", str(FIXTURES / "job.json"),
                 "--prepare-only"]) == 0
    result = json.loads(capsys.readouterr().out)
    folder = Path(result["manifest"]).parent
    assert all(secret not in p.read_text() for p in folder.glob("*.json"))
    config_data["foundry"]["api_key"] = secret
    config_path.write_text(json.dumps(config_data))
    assert main(["validate", "--config", str(config_path)]) == 2
    assert secret not in capsys.readouterr().err


def test_busy_exit_code(config_path, config_data, capsys):
    with ArtifactStore(Path(config_data["workspace"])).lock():
        assert main(["run", "--config", str(config_path), "--fixture", str(FIXTURES / "job.json"),
                     "--prepare-only"]) == 5
    assert json.loads(capsys.readouterr().err)["error"] == "workspace_busy"


def test_preparation_has_no_heavy_imports_or_network(config_path):
    script = r'''
import importlib.abc, socket, sys
blocked = {'main', 'app', 'torch', 'cv2', 'ultralytics', 'mediapipe',
           'faster_whisper', 'google', 'gemini_worker', 'azure', 'openai', 'dotenv',
           'httpx', 'yt_dlp', 'transcribe_backends'}
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked:
            raise AssertionError('forbidden import: ' + fullname)
sys.meta_path.insert(0, Guard())
def deny(*args, **kwargs):
    raise AssertionError('network access attempted')
socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.create_connection = deny
socket.getaddrinfo = deny
from clip_pipeline.__main__ import main
assert main(['run', '--config', sys.argv[1], '--fixture', sys.argv[2], '--prepare-only']) == 0
assert not blocked.intersection(sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", script, str(config_path), str(FIXTURES / "job.json")],
                            cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
