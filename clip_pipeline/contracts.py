"""Versioned boundaries for the controller and future media/provider workers."""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlsplit
import re

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints,
    field_validator, model_validator, model_serializer,
)

from .enrichment_contracts import StoryEnrichment

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RunId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Seconds = Annotated[float, Field(ge=0)]
Stage = Literal["configured", "ingested", "transcribed", "selected", "rendered",
                "audited", "reviewed", "stored"]
STAGES = ("configured", "ingested", "transcribed", "selected", "rendered",
          "audited", "reviewed", "stored")
Outcome = Literal["completed", "completed_no_matches", "needs_review",
                  "partial_failure", "failed"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True,
                              allow_inf_nan=False, validate_default=True)


def clean_https_url(value: str) -> str:
    """Configuration URLs cannot carry inline credentials or query tokens."""
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise ValueError("use an HTTPS URL without credentials, query, or fragment")
    # Also force validation of malformed ports.
    _ = parts.port
    return value


def youtube_url(value: str) -> str:
    """Canonicalize supported video URLs without fetching anything."""
    parts = urlsplit(value)
    if (parts.scheme != "https" or parts.username or parts.password
            or parts.port not in (None, 443)):
        raise ValueError("expected an HTTPS YouTube video URL")
    host = (parts.hostname or "").lower()
    path = parts.path.strip("/").split("/")
    if host == "youtu.be" and len(path) == 1:
        video_id = path[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if path == ["watch"]:
            ids = parse_qs(parts.query).get("v", [])
            video_id = ids[0] if len(ids) == 1 else ""
        elif len(path) == 2 and path[0] in {"shorts", "live"}:
            video_id = path[1]
        else:
            video_id = ""
    else:
        video_id = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("expected one YouTube video, not a channel or playlist")
    return f"https://www.youtube.com/watch?v={video_id}"


class SourceInput(Contract):
    kind: Literal["youtube", "local", "fixture"]
    locator: Text
    permission_status: Literal["unknown", "authorized"] = "unknown"

    @model_validator(mode="after")
    def validate_locator(self):
        if self.kind == "youtube":
            object.__setattr__(self, "locator", youtube_url(self.locator))
        return self


class SourceArtifact(Contract):
    schema_version: Literal["1"] = "1"
    source_id: Text
    origin: SourceInput
    media_sha256: Sha256
    duration_seconds: Annotated[float, Field(gt=0)]
    source_offset_seconds: Annotated[float, Field(ge=0, le=0)] = 0.0
    published_at: AwareDatetime | None = None


class TimeSpan(Contract):
    start: Seconds
    end: Seconds

    @model_validator(mode="after")
    def positive_span(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class ManualRenderRequest(Contract):
    schema_version: Literal["1"] = "1"
    selection_method: Literal["manual"] = "manual"
    span: TimeSpan
    approved: Literal[False] = False


class Word(TimeSpan):
    word: Text


class Segment(TimeSpan):
    segment_id: Text
    text: Text
    words: list[Word] = Field(default_factory=list)

    @model_validator(mode="after")
    def word_bounds(self):
        previous = self.start
        for word in self.words:
            if word.start < previous or word.end > self.end:
                raise ValueError("words must be ordered and within their segment")
            previous = word.end
        return self


class TranscriptArtifact(Contract):
    schema_version: Literal["1"] = "1"
    source_sha256: Sha256
    duration_seconds: Annotated[float, Field(gt=0)]
    source_offset_seconds: Annotated[float, Field(ge=0, le=0)] = 0.0
    language: Text
    backend: Text
    model_version: Text
    timing: Literal["segment", "word"]
    timing_warnings: list[Literal["zero_duration_words_grouped"]] = Field(default_factory=list)
    segments: list[Segment] = Field(min_length=1)

    @model_validator(mode="after")
    def transcript_bounds(self):
        previous = 0.0
        ids = set()
        for segment in self.segments:
            if (segment.start < previous or segment.end > self.duration_seconds
                    or segment.segment_id in ids):
                raise ValueError("segments must have unique IDs, ordered nonoverlapping source bounds")
            if self.timing == "word" and not segment.words:
                raise ValueError("word-timed segments must include words")
            ids.add(segment.segment_id)
            previous = segment.end
        return self


class Evidence(TimeSpan):
    segment_id: Text
    quote: Text


class Candidate(Contract):
    schema_version: Literal["1"] = "1"
    clip_id: Text
    source_sha256: Sha256
    source_duration_seconds: Annotated[float, Field(gt=0)]
    span: TimeSpan
    source_window_id: Text
    topic_path: Literal["allowed_market", "equity_macro"]
    matched_market_ids: list[Text] = Field(default_factory=list)
    market_connection: Text
    content_type: Literal["news", "opinion", "analysis", "education", "advice", "update"]
    evidence: list[Evidence] = Field(min_length=1)
    title: Text
    hook: Text | None = None

    @model_validator(mode="after")
    def eligibility_shape(self):
        if self.span.end > self.source_duration_seconds:
            raise ValueError("candidate exceeds the source duration")
        if self.topic_path == "allowed_market" and not self.matched_market_ids:
            raise ValueError("allowed-market candidates require a market ID")
        if len(set(self.matched_market_ids)) != len(self.matched_market_ids):
            raise ValueError("market IDs must be unique")
        if any(e.start < self.span.start or e.end > self.span.end for e in self.evidence):
            raise ValueError("candidate evidence must be inside the final span")
        return self


class ArtifactRef(Contract):
    path: Text
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=0)]

    @field_validator("path")
    @classmethod
    def relative_path(cls, value):
        path = PurePosixPath(value)
        if (path.is_absolute() or ".." in path.parts or "\\" in value
                or not path.parts or str(path) != value):
            raise ValueError("artifact path must be a normalized relative path")
        return value


