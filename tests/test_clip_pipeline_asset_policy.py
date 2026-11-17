"""Policy isolation, enabled-asset admission, focused audit and honest promotion."""
from datetime import datetime, timezone
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from clip_pipeline.artifacts import digest, json_bytes, read_json_model, ArtifactError
from clip_pipeline.config import PipelineConfig, load_inputs
from clip_pipeline.asset_contracts import AssetPolicy, RelationCatalog, RelationChoice, AutomatedDecision
from clip_pipeline.asset_selection import resolve_choices, select
from clip_pipeline.asset_approval import apply_decision, decision_inputs
from clip_pipeline.asset_audit import release_sequences
from clip_pipeline.audit_contracts import AuditBundle
from clip_pipeline.contracts import StoryPlan, StorySpec, TranscriptArtifact
from clip_pipeline.runner import prepare_run, inspect_run
from clip_pipeline.execution import execute_stories
from clip_pipeline.audit_runner import prepare_audit, execute_audit
from clip_pipeline.storage import promote
from test_clip_pipeline_config import config_data, config_path  # noqa: F401
from test_clip_pipeline_media import media_case  # noqa: F401
from test_clip_pipeline_stories import story_case, fake_worker  # noqa: F401
from test_clip_pipeline_audit import media_worker
from test_clip_pipeline_storage import Sink


def policy():
    return AssetPolicy(catalog=RelationCatalog(version='synthetic-v1', definitions=[]))


def test_legacy_serialization_unchanged(config_data):
    c = PipelineConfig.model_validate(config_data)
    assert 'asset_policy' not in c.model_dump()
    s = StorySpec(story_id='one', title='Example', span={'start': 0.0, 'end': 2.0})
    assert 'asset_relations' not in s.model_dump()
    with pytest.raises(ValidationError):
        AutomatedDecision.model_validate({'reviewer_kind': 'human', 'watched_entire_item': True})


def test_direct_mapping_and_unmapped_company(media_case):
    c, _, _, transcript = media_case; _, markets = load_inputs(c)
    segments = [transcript.segments[0].model_copy(update={'text': 'Example Corporation fell today.'})]
    choice = RelationChoice(relation_id='direct:xyz:EXAMPLE', evidence_segment_ids=['s0'])
    assert resolve_choices([choice], segments, markets, policy().catalog)[0].market_ids == ['xyz:EXAMPLE']
    with pytest.raises(ValueError, match='spoken identity'):
        resolve_choices([choice], [segments[0].model_copy(update={'text': 'Skydance reported Q3 metrics.'})], markets, policy().catalog)
    disabled = markets.model_copy(update={'markets': [m.model_copy(update={'enabled': False}) for m in markets.markets]})
    with pytest.raises(ValueError): resolve_choices([choice], segments, disabled, policy().catalog)
    with pytest.raises(ValueError): resolve_choices([choice.model_copy(update={'evidence_segment_ids':['invented']})], segments, markets, policy().catalog)


def test_no_match_has_no_render_plan(media_case):
    c, _, source, transcript = media_case
    c = c.model_copy(update={'asset_policy': policy()}); topic, markets = load_inputs(c)
    calls = []
    def call(stage, system, payload, model, validate):
        calls.append(stage); value = model(sequences=[]); validate(value); return value
    plan, report = select(c, source, transcript, topic, markets, call)
    assert report['outcome'] == 'completed_no_matches' and not plan.sequences
    assert calls == ['selection']


