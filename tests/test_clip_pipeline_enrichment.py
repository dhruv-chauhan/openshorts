"""Knowledge evidence, bounded association coverage, compatibility and resume."""
import json
from pathlib import Path
import pytest
from clip_pipeline.artifacts import digest, json_bytes, read_json_model, ArtifactError
from clip_pipeline.config import PipelineConfig, EnrichmentSnapshot
from clip_pipeline.contracts import MarketSnapshot, StoryPlan
from clip_pipeline.enrichment_contracts import KnowledgeSnapshot, DiscussionClassification, AssociationResponse
from clip_pipeline.enrichment import candidates_for, enrich_story, validate_classification
from clip_pipeline.selection import select_stories
from clip_pipeline.selection_contracts import EnrichedDraftResponse, EnrichedRelevanceResponse, DraftResponse, RelevanceResponse
from clip_pipeline.selection_runner import prepare_selection, execute_selection
from clip_pipeline.runner import inspect_run
from clip_pipeline.foundry import FoundryError, strict_schema
from test_clip_pipeline_selection import scenario, model_call
from test_clip_pipeline_config import config_data  # noqa: F401
from test_clip_pipeline_media import media_case  # noqa: F401

DAY = '2026-11-01'


def knowledge_data():
    fact = dict(source_ids=['disclosure'], verified_on=DAY, valid_from='2026-01-01', valid_through=DAY, status='reviewed')
    return dict(version='synthetic-v1', categories=[dict(category_id='cloud', dimension='business_role',
        definition='Supplies compute infrastructure', includes=['Compute usage and prices'], excludes=['General technology talk'])],
        sources=[dict(source_id='disclosure', url='https://example.invalid/disclosure',
                      summary='Synthetic: Example supplies compute; Other supplies competing compute.', verified_on=DAY)],
        entities=[dict(entity_id='private', name='Private Models', aliases=['Private Models'], market_ids=[], memberships=[]),
                  dict(entity_id='supplier', name='Example Corporation', aliases=['Example'], market_ids=['xyz:EXAMPLE'],
                       memberships=[dict(record_id='supplier.cloud', category_id='cloud', exposure='Compute sales', scale=None, **fact)]),
                  dict(entity_id='other', name='Other Corporation', aliases=['Other'], market_ids=['xyz:OTHER'],
                       memberships=[dict(record_id='other.cloud', category_id='cloud', exposure='Competing compute sales', scale=None, **fact)])],
        relationships=[dict(record_id='private.customer.supplier', from_entity='private', to_entity='supplier',
                            kind='customer', explanation='Private Models buys compute from Example.', **fact)])


def inputs(media_case):
    config, source, transcript = scenario(media_case, ['Private Models is expanding compute purchases on 2026-10-29.',
                                                     'More capacity could help suppliers, but efficiency may offset demand.'])
    path=Path(config.workspace)/'test-knowledge.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(knowledge_data()))
    config = PipelineConfig.model_validate(config.model_dump() | {'enrichment': {'mode':'snapshot','path':str(path)}})
    from clip_pipeline.config import load_inputs
    policy, markets = load_inputs(config)
    markets = MarketSnapshot.model_validate(markets.model_dump() | {'markets': markets.model_dump()['markets'] + [
        dict(market_id='xyz:OTHER', underlying_name='Other Corporation', aliases=['Other'], asset_type='equity', enabled=True)]})
    return config, source, transcript, policy, markets, KnowledgeSnapshot.model_validate(knowledge_data())


def classification():
    return DiscussionClassification(subjects=[dict(entity_id='private', evidence_segment_ids=['s0'])],
        categories=[dict(category_id='cloud', mechanism='Compute purchases affect demand.', evidence_segment_ids=['s0','s1'])],
        event_date='2026-10-29', time_sensitive=True, explanation='Synthetic event.')