class FinalArtifact(Contract):
    schema_version: Literal["1"] = "1"
    clip_id: Text
    source_sha256: Sha256
    source_span: TimeSpan
    media: ArtifactRef
    captions: ArtifactRef | None = None
    width: Literal[1080] = 1080
    height: Literal[1920] = 1920
    sample_aspect_ratio: Literal["1:1"] = "1:1"
    video_codec: Literal["h264"] = "h264"
    audio_codec: Literal["aac"] = "aac"
    audio_channels: Annotated[int, Field(gt=0)]
    duration_seconds: Annotated[float, Field(gt=0)]


class RuleResult(Contract):
    rule_id: Text
    result: Literal["pass", "reject", "review"]
    reason: Text

StoryId = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")]
PlaybackRate = Annotated[float, Field(ge=1.0, le=1.1)]


class StorySpec(Contract):
    enrichment: StoryEnrichment | None = None

    @model_serializer(mode="wrap")
    def preserve_unenriched_hashes(self, handler):
        data = handler(self)
        if self.enrichment is None:
            data.pop("enrichment", None)
        return data

    story_id: StoryId
    span: TimeSpan
    title: Text
    role: Literal["context", "claim", "explanation", "evidence", "qualification", "conclusion"] = "explanation"
    playback_rate: PlaybackRate = 1.0
    subject: Text | None = None
    matched_market_ids: list[Text] = Field(default_factory=list)
    speaker: Text | None = None
    timeframe: Text | None = None
    stance: Literal["bullish", "neutral", "bearish", "mixed", "unclear"] = Field(default="unclear",
        description="Use unclear when speaker is null or price direction is absent. All other values, including neutral and mixed, require an attributed speaker and evidence.")
    evidence: list[Evidence] = Field(default_factory=list)
    context: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def story_shape(self):
        if self.playback_rate not in (1.0, 1.1):
            raise ValueError("supported story rates are 1.0 and 1.1")
        if any(e.start < self.span.start or e.end > self.span.end for e in self.evidence):
            raise ValueError("story evidence must be inside the story span")
        if self.stance != "unclear" and (not self.evidence or not self.speaker or not self.subject):
            raise ValueError("directional stance requires attributed subject and evidence")
        if len(self.matched_market_ids) != len(set(self.matched_market_ids)):
            raise ValueError("duplicate market IDs")
        return self


class StorySequence(Contract):
    sequence_id: StoryId
    title: Text
    stories: list[StorySpec] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def source_order(self):
        for before, after in zip(self.stories, self.stories[1:]):
            if after.span.start < before.span.end:
                raise ValueError("stories must be chronological and nonoverlapping")
        return self