@pytest.fixture
def asset_case(story_case, monkeypatch):
    config, config_file, plan_file, plan, source, transcript = story_case
    c = PipelineConfig.model_validate(config.model_dump() | {'asset_policy': policy().model_dump(),
        'blob': {'account_url': 'https://example.blob.core.windows.net', 'runs_container': 'runs', 'approved_container': 'approved'}})
    d = transcript.model_dump()
    for s in d['segments']:
        words = ['Example', 'Corporation', 'rose.']; width = (s['end'] - s['start']) / len(words)
        s['text'] = ' '.join(words)
        s['words'] = [{'word': w, 'start': s['start'] + i*width, 'end': s['start']+(i+1)*width} for i,w in enumerate(words)]
    transcript = TranscriptArtifact.model_validate(d)
    _, markets = load_inputs(c); stories = []
    for i,s in enumerate(plan.sequences[0].stories):
        relations = resolve_choices([RelationChoice(relation_id='direct:xyz:EXAMPLE', evidence_segment_ids=[f's{i}'])],
                                     transcript.segments, markets, c.asset_policy.catalog)
        stories.append(s.model_copy(update={'matched_market_ids': ['xyz:EXAMPLE'], 'asset_relations': relations, 'evidence': []}))
    plan = plan.model_copy(update={'transcript_sha256': digest(transcript),
        'sequences': [plan.sequences[0].model_copy(update={'stories': stories})]})
    config_file.write_bytes(json_bytes(c)); plan_file.write_bytes(json_bytes(plan))
    case = (c, config_file, plan_file, plan, source, transcript)
    fake_worker(case, monkeypatch)
    render,_ = prepare_run(config_file, story_plan_path=plan_file)
    execute_stories(Path(c.workspace), render.run_id)
    manifest, directory = prepare_audit(config_file, Path(c.workspace), render.run_id)
    return Path(c.workspace), manifest, directory, plan, transcript


