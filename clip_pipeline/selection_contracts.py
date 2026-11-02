"""Strict model responses; none constitutes a final-media audit or approval."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from .contracts import Contract, Text, Evidence, StorySpec, StoryId, TimeSpan, RuleResult

TopicPath = Literal['allowed_market', 'equity_macro']


class WindowDecision(Contract):
    window_id: Text
    decision: Literal['eligible', 'irrelevant', 'review'] = Field(
        description="Clearly nonfinancial content is irrelevant even when a word overlaps a market alias. Review is for genuinely unresolved plausible market relevance/identity or material ambiguity.")
    topic_path: TopicPath | None
    matched_market_ids: list[Text]
    connection: str
    evidence: list[Evidence]
    continues_before: bool
    continues_after: bool
    reason: Text

    @model_validator(mode='after')
    def eligible_shape(self):
        if self.decision == 'eligible' and (not self.evidence or self.topic_path is None or not self.connection.strip()):
            raise ValueError('eligibility requires a topic path and evidence')
        if self.topic_path == 'allowed_market' and not self.matched_market_ids:
            raise ValueError('allowed-market path requires IDs')
        return self


class RelevanceResponse(Contract):
    windows: list[WindowDecision]


class SelectedStory(StorySpec):
    topic_path: Literal['allowed_market', 'equity_macro', 'market_exposure']
    market_connection: Text
    freshness: Literal['time_sensitive', 'evergreen', 'uncertain']
    event_date: Text | None
    evidence: list[Evidence] = Field(min_length=1, description="Exact source quotes. Every evidence start/end must lie inside this story's span; do not cite a full segment extending beyond the cut.")
    matched_market_ids: list[Text] = Field(default_factory=list,
        description="Only supplied enabled IDs whose underlying name or explicit alias is spoken inside this story. Never substitute an unrelated company's ID. Macro may use an empty list.")


class Omission(TimeSpan):
    reason: Text


class ProposedSequence(Contract):
    sequence_id: StoryId
    title: Text
    stories: list[SelectedStory] = Field(min_length=1, max_length=100,
        description="Complete contiguous ideas in source order. Keep question, answer and essential qualification together in one story; avoid sentence-sized fragments.")
    usefulness: Annotated[int, Field(ge=0, le=100)]
    omissions: list[Omission]


class ProposalResponse(Contract):
    sequences: list[ProposedSequence] = Field(max_length=100)
    review_reasons: list[Text]
    explanation: Text


class AttributedStance(Contract):
    speaker: Text = Field(description="An explicitly identifiable source speaker; never invent a name or generic label.")
    stance: Literal['bullish', 'neutral', 'bearish', 'mixed']
    timeframe: Text | None


class StoryDraft(Contract):
    story_id: StoryId
    title: Text
    subject: Text
    first_segment_id: Text
    last_segment_id: Text
    evidence_segment_ids: list[Text] = Field(min_length=1)
    context_segment_ids: list[Text]
    role: Literal['context', 'claim', 'explanation', 'evidence', 'qualification', 'conclusion']
    attribution: AttributedStance | None = Field(description="Null if the speaker or price direction is unknown. Descriptive facts without a price view do not imply neutral stance.")
    topic_path: TopicPath
    matched_market_ids: list[Text] = Field(description="Subset of the union of spoken_market_ids in this story's selected segment range. A name in another story is not a direct match. Use [] for provisional exposure if none is spoken here.")
    market_connection: Text
    freshness: Literal['time_sensitive', 'evergreen', 'uncertain']
    event_date: Text | None


class SequenceDraft(Contract):
    sequence_id: StoryId
    title: Text
    stories: list[StoryDraft] = Field(min_length=1, max_length=100)
    usefulness: Annotated[int, Field(ge=0, le=100)]
    omission_reasons: list[Text] = Field(description="One reason for each adjacent story pair, including natural pauses; length is number of stories minus one.")


class DraftResponse(Contract):
    sequences: list[SequenceDraft] = Field(max_length=100)
    review_reasons: list[Text]
    explanation: Text


class StoryCheck(Contract):
    story_id: StoryId
    rules: list[RuleResult]


class SequenceCheck(Contract):
    sequence_id: StoryId
    stories: list[StoryCheck]
    rules: list[RuleResult]
    explanation: Text


class SelectionReview(Contract):
    sequences: list[SequenceCheck]


class RankedSequence(Contract):
    sequence_id: StoryId
    duplicate_of: StoryId | None
    reason: Text


class RankingResponse(Contract):
    ordered: list[RankedSequence]


class EnrichedWindowDecision(WindowDecision):
    topic_path: Literal['allowed_market', 'equity_macro', 'market_exposure'] | None


class EnrichedRelevanceResponse(Contract):
    windows: list[EnrichedWindowDecision]


class EnrichedStoryDraft(StoryDraft):
    topic_path: Literal['allowed_market', 'equity_macro', 'market_exposure']


class EnrichedSequenceDraft(SequenceDraft):
    stories: list[EnrichedStoryDraft] = Field(min_length=1, max_length=100)


class EnrichedDraftResponse(DraftResponse):
    sequences: list[EnrichedSequenceDraft] = Field(max_length=100)