def evaluator(transcript, policy, *, path='market_exposure', decision='supported', classification_override=None, mutate=None):
    original, calls = model_call(transcript, policy, path='equity_macro')
    def call(stage, system, payload, model, validate):
        if model is DiscussionClassification:
            result = classification_override or classification()
        elif model is AssociationResponse:
            data = {'associations':[c | dict(decision=decision, mechanism='Compute demand affects supplier utilization.',
                materiality='The source describes expansion of the core supplied service.', exposure_direction='unclear',
                evidence_segment_ids=['s0','s1'], reason='Source and knowledge checked.') for c in payload['candidates']]}
            if mutate: mutate(data)
            result = model.model_validate(data)
        elif model in (EnrichedRelevanceResponse, EnrichedDraftResponse):
            base_model = RelevanceResponse if model is EnrichedRelevanceResponse else DraftResponse
            data = original(stage, system, payload, base_model, lambda _:None).model_dump()
            for item in data.get('windows', []): item['topic_path'] = path
            for seq in data.get('sequences', []):
                for story in seq['stories']: story['topic_path'] = path
            result = model.model_validate(data)
        else:
            return original(stage, system, payload, model, validate)
        calls.append(model.__name__)
        validate(result)
        return result
    return call, calls


def test_multiple_inferred_markets_without_spoken_ids(media_case):
    config,source,transcript,policy,markets,k = inputs(media_case)
    call,_ = evaluator(transcript,policy)
    plan,report = select_stories(config,source,transcript,policy,markets,call,knowledge=k,assessed_on=DAY)
    story = plan.sequences[0].stories[0]
    assert report['outcome']=='selected' and story.matched_market_ids==[] and story.stance=='unclear'
    assert {a.market_id for a in story.enrichment.associations if a.decision=='supported'}=={'xyz:EXAMPLE','xyz:OTHER'}
    assert plan.selection_binding['enrichment']==digest(k)
    assert StoryPlan.model_validate_json(json_bytes(plan))==plan


@pytest.mark.parametrize('mutation', ['cycle','missing_source','duplicate_market','unknown_category','bad_dates','unknown_endpoint'])
def test_invalid_knowledge_is_not_silently_accepted(mutation):
    data=knowledge_data()
    if mutation=='cycle':data['categories'][0]['parent_id']='cloud'
    if mutation=='missing_source':data['sources']=[]
    if mutation=='duplicate_market':data['entities'][0]['market_ids']=['xyz:EXAMPLE']
    if mutation=='unknown_category':data['entities'][1]['memberships'][0]['category_id']='unknown'
    if mutation=='bad_dates':data['relationships'][0]['valid_from']='2026-02-30'
    if mutation=='unknown_endpoint':data['relationships'][0]['to_entity']='invented'
    with pytest.raises(ValueError):KnowledgeSnapshot.model_validate(data)


@pytest.mark.parametrize('mode', ['stale','future_event','old_event','unknown_date','conflict','future_verification'])
def test_temporal_checks_cannot_be_overruled_by_model(media_case,mode):
    config,source,transcript,policy,markets,k=inputs(media_case)
    data=k.model_dump(); cls=classification(); assessed=DAY
    if mode=='stale':assessed='2028-01-01'
    if mode=='future_event':cls=cls.model_copy(update={'event_date':'2026-12-01'})
    if mode=='old_event':cls=cls.model_copy(update={'event_date':'2025-10-29'})
    if mode=='unknown_date':cls=cls.model_copy(update={'event_date':None})
    if mode=='future_verification':assessed='2026-10-30'
    if mode=='conflict':
        for e in data['entities']:
            for m in e['memberships']:m['status']='conflicted'
        data['relationships'][0]['status']='conflicted'
    k=KnowledgeSnapshot.model_validate(data)
    call,_=evaluator(transcript,policy,classification_override=cls)
    plan,report=select_stories(config,source,transcript,policy,markets,call,knowledge=k,assessed_on=assessed)
    assert not plan.sequences and report['outcome']=='needs_review'
    associations=report['proposals'][0]['sequences'][0]['stories'][0]['enrichment']['associations']
    assert all(a['decision']=='needs_review' for a in associations)