class FocusedBackend:
    calls = 0
    reject_second = False
    def __init__(self, config, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def generate(self, *, stage, system, payload, response_model, validate):
        type(self).calls += 1
        blockers = []
        if self.reject_second and payload['metadata']['story_id'] == 'earnings':
            blockers = [{'code':'wrong_number','explanation':'Synthetic material number corruption.',
                'source_segment_ids':['s1'],'final_segment_ids':['s0']}]
        value = response_model(independently_understandable=True, blockers=blockers, diagnostics=[])
        validate(value); return value


@pytest.mark.parametrize('partial', [False, True])
def test_focused_audit_salvage_and_automated_commit(asset_case, tmp_path, partial):
    ws,m,d,plan,_ = asset_case; FocusedBackend.calls = 0; FocusedBackend.reject_second = partial
    m = execute_audit(ws, m.run_id, backend_factory=FocusedBackend, worker=media_worker(asset_case, []))
    assert FocusedBackend.calls == 2  # no additional sequence or exposure calls
    bundle = read_json_model(d/m.artifacts['audit_bundle'].path, AuditBundle)
    release = release_sequences(plan, bundle.stories)
    assert len(release.sequences) == 1
    assert len(release.sequences[0].stories) == (1 if partial else 2)
    assert release.sequences[0].sequence_id.startswith('salvage-') == partial
    assert 'human_review' not in m.artifacts and 'review_template' not in m.artifacts
    decision = AutomatedDecision(**decision_inputs(m,bundle), decision_id='decision-one', actor='content-api:test',
        decided_at=datetime.now(timezone.utc), approved_sequence_ids=[q.sequence_id for q in release.sequences])
    p = tmp_path/'decision.json'; p.write_bytes(json_bytes(decision))
    apply_decision(ws,m.run_id,p)
    sink = Sink(); result = promote(ws,m.run_id,sink_factory=sink)
    commit = json.loads(sink.objects[sink.writes[-1]])
    assert result.stage == 'stored' and commit['schema_version'] == '2'
    assert 'decision' in commit and 'review' not in commit
    assert sink.writes[-1][1].endswith('/commit.json')
    assert 'human_review' not in inspect_run(ws,m.run_id).artifacts


def test_curated_groups_people_and_disabled_members(media_case):
    from clip_pipeline.asset_contracts import RelationDefinition
    from clip_pipeline.asset_selection import candidates
    c,_,_,transcript=media_case;_,markets=load_inputs(c)
    defs=[RelationDefinition(relation_id='group:example',kind='group',name='Example group',aliases=[],
        market_ids=['xyz:EXAMPLE','xyz:MISSING'],description='Synthetic explicitly named group.',source_urls=['https://example.com/group']),
        RelationDefinition(relation_id='person:example',kind='person',name='Pat Example',aliases=[],
        market_ids=['xyz:EXAMPLE'],description='Synthetic named founder.',source_urls=['https://example.com/founder'])]
    catalog=RelationCatalog(version='synthetic',definitions=defs)
    assert candidates(markets,catalog)['group:example']['market_ids']==['xyz:EXAMPLE']
    for rid,text in [('group:example','Example group gained today.'),('person:example','Pat Example described the business.')]:
        segment=transcript.segments[0].model_copy(update={'text':text})
        relation=resolve_choices([RelationChoice(relation_id=rid,evidence_segment_ids=[segment.segment_id])],[segment],markets,catalog)
        assert relation[0].market_ids==['xyz:EXAMPLE']
    with pytest.raises(ValueError):
        resolve_choices([RelationChoice(relation_id='group:example',evidence_segment_ids=['s0'])],
            [transcript.segments[0].model_copy(update={'text':'Skydance reported earnings.'})],markets,catalog)


def test_nonindependent_survivor_is_not_salvaged(asset_case):
    ws,m,d,plan,_=asset_case;FocusedBackend.reject_second=True
    m=execute_audit(ws,m.run_id,backend_factory=FocusedBackend,worker=media_worker(asset_case,[]))
    bundle=read_json_model(d/m.artifacts['audit_bundle'].path,AuditBundle)
    results=[i.model_copy(update={'independently_understandable':False}) for i in bundle.stories]
    assert not release_sequences(plan,results).sequences


def test_automated_decision_cannot_change_hash_or_release_set(asset_case,tmp_path):
    ws,m,d,plan,_=asset_case;FocusedBackend.reject_second=False
    m=execute_audit(ws,m.run_id,backend_factory=FocusedBackend,worker=media_worker(asset_case,[]))
    bundle=read_json_model(d/m.artifacts['audit_bundle'].path,AuditBundle)
    value=dict(**decision_inputs(m,bundle),decision_id='one',actor='service',decided_at=datetime.now(timezone.utc),
               approved_sequence_ids=[q.sequence_id for q in plan.sequences])
    for patch in [{'audit_sha256':'f'*64},{'approved_sequence_ids':[]},{'targets':{}}]:
        p=tmp_path/'bad.json';p.write_bytes(json_bytes(AutomatedDecision(**(value|patch))))
        with pytest.raises(ArtifactError):apply_decision(ws,m.run_id,p)
    assert inspect_run(ws,m.run_id).stage=='audited'


def test_performance_observation_selects_without_thesis(asset_case):
    ws,m,d,plan,transcript=asset_case
    config=read_json_model(d/m.artifacts['config'].path,PipelineConfig)
    from clip_pipeline.contracts import SourceArtifact
    source=read_json_model(d/m.artifacts['render.source'].path,SourceArtifact)
    topic,markets=load_inputs(config)
    def call(stage,system,payload,model,validate):
        value=model.model_validate({'sequences':[{'title':'Performance observation','stories':[{
          'title':'Example Corporation rose','first_segment_id':'s0','last_segment_id':'s0',
          'relations':[{'relation_id':'direct:xyz:EXAMPLE','evidence_segment_ids':['s0']}]}]}]})
        validate(value);return value
    selected,report=select(config,source,transcript,topic,markets,call)
    assert selected.sequences[0].stories[0].matched_market_ids==['xyz:EXAMPLE']
    assert selected.sequences[0].stories[0].stance=='unclear'
    assert report['outcome']=='selected'


def test_bad_optional_relation_does_not_poison_grounded_story(asset_case):
    from clip_pipeline.asset_contracts import RelationDefinition
    from clip_pipeline.asset_audit import relation_issues
    ws,m,d,_,transcript=asset_case
    c=read_json_model(d/m.artifacts['config'].path,PipelineConfig)
    from clip_pipeline.contracts import SourceArtifact
    source=read_json_model(d/m.artifacts['render.source'].path,SourceArtifact)
    catalog=RelationCatalog(version='test',definitions=[RelationDefinition(relation_id='group:test',kind='group',name='Test group',aliases=[],market_ids=['xyz:EXAMPLE'],description='Synthetic group.',source_urls=['https://example.com/group'])])
    c=c.model_copy(update={'asset_policy':AssetPolicy(catalog=catalog)})
    topic,markets=load_inputs(c)
    def call(stage,system,payload,model,validate):
        value=model.model_validate({'sequences':[{'title':'Performance observation','stories':[{
          'title':'Example Corporation rose','first_segment_id':'s0','last_segment_id':'s0',
          'relations':[{'relation_id':'direct:xyz:EXAMPLE','evidence_segment_ids':['s0']},
                       {'relation_id':'group:test','evidence_segment_ids':['s1']}]}]}]})
        validate(value);return value
    plan,report=select(c,source,transcript,topic,markets,call)
    story=plan.sequences[0].stories[0]
    assert [r.relation_id for r in story.asset_relations]==['direct:xyz:EXAMPLE']
    assert not relation_issues(story,transcript,markets,c)
    assert report['withheld'][0]['reason']=='relation_not_grounded_in_retained_story'
    assert report['withheld'][0]['relation_id']=='group:test'


def test_invalid_only_relation_still_blocks_story(asset_case):
    ws,m,d,_,transcript=asset_case
    c=read_json_model(d/m.artifacts['config'].path,PipelineConfig)
    from clip_pipeline.contracts import SourceArtifact
    source=read_json_model(d/m.artifacts['render.source'].path,SourceArtifact);topic,markets=load_inputs(c)
    def call(stage,system,payload,model,validate):
        value=model.model_validate({'sequences':[{'title':'Unmapped company','stories':[{
          'title':'Unmapped company','first_segment_id':'s0','last_segment_id':'s0',
          'relations':[{'relation_id':'direct:xyz:UNLISTED','evidence_segment_ids':['s0']}]}]}]})
        validate(value);return value
    plan,report=select(c,source,transcript,topic,markets,call)
    assert not plan.sequences and report['outcome']=='completed_no_matches'
    assert report['withheld'][-1]['reason']=='story_has_no_enabled_asset_relation'


def test_unmapped_candidate_does_not_discard_mapped_stories(asset_case):
    ws,m,d,_,transcript=asset_case
    c=read_json_model(d/m.artifacts['config'].path,PipelineConfig)
    from clip_pipeline.contracts import SourceArtifact
    source=read_json_model(d/m.artifacts['render.source'].path,SourceArtifact);topic,markets=load_inputs(c)
    def call(stage,system,payload,model,validate):
        value=model.model_validate({'sequences':[{'title':'Market update','stories':[
          {'title':'Unlisted company','first_segment_id':'s0','last_segment_id':'s0',
           'relations':[{'relation_id':'direct:xyz:UNLISTED','evidence_segment_ids':['s0']}]},
          {'title':'Example Corporation rose','first_segment_id':'s1','last_segment_id':'s1',
           'relations':[{'relation_id':'direct:xyz:EXAMPLE','evidence_segment_ids':['s1']}]}]}]})
        validate(value);return value
    plan,report=select(c,source,transcript,topic,markets,call)
    assert [s.story_id for s in plan.sequences[0].stories]==['a0-q0-s1']
    assert plan.sequences[0].title=='Example Corporation rose'
    assert plan.sequences[0].stories[0].matched_market_ids==['xyz:EXAMPLE']
    assert report['outcome']=='selected'
    assert any(w.get('story_id')=='a0-q0-s0' and w['reason']=='story_has_no_enabled_asset_relation' for w in report['withheld'])
