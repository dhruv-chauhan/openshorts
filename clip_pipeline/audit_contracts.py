"""Final-media audit and explicit human review; neither silently approves content."""
from typing import Literal
from pydantic import AwareDatetime, Field, model_validator
from .contracts import Contract, Text, Sha256, StoryId, RuleResult, TranscriptArtifact


class AuditIssue(Contract):
    issue_id: Text
    rule_id: Text
    severity: Literal['reject', 'review']
    kind: Literal['media', 'asr_uncertainty', 'source_claim', 'numbers', 'context',
                  'freshness', 'relevance', 'association', 'attribution', 'caption']
    explanation: Text
    source_segment_ids: list[Text]
    final_segment_ids: list[Text]


class SemanticAudit(Contract):
    decision: Literal['pass', 'reject', 'needs_review']
    rules: list[RuleResult]
    issues: list[AuditIssue]
    summary: Text

    @model_validator(mode='after')
    def consistent(self):
        if len({r.rule_id for r in self.rules}) != len(self.rules):
            raise ValueError('duplicate audit rule')
        if len({i.issue_id for i in self.issues}) != len(self.issues):
            raise ValueError('duplicate audit issue')
        decisions = [r.result for r in self.rules] + [i.severity for i in self.issues]
        expected = 'reject' if 'reject' in decisions else 'needs_review' if 'review' in decisions else 'pass'
        if self.decision != expected:
            raise ValueError('audit decision must reflect rules and issues')
        for rule in self.rules:
            if rule.result != 'pass' and not any(i.rule_id == rule.rule_id and i.severity == rule.result for i in self.issues):
                raise ValueError('nonpassing rule requires an actionable issue')
        return self


class MediaInspection(Contract):
    schema_version: Literal['1'] = '1'
    story_id: StoryId
    final_sha256: Sha256
    checks: dict[str, bool]
    issues: list[AuditIssue]
    transcript: TranscriptArtifact | None
    transcription: dict = Field(default_factory=dict)
    captions_text: str
    word_disagreements: list[dict] = Field(default_factory=list)


class ItemAudit(Contract):
    item_id: StoryId
    kind: Literal['story', 'sequence']
    target_sha256: Sha256
    semantic: SemanticAudit
    deterministic_issues: list[AuditIssue]

    @property
    def decision(self):
        if self.semantic.decision == 'reject' or any(i.severity == 'reject' for i in self.deterministic_issues):
            return 'reject'
        if self.semantic.decision == 'needs_review' or self.deterministic_issues:
            return 'needs_review'
        return 'pass'


class AuditBundle(Contract):
    schema_version: Literal['1'] = '1'
    binding: dict[str, Sha256]
    stories: list[ItemAudit]
    sequences: list[ItemAudit]
    approved: Literal[False] = False
    audit_scope: Literal['source_fidelity_and_relevance_not_external_truth'] = 'source_fidelity_and_relevance_not_external_truth'


class IssueResolution(Contract):
    issue_id: Text
    evidence: str = Field(min_length=20)


class ItemReview(Contract):
    item_id: StoryId
    kind: Literal['story', 'sequence']
    target_sha256: Sha256
    decision: Literal['approve', 'reject', 'pending']
    watched_entire_item: bool
    resolutions: list[IssueResolution]
    notes: str


class HumanReview(Contract):
    schema_version: Literal['1'] = '1'
    run_id: Text
    audit_sha256: Sha256
    reviewer_kind: Literal['human']
    reviewer: Text
    reviewed_at: AwareDatetime
    items: list[ItemReview]