@pytest.mark.parametrize('decision', ['unsupported','needs_review'])
def test_optional_links_do_not_poison_macro_eligibility(media_case,decision):
    config,source,transcript,policy,markets,k=inputs(media_case)
    call,_=evaluator(transcript,policy,path='equity_macro',decision=decision)
    plan,report=select_stories(config,source,transcript,policy,markets,call,knowledge=k,assessed_on=DAY)
    assert plan.sequences and report['outcome']=='selected'
    assert all(a.decision==decision for a in plan.sequences[0].stories[0].enrichment.associations)


def test_category_membership_alone_does_not_qualify_story(media_case):
    config,source,transcript,policy,markets,k=inputs(media_case)
    call,_=evaluator(transcript,policy,decision='unsupported')
    plan,report=select_stories(config,source,transcript,policy,markets,call,knowledge=k,assessed_on=DAY)
    assert not plan.sequences and report['outcome']=='completed_no_matches'
    assert report['withheld'][0]['reason']=='no_supported_exposure'


def test_fanout_exhaustion_is_review_not_truncated_success(media_case):
    config,source,transcript,policy,markets,k=inputs(media_case)
    config=config.model_copy(update={'enrichment':config.enrichment.model_copy(update={'max_candidates_per_story':1})})
    call,calls=evaluator(transcript,policy)
    plan,report=select_stories(config,source,transcript,policy,markets,call,knowledge=k,assessed_on=DAY)
    assert not plan.sequences and report['outcome']=='needs_review' and 'AssociationResponse' not in calls


def test_one_hop_disabled_and_unknown_markets(media_case):
    config,source,transcript,policy,markets,k=inputs(media_case)
    data=k.model_dump(); edge=data['relationships'][0].copy()
    edge.update(record_id='supplier.customer.other',from_entity='supplier',to_entity='other');data['relationships'].append(edge)
    data['entities'][1]['market_ids'].append('xyz:UNKNOWN');k=KnowledgeSnapshot.model_validate(data)
    cls=classification().model_copy(update={'categories':[]})
    from clip_pipeline.contracts import StorySpec
    story=StorySpec(story_id='s',title='t',span={'start':0.0,'end':18.0})
    candidates=candidates_for(cls,story,k,markets)
    assert [c['market_id'] for c in candidates]==['xyz:EXAMPLE']  # not OTHER via two edges
    markets=markets.model_copy(update={'markets':[m.model_copy(update={'enabled':False}) for m in markets.markets]})
    assert candidates_for(cls,story,k,markets)==[]


@pytest.mark.parametrize('mutation', ['unknown_candidate','fake_market','fake_record','missing_candidate','outside_evidence'])
def test_association_response_cannot_invent_links(media_case,mutation):
    config,source,transcript,policy,markets,k=inputs(media_case)
    def mutate(data):
        a=data['associations'][0]
        if mutation=='unknown_candidate':a['candidate_id']='unknown'
        if mutation=='fake_market':a['market_id']='xyz:UNKNOWN'
        if mutation=='fake_record':a['knowledge_record_ids']=['invented']
        if mutation=='missing_candidate':data['associations'].pop()
        if mutation=='outside_evidence':a['evidence_segment_ids']=['outside']
    call,_=evaluator(transcript,policy,mutate=mutate)
    with pytest.raises(ValueError):select_stories(config,source,transcript,policy,markets,call,knowledge=k,assessed_on=DAY)


