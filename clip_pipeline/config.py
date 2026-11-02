"""Explicit nonsecret configuration. No dotenv, environment reads, or network IO."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator, model_serializer

from .contracts import Contract, Text, TopicPolicy, MarketSnapshot, clean_https_url

EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]


class FoundryConfig(Contract):
    provider: Literal["foundry"] = "foundry"
    endpoint: Text
    relevance_deployment: Text
    selection_deployment: Text
    audit_deployment: Text
    auth_mode: Literal["managed_identity", "developer_identity", "api_key"] = "managed_identity"
    api_key_env: EnvName | None = None
    timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 120.0
    max_attempts: Annotated[int, Field(ge=1, le=5)] = 3
    identity_scope: Literal["https://ai.azure.com/.default", "https://cognitiveservices.azure.com/.default"] = "https://ai.azure.com/.default"
    managed_identity_client_id: Annotated[str, Field(
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )] | None = None
    structured_outputs: Literal[True] = True
    output_token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    max_output_tokens: Annotated[int, Field(ge=128, le=32768)] = 4096
    temperature: Annotated[float, Field(ge=0, le=2)] | None = None
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    # Deployment-specific USD per million tokens; absent prices mean unknown cost.
    prices: dict[str, "TokenPrices"] = Field(default_factory=dict)

    @field_validator("endpoint")
    @classmethod
    def inference_url(cls, value):
        clean_https_url(value)
        parsed = urlsplit(value)
        if (not (parsed.hostname or "").endswith((".openai.azure.com", ".services.ai.azure.com",
                                                   ".cognitiveservices.azure.com")) or
                parsed.port not in (None, 443)):
            raise ValueError("expected a public Azure inference host")
        if parsed.path.rstrip("/") != "/openai/v1":
            raise ValueError("expected an Azure OpenAI /openai/v1 inference endpoint")
        return value.rstrip("/") + "/"

    @model_validator(mode="after")
    def auth_combination(self):
        if (self.auth_mode == "api_key") != (self.api_key_env is not None):
            raise ValueError("api_key mode requires only a secret environment-variable reference")
        if self.managed_identity_client_id and self.auth_mode != "managed_identity":
            raise ValueError("client ID is only valid with managed identity")
        return self


class TokenPrices(Contract):
    input_per_million: Annotated[float, Field(ge=0)]
    output_per_million: Annotated[float, Field(ge=0)]


class ApiMarkets(Contract):
    mode: Literal["api"]
    url: Text
    bearer_token_env: EnvName | None = None
    max_cache_age_seconds: Annotated[int, Field(ge=0, le=86400)] = 3600

    _url = field_validator("url")(clean_https_url)


class SnapshotMarkets(Contract):
    mode: Literal["snapshot"]
    path: Text
    purpose: Literal["development"] = "development"


class MockApiMarkets(Contract):
    mode: Literal["mock_api"]
    path: Text
    purpose: Literal["development"] = "development"


class SelectionConfig(Contract):
    min_seconds: Annotated[float, Field(ge=5, le=175)] = 15.0
    max_seconds: Annotated[float, Field(ge=10, le=180)] = 60.0
    min_clips: Annotated[int, Field(ge=0, le=0)] = 0
    max_candidates: Annotated[int, Field(ge=1, le=100)] = 5
    context_budget_tokens: Annotated[int, Field(ge=1024)] = 16000
    window_seconds: Annotated[float, Field(ge=10, le=300)] = 45.0
    overlap_segments: Annotated[int, Field(ge=1, le=10)] = 1
    relevance_batch_windows: Annotated[int, Field(ge=1, le=20)] = 4

    @model_validator(mode="after")
    def duration_band(self):
        if self.max_seconds <= self.min_seconds:
            raise ValueError("max_seconds must exceed min_seconds")
        return self


class StoriesConfig(Contract):
    max_sequences: Annotated[int, Field(ge=1, le=100)] = 5
    max_stories_per_sequence: Annotated[int, Field(ge=1, le=100)] = 5
    max_story_seconds: Annotated[float, Field(gt=0, le=3600)] = 300.0
    max_sequence_seconds: Annotated[float, Field(gt=0, le=14400)] = 900.0
    preferred_sequence_min_seconds: Annotated[float, Field(ge=0)] = 60.0
    preferred_sequence_max_seconds: Annotated[float, Field(gt=0)] = 180.0

    @model_validator(mode="after")
    def preferred_band(self):
        if not self.preferred_sequence_min_seconds < self.preferred_sequence_max_seconds <= self.max_sequence_seconds:
            raise ValueError("preferred sequence band must fit the hard sequence limit")
        return self


class MediaConfig(Contract):
    aspect_ratio: Literal["9:16"] = "9:16"
    width: Literal[1080] = 1080
    height: Literal[1920] = 1920
    video_codec: Literal["h264"] = "h264"
    audio_codec: Literal["aac"] = "aac"
    layout: Literal["track_general", "preserve_full_width"] = "track_general"
    captions: bool = True
    hook_overlay: bool = False
    language: Text = "en"
    whisper_model: Text = "small"
    whisper_device: Literal["cpu", "cuda"] = "cpu"
    whisper_compute: Literal["int8", "float16", "float32"] = "int8"
    encoder: Literal["x264", "nvenc", "auto"] = "x264"
    render_workers: Annotated[int, Field(ge=1, le=1)] = 1

    @model_validator(mode="after")
    def compute_device(self):
        if self.whisper_device == "cpu" and self.whisper_compute == "float16":
            raise ValueError("CPU transcription cannot use float16")
        return self


class AuditConfig(Contract):
    whisper_model: Text = "small"
    context_padding_seconds: Annotated[float, Field(ge=0, le=600)] = 45.0

    @model_serializer(mode="wrap")
    def preserve_defaults(self, handler):
        data = handler(self)
        if self.whisper_model == "small":
            data.pop("whisper_model", None)
        if self.context_padding_seconds == 45.0:
            data.pop("context_padding_seconds", None)
        return data

    final_audio_asr: Literal[True] = True
    require_manual_review: Literal[True] = True


class BlobConfig(Contract):
    account_url: Text
    runs_container: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")]
    approved_container: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")]

    @field_validator("account_url")
    @classmethod
    def account_endpoint(cls, value):
        clean_https_url(value)
        parsed = urlsplit(value)
        if not (parsed.hostname or '').endswith('.blob.core.windows.net') or parsed.port not in (None, 443):
            raise ValueError("expected an Azure Blob account endpoint")
        if parsed.path not in ("", "/"):
            raise ValueError("expected a storage account URL, not a container or blob")
        return value.rstrip("/")

    @model_validator(mode="after")
    def distinct_containers(self):
        if self.runs_container == self.approved_container:
            raise ValueError("run and approved containers must be separate")
        if any("--" in name for name in (self.runs_container, self.approved_container)):
            raise ValueError("container names cannot contain consecutive hyphens")
        return self


class LimitsConfig(Contract):
    media_timeout_seconds: Annotated[int, Field(ge=30, le=86400)] = 14400
    download_timeout_seconds: Annotated[int, Field(ge=30, le=86400)] = 1800
    max_source_seconds: Annotated[int, Field(gt=0)] = 14400
    max_source_bytes: Annotated[int, Field(gt=0)] = 10737418240
    min_free_disk_bytes: Annotated[int, Field(gt=0)] = 21474836480
    max_model_calls: Annotated[int, Field(gt=0)] = 100
    max_model_tokens: Annotated[int, Field(gt=0)] = 1000000
    max_jobs: Annotated[int, Field(ge=1, le=1)] = 1


class EnrichmentOff(Contract):
    mode: Literal["off"] = "off"


class EnrichmentSnapshot(Contract):
    mode: Literal["snapshot"]
    path: Text
    max_candidates_per_story: Annotated[int, Field(ge=1, le=200)] = 30
    membership_max_age_days: Annotated[int, Field(ge=1, le=3650)] = 365
    relationship_max_age_days: Annotated[int, Field(ge=1, le=3650)] = 90


class PipelineConfig(Contract):
    schema_version: Literal["1"] = "1"
    workspace: Text
    policy_path: Text
    foundry: FoundryConfig
    markets: Annotated[ApiMarkets | SnapshotMarkets | MockApiMarkets, Field(discriminator="mode")]
    enrichment: Annotated[EnrichmentOff | EnrichmentSnapshot, Field(discriminator="mode")] = Field(default_factory=EnrichmentOff)
    blob: BlobConfig | None = None
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    stories: StoriesConfig = Field(default_factory=StoriesConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    @model_serializer(mode="wrap")
    def preserve_off_hashes(self, handler):
        data = handler(self)
        if self.enrichment.mode == "off":
            data.pop("enrichment", None)
        return data

    @model_validator(mode="after")
    def model_budget(self):
        if self.foundry.max_output_tokens >= self.selection.context_budget_tokens:
            raise ValueError("context budget must leave room for input and reserved output")
        return self


def resolve_path(value: str, relative_to: Path) -> Path:
    path = Path(value).expanduser()
    return (relative_to / path).resolve()


def load_config(path: Path) -> PipelineConfig:
    from .artifacts import read_json_model

    path = path.resolve()
    config = read_json_model(path, PipelineConfig)
    data = config.model_dump(mode="json")
    for key in ("workspace", "policy_path"):
        data[key] = str(resolve_path(data[key], path.parent))
    if config.markets.mode in {"snapshot", "mock_api"}:
        data["markets"]["path"] = str(resolve_path(config.markets.path, path.parent))
    if config.enrichment.mode == "snapshot":
        data["enrichment"]["path"] = str(resolve_path(config.enrichment.path, path.parent))
    return PipelineConfig.model_validate(data)


def load_inputs(config: PipelineConfig) -> tuple[TopicPolicy, MarketSnapshot | None]:
    from .artifacts import read_json_model

    if config.enrichment.mode == "snapshot":
        from .enrichment import load_knowledge
        load_knowledge(config.enrichment)
    policy = read_json_model(Path(config.policy_path), TopicPolicy)
    snapshot = (read_json_model(Path(config.markets.path), MarketSnapshot)
                if config.markets.mode in {"snapshot", "mock_api"} else None)
    return policy, snapshot