class StoryPlan(Contract):
    schema_version: Literal["1"] = "1"
    selection_method: Literal["manual", "foundry"] = "manual"
    selection_binding: dict[str, Sha256] = Field(default_factory=dict)
    source: SourceInput
    source_sha256: Sha256
    transcript_sha256: Sha256
    sequences: list[StorySequence] = Field(default_factory=list, max_length=100)
    approved: Literal[False] = False

    @model_serializer(mode="wrap")
    def preserve_manual_hashes(self, handler):
        data = handler(self)
        if not self.selection_binding:
            data.pop("selection_binding", None)
        return data

    @model_validator(mode="after")
    def identities(self):
        if self.selection_method == "foundry" and set(self.selection_binding) not in ({"markets", "policy", "prompts", "settings", "report"}, {"markets", "policy", "prompts", "settings", "report", "enrichment"}):
            raise ValueError("Foundry plans require selection provenance hashes")
        if self.selection_method == "foundry" and any(s.enrichment is not None and s.enrichment.knowledge_sha256 != self.selection_binding.get("enrichment") for q in self.sequences for s in q.stories):
            raise ValueError("enriched stories require matching plan provenance")
        if self.source.kind == "fixture":
            raise ValueError("story rendering requires real media")
        if len({s.sequence_id for s in self.sequences}) != len(self.sequences):
            raise ValueError("duplicate sequence IDs")
        stories = [story for sequence in self.sequences for story in sequence.stories]
        if len({s.story_id for s in stories}) != len(stories):
            raise ValueError("story IDs must be unique across the plan")
        ordered = sorted(stories, key=lambda s: s.span.start)
        if any(b.span.start < a.span.end for a, b in zip(ordered, ordered[1:])):
            raise ValueError("source spans may not overlap across sequences")
        return self


class StoryArtifact(Contract):
    schema_version: Literal["1"] = "1"
    story: StorySpec
    final: FinalArtifact
    clean_media: ArtifactRef
    layout_ranges: ArtifactRef
    timeline: ArtifactRef
    render_report: ArtifactRef
    captions_srt: ArtifactRef | None = None
    approved: Literal[False] = False
    audit_performed: Literal[False] = False

    @model_validator(mode="after")
    def identity_and_duration(self):
        if self.final.clip_id != self.story.story_id or self.final.source_span != self.story.span:
            raise ValueError("rendered story identity/span mismatch")
        expected = (self.story.span.end - self.story.span.start) / self.story.playback_rate
        if abs(self.final.duration_seconds - expected) > 0.2:
            raise ValueError("story duration does not match playback rate")
        return self


class SequenceManifest(Contract):
    schema_version: Literal["1"] = "1"
    plan_sha256: Sha256
    source_sha256: Sha256
    transcript_sha256: Sha256
    sequence: StorySequence
    stories: list[StoryArtifact]
    duration_seconds: Annotated[float, Field(gt=0)]
    approved: Literal[False] = False
    audit_performed: Literal[False] = False

    @model_validator(mode="after")
    def complete_sequence(self):
        if [s.story for s in self.stories] != self.sequence.stories:
            raise ValueError("sequence must contain exactly its planned stories in order")
        if any(s.final.source_sha256 != self.source_sha256 for s in self.stories):
            raise ValueError("sequence source mismatch")
        if abs(sum(s.final.duration_seconds for s in self.stories) - self.duration_seconds) > 0.001:
            raise ValueError("sequence duration must sum actual story durations")
        return self


class StoryProgress(Contract):
    state: Literal["pending", "running", "rendered", "failed", "interrupted"] = "pending"
    attempts: Annotated[int, Field(ge=0)] = 0
    reason_code: Text | None = None
    artifact_key: Text | None = None

    @model_validator(mode="after")
    def artifact_state(self):
        if (self.state == "rendered") != (self.artifact_key is not None):
            raise ValueError("only rendered stories have a committed artifact")
        return self


class AuditResult(Contract):
    schema_version: Literal["1"] = "1"
    clip_id: Text
    final_sha256: Sha256
    policy_version: Text
    market_snapshot_version: Text
    decision: Literal["pass", "reject", "needs_review"]
    rules: list[RuleResult] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_decision(self):
        results = {r.result for r in self.rules}
        expected = "reject" if "reject" in results else "needs_review" if "review" in results else "pass"
        if self.decision != expected or len({r.rule_id for r in self.rules}) != len(self.rules):
            raise ValueError("decision must reflect unique per-rule results")
        return self


class Market(Contract):
    market_id: Text
    underlying_name: Text
    asset_type: Literal["equity", "index", "commodity", "fx", "crypto", "pre_ipo", "etf"]
    aliases: list[Text] = Field(default_factory=list)
    enabled: bool


class MarketSnapshot(Contract):
    schema_version: Literal["1"] = "1"
    version: Text
    markets: list[Market]

    @model_validator(mode="after")
    def unique_markets(self):
        if len({m.market_id for m in self.markets}) != len(self.markets):
            raise ValueError("market IDs must be unique")
        return self


class PolicyRule(Contract):
    rule_id: Text
    severity: Literal["reject", "review"]
    instruction: Text
    positive_examples: list[Text] = Field(default_factory=list)
    negative_examples: list[Text] = Field(default_factory=list)