def test_unknown_subject_or_category_cannot_create_knowledge(media_case):
    _,_,transcript,_,_,k=inputs(media_case)
    cls=classification().model_dump();cls['subjects'][0]['entity_id']='supplier'
    with pytest.raises(ValueError,match='spoken identity'):
        validate_classification(DiscussionClassification.model_validate(cls),transcript.segments,k)
    cls=classification().model_dump();cls['categories'][0]['category_id']='invented'
    with pytest.raises(ValueError,match='unknown category'):
        validate_classification(DiscussionClassification.model_validate(cls),transcript.segments,k)


def test_missing_profiles_preserve_macro_and_report_gaps(media_case):
    config,source,transcript,policy,markets,k=inputs(media_case)
    k=KnowledgeSnapshot(version='empty',categories=[],entities=[],sources=[])
    cls=DiscussionClassification(subjects=[],categories=[],event_date=None,time_sensitive=False,explanation='Macro only.')
    call,_=evaluator(transcript,policy,path='equity_macro',classification_override=cls)
    plan,report=select_stories(config,source,transcript,policy,markets,call,knowledge=k,assessed_on=DAY)
    assert report['outcome']=='selected'
    assert 'missing_market_profile:xyz:EXAMPLE' in plan.sequences[0].stories[0].enrichment.coverage_gaps


def test_durable_enrichment_resume_and_input_changes(media_case,tmp_path,monkeypatch):
    from datetime import datetime, timezone
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026,11,1,tzinfo=timezone.utc)
    monkeypatch.setattr('clip_pipeline.selection_runner.datetime', FixedDateTime)
    config,source,transcript,policy,markets,k=inputs(media_case)
    kp=tmp_path/'knowledge.json';kp.write_bytes(json_bytes(k))
    config=config.model_copy(update={'enrichment':config.enrichment.model_copy(update={'path':str(kp)})})
    paths=[]
    for name,obj in [('config',config),('source',source),('transcript',transcript)]:
        p=tmp_path/(name+'.json');p.write_bytes(json_bytes(obj));paths.append(p)
    manifest,directory=prepare_selection(*paths)
    original,calls=evaluator(transcript,policy);failed=[False]
    class Backend:
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def generate(self,*,stage,system,payload,response_model,validate):
            if response_model is AssociationResponse and not failed[0]:
                failed[0]=True;raise FoundryError('synthetic_interruption')
            return original(stage,system,payload,response_model,validate)
    with pytest.raises(FoundryError):execute_selection(directory.parent.parent,manifest.run_id,backend_factory=Backend)
    changed=k.model_copy(update={'version':'changed'});kp.write_bytes(json_bytes(changed))
    result=execute_selection(directory.parent.parent,manifest.run_id,backend_factory=Backend)
    assert calls.count('DiscussionClassification')==1
    plan=read_json_model(directory/result.artifacts['story_plan'].path,StoryPlan)
    assert plan.selection_binding['enrichment']==digest(k)
    new,new_dir=prepare_selection(*paths)
    assert new.artifacts['enrichment'].sha256 != manifest.artifacts['enrichment'].sha256
    assert new.artifacts['transcript'].sha256 == manifest.artifacts['transcript'].sha256
    assert inspect_run(directory.parent.parent,manifest.run_id).stage=='selected'
    (directory/result.artifacts['enrichment'].path).write_bytes(json_bytes(changed))
    with pytest.raises(ArtifactError):inspect_run(directory.parent.parent,manifest.run_id)


def test_new_structured_models_are_foundry_compatible():
    for model in (EnrichedDraftResponse,EnrichedRelevanceResponse,DiscussionClassification,AssociationResponse):
        schema=strict_schema(model)
        assert schema['additionalProperties'] is False


def test_off_and_legacy_stories_preserve_hashes(media_case):
    config,source,transcript,policy,markets,k=inputs(media_case)
    old=config.model_dump();old.pop('enrichment')
    assert PipelineConfig.model_validate(old).model_dump()==old
    from clip_pipeline.contracts import StorySpec
    story=StorySpec(story_id='s',title='t',span={'start':0.0,'end':18.0})
    assert 'enrichment' not in story.model_dump()


