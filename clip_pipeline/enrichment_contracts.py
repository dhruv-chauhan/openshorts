"""Versioned knowledge and editorial associations, independent of media contracts."""
from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Id = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$')]
Day = Annotated[str, StringConstraints(pattern=r'^\d{4}-\d{2}-\d{2}$')]
Hash = Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{64}$')]


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, allow_inf_nan=False, validate_default=True)


class Sourced(Record):
    source_ids: list[Id] = Field(min_length=1)
    verified_on: Day
    valid_from: Day
    valid_through: Day
    status: Literal['reviewed', 'conflicted'] = 'reviewed'

    @model_validator(mode='after')
    def dates(self):
        start, end, verified = map(date.fromisoformat, (self.valid_from, self.valid_through, self.verified_on))
        if start > end or end > verified:
            raise ValueError('applicability must be ordered and cannot extend beyond verification')
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError('duplicate sources')
        return self


class KnowledgeSource(Record):
    source_id: Id
    url: Text
    summary: Text
    verified_on: Day

    @field_validator('url')
    @classmethod
    def public_reference(cls, value):
        parts = urlsplit(value)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('citation must be a credential-free HTTPS reference')
        return value

    @field_validator('verified_on')
    @classmethod
    def day(cls, value):
        date.fromisoformat(value)
        return value


class Category(Record):
    category_id: Id
    dimension: Literal['sector', 'business_role', 'end_market', 'geography', 'macro_sensitivity']
    definition: Text
    includes: list[Text] = Field(min_length=1)
    excludes: list[Text] = Field(min_length=1)
    parent_id: Id | None = None


class Membership(Sourced):
    record_id: Id
    category_id: Id
    exposure: Text
    scale: Text | None = None  # Defined qualitative scope, never a model-generated numeric weight.


class EntityProfile(Record):
    entity_id: Id
    name: Text
    aliases: list[Text]
    market_ids: list[Id]
    memberships: list[Membership]


class Relationship(Sourced):
    record_id: Id
    from_entity: Id
    to_entity: Id
    kind: Literal['supplier', 'customer', 'competitor', 'investor', 'owner']
    explanation: Text


class KnowledgeSnapshot(Record):
    schema_version: Literal['1'] = '1'
    version: Text
    categories: list[Category] = Field(max_length=500)
    entities: list[EntityProfile] = Field(max_length=1000)
    relationships: list[Relationship] = Field(default_factory=list, max_length=2000)
    sources: list[KnowledgeSource] = Field(max_length=2000)

    @model_validator(mode='after')
    def references(self):
        def index(items, key):
            result = {getattr(item, key): item for item in items}
            if len(result) != len(items):
                raise ValueError('duplicate knowledge IDs')
            return result
        cats = index(self.categories, 'category_id')
        entities = index(self.entities, 'entity_id')
        sources = index(self.sources, 'source_id')
        records = [m for e in self.entities for m in e.memberships] + self.relationships
        index(records, 'record_id')
        markets = [m for e in self.entities for m in e.market_ids]
        if len(markets) != len(set(markets)):
            raise ValueError('a market must resolve to exactly one entity')
        for cat in self.categories:
            seen = {cat.category_id}; parent = cat.parent_id
            while parent is not None:
                if parent not in cats or parent in seen:
                    raise ValueError('unknown parent or taxonomy cycle')
                seen.add(parent); parent = cats[parent].parent_id
        for entity in self.entities:
            if len({m.category_id for m in entity.memberships}) != len(entity.memberships):
                raise ValueError('duplicate entity category')
            if any(m.category_id not in cats for m in entity.memberships):
                raise ValueError('unknown category')
        for relation in self.relationships:
            if relation.from_entity not in entities or relation.to_entity not in entities or relation.from_entity == relation.to_entity:
                raise ValueError('invalid relationship endpoints')
        for record in records:
            if not set(record.source_ids).issubset(sources):
                raise ValueError('unknown evidence source')
            if any(record.verified_on > sources[s].verified_on for s in record.source_ids):
                raise ValueError('record verification exceeds source verification')
        return self


class SubjectMatch(Record):
    entity_id: Id
    evidence_segment_ids: list[Text] = Field(min_length=1)


class CategoryMatch(Record):
    category_id: Id
    mechanism: Text
    evidence_segment_ids: list[Text] = Field(min_length=1)


class DiscussionClassification(Record):
    subjects: list[SubjectMatch]
    categories: list[CategoryMatch]
    event_date: Day | None
    time_sensitive: bool
    explanation: Text

    @field_validator('event_date')
    @classmethod
    def day(cls, value):
        if value is not None:
            date.fromisoformat(value)
        return value


class Association(Record):
    candidate_id: Id
    market_id: Id
    basis: Literal['direct', 'category_exposure', 'relationship']
    decision: Literal['supported', 'needs_review', 'unsupported']
    mechanism: Text
    materiality: Text
    exposure_direction: Literal['positive', 'negative', 'mixed', 'unclear']
    evidence_segment_ids: list[Text] = Field(min_length=1)
    knowledge_record_ids: list[Id]
    reason: Text


class AssociationResponse(Record):
    associations: list[Association]


class StoryEnrichment(Record):
    schema_version: Literal['1'] = '1'
    knowledge_sha256: Hash
    assessed_on: Day
    classification: DiscussionClassification
    associations: list[Association]
    coverage_gaps: list[Text]
    review_reasons: list[Text]
    # True means all candidates were assessed, not that all are supported.
    coverage_complete: bool
