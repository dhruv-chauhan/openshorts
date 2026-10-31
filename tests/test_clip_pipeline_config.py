"""Phase A: bad inputs fail before creating artifacts or touching services."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from clip_pipeline.artifacts import ArtifactError, read_json_model
from clip_pipeline.config import PipelineConfig, load_config, load_inputs
from clip_pipeline.contracts import FixtureBundle, MarketSnapshot, SourceInput, TopicPolicy
from clip_pipeline.runner import prepare_run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/clip_pipeline"


@pytest.fixture
def config_data(tmp_path):
    data = json.loads((FIXTURES / "config.json").read_text())
    data.update(workspace=str(tmp_path / "workspace"),
                policy_path=str(ROOT / "configs/market-policy.example.json"))
    data["markets"]["path"] = str(FIXTURES / "markets.json")
    return data


@pytest.fixture
def config_path(tmp_path, config_data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data))
    return path


def test_example_and_fixture_configs():
    for path in (ROOT / "configs/clip-only.example.json", FIXTURES / "config.json"):
        config = load_config(path)
        policy, snapshot = load_inputs(config)
        assert Path(config.workspace).is_absolute()
        assert policy.rules
        assert config.selection.min_clips == 0
        assert config.media.width / config.media.height == 9 / 16
        assert (snapshot is not None) == (config.markets.mode == "snapshot")


@pytest.mark.parametrize("section,key,value", [
    ("media", "width", 1920), ("media", "aspect_ratio", "16:9"),
    ("media", "render_workers", 2), ("media", "captions", "false"),
    ("media", "auto_layout", True), ("media", "whisper_compute", "float16"),
    ("selection", "min_clips", 1), ("selection", "min_clips", False),
    ("selection", "max_candidates", 0), ("selection", "max_seconds", 10.0),
    ("selection", "max_seconds", float("nan")),
    ("audit", "require_manual_review", False), ("limits", "max_jobs", 2),
    ("foundry", "api_key", "secret"), ("foundry", "provider", "gemini"),
    ("foundry", "endpoint", "https://example.com/api/projects/clip"),
    ("foundry", "endpoint", "https://user:secret@example.com/openai/v1"),
    ("foundry", "endpoint", "https://example.com/openai/v1?token=secret"),
    ("foundry", "timeout_seconds", "120"),
])
def test_invalid_settings_do_not_create_workspace(config_path, config_data, section, key, value):
    config_data.setdefault(section, {})[key] = value
    config_path.write_text(json.dumps(config_data))
    with pytest.raises(ValidationError):
        prepare_run(config_path, fixture_path=FIXTURES / "job.json")
    assert not Path(config_data["workspace"]).exists()


def test_auth_requires_reference_only(config_data):
    config_data["foundry"]["auth_mode"] = "api_key"
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(config_data)
    config_data["foundry"]["api_key_env"] = "CLIP_FOUNDRY_KEY"
    assert PipelineConfig.model_validate(config_data).foundry.api_key_env == "CLIP_FOUNDRY_KEY"
    config_data["foundry"]["auth_mode"] = "managed_identity"
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(config_data)


def test_snapshot_mode_cannot_silently_be_api(config_data):
    config_data["markets"]["url"] = "https://example.invalid/markets"
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(config_data)


def test_duplicate_json_keys_rejected(config_path):
    config_path.write_text('{"schema_version":"1","schema_version":"1"}')
    with pytest.raises(ArtifactError, match="duplicate"):
        load_config(config_path)


def test_unknown_assets_duplicate_markets_and_rules_rejected():
    markets = json.loads((FIXTURES / "markets.json").read_text())
    markets["markets"].append(markets["markets"][0])
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(markets)
    markets["markets"] = markets["markets"][:1]
    markets["markets"][0]["asset_type"] = "unrecognized"
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(markets)
    policy = json.loads((ROOT / "configs/market-policy.example.json").read_text())
    policy["rules"].append(policy["rules"][0])
    with pytest.raises(ValidationError):
        TopicPolicy.model_validate(policy)


def test_source_url_normalization():
    source = SourceInput(kind="youtube", locator="https://youtu.be/abcdefghijk?secret=never-save#fragment")
    assert source.locator == "https://www.youtube.com/watch?v=abcdefghijk"
    assert source.permission_status == "unknown"
    for url in ("https://youtube.com/playlist?list=abcdefghijk", "http://youtu.be/abcdefghijk",
                "https://youtube.com.evil.invalid/watch?v=abcdefghijk", "https://localhost/video"):
        with pytest.raises(ValidationError):
            SourceInput(kind="youtube", locator=url)


def test_fixture_is_explicit_and_source_bound(tmp_path):
    fixture = read_json_model(FIXTURES / "job.json", FixtureBundle)
    assert fixture.synthetic and fixture.candidates[0].matched_market_ids == []
    for change in (lambda d: d.update(synthetic=False),
                   lambda d: d["transcript"].update(source_sha256="b" * 64),
                   lambda d: d["candidates"][0]["evidence"][0].update(quote="invented")):
        data = fixture.model_dump(mode="json")
        change(data)
        with pytest.raises(ValidationError):
            FixtureBundle.model_validate_json(json.dumps(data))