class TopicPolicy(Contract):
    schema_version: Literal["1"] = "1"
    version: Text
    allowed_market_description: Text
    equity_macro_description: Text
    rules: list[PolicyRule] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_rules(self):
        if len({r.rule_id for r in self.rules}) != len(self.rules):
            raise ValueError("policy rule IDs must be unique")
        return self


class FixtureBundle(Contract):
    """Synthetic contract exercise only: no assertion that playable media exists."""
    schema_version: Literal["1"] = "1"
    synthetic: Literal[True]
    source: SourceArtifact
    transcript: TranscriptArtifact
    candidates: list[Candidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def matching_source(self):
        if self.source.origin.kind != "fixture":
            raise ValueError("fixture source must be explicitly synthetic")
        if (self.transcript.source_sha256 != self.source.media_sha256
                or self.transcript.duration_seconds != self.source.duration_seconds):
            raise ValueError("fixture transcript must match the source")
        if len({c.clip_id for c in self.candidates}) != len(self.candidates):
            raise ValueError("fixture clip IDs must be unique")
        segments = {s.segment_id: s for s in self.transcript.segments}
        for candidate in self.candidates:
            if (candidate.source_sha256 != self.source.media_sha256
                    or candidate.source_duration_seconds != self.source.duration_seconds):
                raise ValueError("fixture candidate must match the source")
            for evidence in candidate.evidence:
                segment = segments.get(evidence.segment_id)
                if (segment is None or evidence.quote not in segment.text
                        or evidence.start < segment.start or evidence.end > segment.end):
                    raise ValueError("fixture evidence must match a transcript segment")
        return self


class RunManifest(Contract):
    schema_version: Literal["1"] = "1"
    implementation_version: Text
    run_id: RunId
    created_at: AwareDatetime
    updated_at: AwareDatetime
    execution_kind: Literal["preparation", "fixture", "manual_render", "manual_stories", "topic_selection", "story_audit"]
    source: SourceInput
    stage: Stage = "configured"
    state: Literal["prepared", "running", "interrupted", "retryable", "checkpointed", "finished"] = "prepared"
    outcome: Outcome | None = None
    reason_code: Text = "phase_a_prepared"
    input_fingerprint: Sha256
    artifacts: dict[str, ArtifactRef]
    input_artifact_keys: list[Text] | None = None
    story_progress: dict[StoryId, StoryProgress] = Field(default_factory=dict)

    @model_validator(mode="after")
    def state_consistency(self):
        if self.input_artifact_keys is not None and (
                len(set(self.input_artifact_keys)) != len(self.input_artifact_keys) or
                not set(self.input_artifact_keys).issubset(self.artifacts)):
            raise ValueError("input artifact keys must be unique and present")
        if self.state == "checkpointed" and not ((self.execution_kind in {"manual_render", "manual_stories"} and self.stage == "rendered") or (self.execution_kind == "topic_selection" and self.stage == "selected") or (self.execution_kind == "story_audit" and self.stage in {"audited", "reviewed"})):
            raise ValueError("only manual rendered output can stop at an unaudited checkpoint")
        if self.story_progress and self.execution_kind != "manual_stories":
            raise ValueError("story progress/retryable checkpoints require a story run")
        if self.state in {"interrupted", "retryable"} and self.execution_kind not in {"manual_stories", "topic_selection", "story_audit"}:
            raise ValueError("retryable checkpoints require a story or selection run")
        if any(p.artifact_key not in self.artifacts for p in self.story_progress.values() if p.artifact_key):
            raise ValueError("story progress references a missing artifact")
        if self.execution_kind == "manual_stories" and self.state == "checkpointed" and any(
                p.state != "rendered" for p in self.story_progress.values()):
            raise ValueError("a sequence checkpoint requires all stories")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if (self.state == "finished") != (self.outcome is not None):
            raise ValueError("only finished runs have terminal outcomes")
        if self.state == "prepared" and self.stage != "configured":
            raise ValueError("prepared runs must remain at configured")
        if self.outcome == "completed" and self.stage != "stored":
            raise ValueError("completion requires stored artifacts")
        if self.outcome == "completed_no_matches" and STAGES.index(self.stage) < STAGES.index("transcribed"):
            raise ValueError("no-match requires source evaluation, not just preparation")
        if self.outcome == "needs_review" and STAGES.index(self.stage) < STAGES.index("audited") and not (self.execution_kind == "topic_selection" and self.stage == "selected"):
            raise ValueError("review outcome requires an audit")
        if (self.execution_kind == "fixture") != (self.source.kind == "fixture"):
            raise ValueError("fixture runs require a fixture source")
        return self
