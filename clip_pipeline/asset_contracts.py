"""Relevance and automated authority contracts, separate from human reviews."""
from typing import Annotated, Literal
from pydantic import AwareDatetime, Field, StringConstraints, model_validator
from .enrichment_contracts import Record, Id, Text

Hash = Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{64}$')]


class RelationDefinition(Record):
    relation_id: Id
    kind: Literal['person', 'company', 'group', 'macro']
    name: Text
    aliases: list[Text] = Field(default_factory=list)
    market_ids: list[Id] = Field(default_factory=list)
    asset_types: list[Literal['equity', 'index', 'commodity', 'fx', 'crypto', 'pre_ipo', 'etf']] = Field(default_factory=list)
    description: Text
    source_urls: list[Text] = Field(min_length=1)


class RelationCatalog(Record):
    version: Text
    definitions: list[RelationDefinition] = Field(default_factory=list, max_length=500)

    @model_validator(mode='after')
    def unique(self):
        if len({r.relation_id for r in self.definitions}) != len(self.definitions):
            raise ValueError('duplicate relation definition')
        for r in self.definitions:
            if not r.market_ids and not r.asset_types:
                raise ValueError('relation needs explicit members or asset types')
            if r.kind in ('person', 'company') and r.asset_types:
                raise ValueError('people and companies cannot expand to an asset class')
            from urllib.parse import urlsplit
            for url in r.source_urls:
                p = urlsplit(url)
                if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
                    raise ValueError('relation provenance needs credential-free HTTPS URLs')
        return self


class AssetPolicy(Record):
    mode: Literal['asset-relevance-v1'] = 'asset-relevance-v1'
    catalog: RelationCatalog
    sample_percent: Annotated[int, Field(ge=0, le=100)] = 10


class AssetRelation(Record):
    relation_id: Id
    kind: Literal['direct', 'person', 'company', 'group', 'macro']
    market_ids: list[Id] = Field(min_length=1)
    evidence_segment_ids: list[Text] = Field(min_length=1)
    catalog_sha256: Hash


class RelationChoice(Record):
    relation_id: Id
    evidence_segment_ids: list[Text] = Field(min_length=1)


class AssetDraft(Record):
    first_segment_id: Text
    last_segment_id: Text
    title: Text
    relations: list[RelationChoice] = Field(min_length=1)


class AssetDraftSequence(Record):
    title: Text
    stories: list[AssetDraft] = Field(min_length=1)


class AssetProposals(Record):
    sequences: list[AssetDraftSequence] = Field(default_factory=list)


class FidelityFinding(Record):
    code: Id
    explanation: Text
    source_segment_ids: list[Text]
    final_segment_ids: list[Text]


class FidelityReport(Record):
    independently_understandable: bool
    blockers: list[FidelityFinding]
    diagnostics: list[FidelityFinding]


class AutomatedDecision(Record):
    schema_version: Literal['1'] = '1'
    decision_kind: Literal['automated_policy'] = 'automated_policy'
    decision_id: Text
    run_id: Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{32}$')]
    audit_sha256: Hash
    policy_sha256: Hash
    config_sha256: Hash
    actor: Text
    decided_at: AwareDatetime
    approved_sequence_ids: list[Id]
    targets: dict[Text, Hash]