def test_renderer_pins_knowledge_and_preserves_story_metadata(media_case,tmp_path,monkeypatch):
    from clip_pipeline.runner import prepare_run
    from clip_pipeline.execution import execute_stories
    from clip_pipeline.contracts import SequenceManifest
    from test_clip_pipeline_stories import fake_worker
    config,source,transcript,policy,markets,k=inputs(media_case)
    call,_=evaluator(transcript,policy)
    plan,_=select_stories(config,source,transcript,policy,markets,call,knowledge=k,assessed_on=DAY)
    cp,pp=tmp_path/'render-config.json',tmp_path/'plan.json'
    cp.write_bytes(json_bytes(config));pp.write_bytes(json_bytes(plan))
    fake_worker((config,cp,pp,plan,source,transcript),monkeypatch)
    m,d=prepare_run(cp,story_plan_path=pp)
    assert 'enrichment' in m.input_artifact_keys
    result=execute_stories(d.parent.parent,m.run_id)
    key='sequence.'+plan.sequences[0].sequence_id
    output=read_json_model(d/result.artifacts[key].path,SequenceManifest)
    assert output.stories[0].story.enrichment==plan.sequences[0].stories[0].enrichment
    assert not output.approved and not output.audit_performed
    Path(config.enrichment.path).write_bytes(json_bytes(k.model_copy(update={'version':'changed'})))
    with pytest.raises(ArtifactError,match='exact enrichment'):prepare_run(cp,story_plan_path=pp)


def test_preview_escapes_knowledge_and_labels_inference(media_case):
    from clip_pipeline.report import selection_preview
    config,source,transcript,policy,markets,k=inputs(media_case)
    call,_=evaluator(transcript,policy)
    plan,report=select_stories(config,source,transcript,policy,markets,call,knowledge=k,assessed_on=DAY)
    report['knowledge']['sources'][0]['summary']='<script>unsafe</script>'
    html=selection_preview(plan,report,transcript)
    assert '<script>unsafe</script>' not in html and '&lt;script&gt;' in html
    assert 'not speaker claims' in html and 'https://example.invalid/disclosure' in html


def test_full_date_required_and_not_inferred_from_publication():
    from clip_pipeline.enrichment import date_is_spoken
    assert date_is_spoken('2026-10-29','On October 29, 2026 the company expanded.')
    assert date_is_spoken('2026-10-29','Reported on 2026-10-29.')
    assert not date_is_spoken('2026-10-29','In the third quarter of 2026.')


def test_shared_knowledge_overflow_reports_unevaluated_coverage(media_case):
    config,source,transcript,policy,markets,k=inputs(media_case)
    config=config.model_copy(update={'selection':config.selection.model_copy(update={'context_budget_tokens':5000})})
    def no_call(*args):raise AssertionError('Cannot send a truncated knowledge/window payload')
    plan,report=select_stories(config,source,transcript,policy,markets,no_call,knowledge=k,assessed_on=DAY)
    assert not plan.sequences and report['outcome']=='needs_review' and not report['coverage_complete']
    assert all('not_evaluated' in w['reason'] for w in report['windows'])


def test_failed_exposure_does_not_hide_source_policy_uncertainty(media_case):
    from clip_pipeline.selection_contracts import SelectionReview
    config,source,transcript,policy,markets,k=inputs(media_case)
    original,_=evaluator(transcript,policy,decision='unsupported')
    def call(stage,system,payload,model,validate):
        result=original(stage,system,payload,model,lambda _:None)
        if model is SelectionReview:
            data=result.model_dump()
            data['sequences'][0]['rules'][0]['result']='review'
            result=model.model_validate(data)
        validate(result)
        return result
    plan,report=select_stories(config,source,transcript,policy,markets,call,knowledge=k,assessed_on=DAY)
    assert not plan.sequences and report['outcome']=='needs_review'
