"""Final media evidence, independent audits, review binding and durable recovery."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from clip_pipeline.artifacts import ArtifactError, digest, json_bytes, read_json_model
from clip_pipeline.audit import audit_item, validate_semantic
from clip_pipeline.audit_contracts import AuditBundle, AuditIssue, HumanReview, MediaInspection, SemanticAudit
from clip_pipeline.audit_runner import prepare_audit, execute_audit
from clip_pipeline.contracts import StoryArtifact, TranscriptArtifact
from clip_pipeline.execution import execute_stories
from clip_pipeline.foundry import FoundryError
from clip_pipeline.review import apply_review, review_template, validate_review
from clip_pipeline.runner import prepare_run, inspect_run
from test_clip_pipeline_config import config_data, config_path  # noqa: F401
from test_clip_pipeline_media import media_case  # noqa: F401
from test_clip_pipeline_stories import story_case, fake_worker  # noqa: F401


@pytest.fixture
def audit_case(story_case, monkeypatch):
    config, config_file, plan_file, plan, source, transcript = story_case
    data=config.model_dump(); data['blob']={'account_url':'https://example.blob.core.windows.net','runs_container':'runs','approved_container':'approved'}
    config_file.write_bytes(json_bytes(data))
    fake_worker(story_case,monkeypatch)
    render,_=prepare_run(config_file,story_plan_path=plan_file)
    execute_stories(Path(config.workspace),render.run_id)
    manifest,directory=prepare_audit(config_file,Path(config.workspace),render.run_id)
    return Path(config.workspace),manifest,directory,plan,transcript


class Backend:
    calls=[]
    fail_at=None
    def __init__(self,config,**kwargs):pass
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def generate(self,*,stage,system,payload,response_model,validate):
        self.calls.append(payload)
        if len(self.calls)==self.fail_at:raise RuntimeError('injected provider failure')
        value=response_model.model_validate(dict(decision='pass',rules=[{'rule_id':r['rule_id'],'result':'pass','reason':'Supported by final audio.'} for r in payload['policy']['rules']],issues=[],summary='Synthetic audit passed.'))
        validate(value);return value


def media_worker(case,calls,*,bad_hash=False,missing=False):
    def worker(action,directory,config_path,config,**inputs):
        target=read_json_model(inputs['request'],StoryArtifact);calls.append(target.story.story_id)
        assert action=='audit-story' and 'supplied' not in inputs
        transcript=case[4].model_copy(update={'source_sha256':'f'*64 if bad_hash else target.final.media.sha256,
            'duration_seconds':target.final.duration_seconds,'segments':case[4].segments[:1]})
        result=MediaInspection(story_id=target.story.story_id,final_sha256=target.final.media.sha256,
            checks={'format_duration_decode':True,'final_audio_transcribed':not missing},issues=[],
            transcript=None if missing else transcript,captions_text='Rates affect stocks.')
        directory.mkdir(parents=True);path=directory/'inspection.json';path.write_bytes(json_bytes(result))
        return {'inspection':path}
    return worker


def finish(case):
    Backend.calls=[];Backend.fail_at=None
    workspace,m,_,_,_=case
    return execute_audit(workspace,m.run_id,backend_factory=Backend,worker=media_worker(case,[]))


def bundle_for(case,manifest):return read_json_model(case[2]/manifest.artifacts['audit_bundle'].path,AuditBundle)


def human_decisions(manifest,bundle):
    data=review_template(manifest,bundle)
    data.update(reviewer='Synthetic test reviewer',reviewed_at=datetime.now(timezone.utc))
    for item in data['items']:item.update(decision='approve',watched_entire_item=True)
    return HumanReview.model_validate(data)


def test_audit_retry_reuses_media_and_validated_calls(audit_case):
    ws,m,d,plan,_=audit_case;calls=[];Backend.calls=[];Backend.fail_at=2
    with pytest.raises(RuntimeError):execute_audit(ws,m.run_id,backend_factory=Backend,worker=media_worker(audit_case,calls))
    failed=inspect_run(ws,m.run_id)
    assert failed.state=='retryable' and failed.outcome is None and len(calls)==2
    Backend.fail_at=None
    result=execute_audit(ws,m.run_id,backend_factory=Backend,worker=media_worker(audit_case,calls))
    assert result.stage=='audited' and result.state=='checkpointed' and result.outcome is None
    assert len(calls)==2 and len(Backend.calls)==4 # first success + failed request + retry + sequence
    seq=Backend.calls[-1]
    assert seq['kind']=='sequence' and seq['omitted_source_intervals']==[{'start':4.0,'end':5.0}]
    assert all(':' in s['segment_id'] for s in seq['final_audio'])
    assert 'source_context' in seq and 'proposed_metadata_not_evidence' in seq
    assert not bundle_for(audit_case,result).approved
    assert execute_audit(ws,m.run_id,backend_factory=Backend)==result
    html=(d/result.artifacts['preview'].path).read_text()
    assert html.count('<video ')==2


def test_wrong_final_audio_binding_is_operational_failure(audit_case):
    ws,m,*_=audit_case
    with pytest.raises(ArtifactError,match='delivered bytes'):
        execute_audit(ws,m.run_id,backend_factory=Backend,worker=media_worker(audit_case,[],bad_hash=True))
    assert inspect_run(ws,m.run_id).outcome is None


def test_human_review_all_items_watched_and_hash_bound(audit_case,tmp_path):
    m=finish(audit_case);bundle=bundle_for(audit_case,m);review=human_decisions(m,bundle);plan=audit_case[3]
    assert validate_review(m,bundle,review,plan)==['discussion']
    for key,value,pattern in [('watched_entire_item',False,'full visual'),('target_sha256','f'*64,'target changed'),('decision','pending','every planned story')]:
        data=review.model_dump();data['items'][0][key]=value
        with pytest.raises(ArtifactError,match=pattern):validate_review(m,bundle,HumanReview.model_validate(data),plan)
    path=tmp_path/'review.json';path.write_bytes(json_bytes(review))
    updated=apply_review(audit_case[0],m.run_id,path)
    assert updated.stage=='reviewed' and updated.outcome is None
    (audit_case[2]/updated.artifacts['render.story.rates.final_media'].path).write_bytes(b'changed')
    with pytest.raises(ArtifactError,match='checksum'):inspect_run(audit_case[0],m.run_id)


def test_missing_final_audio_cannot_be_human_overridden(audit_case):
    ws,m,*_=audit_case;Backend.calls=[];Backend.fail_at=None
    m=execute_audit(ws,m.run_id,backend_factory=Backend,worker=media_worker(audit_case,[],missing=True))
    bundle=bundle_for(audit_case,m)
    assert all(i.decision=='reject' for i in bundle.stories+bundle.sequences)
    with pytest.raises(ArtifactError,match='cannot be overridden'):
        validate_review(m,bundle,human_decisions(m,bundle),audit_case[3])


def test_review_ambiguity_requires_explicit_evidence(audit_case):
    m=finish(audit_case);bundle=bundle_for(audit_case,m)
    issue=AuditIssue(issue_id='unclear_number',rule_id='material_numbers',severity='review',kind='asr_uncertainty',
        explanation='Indistinct number in the final audio.',source_segment_ids=['s0'],final_segment_ids=['s0'])
    semantic=SemanticAudit(decision='needs_review',rules=[{'rule_id':'material_numbers','result':'review','reason':'Listen to the number.'}],issues=[issue],summary='One ambiguity.')
    bundle=bundle.model_copy(update={'stories':[bundle.stories[0].model_copy(update={'semantic':semantic}),*bundle.stories[1:]]})
    review=human_decisions(m,bundle)
    with pytest.raises(ArtifactError,match='every review issue'):validate_review(m,bundle,review,audit_case[3])
    data=review.model_dump();data['items'][0]['resolutions']=[{'issue_id':'unclear_number','evidence':'At 00:01 the spoken number is clear and matches the original source.'}]
    assert validate_review(m,bundle,HumanReview.model_validate(data),audit_case[3])


def test_semantic_evidence_and_rule_coverage():
    result=SemanticAudit(decision='pass',rules=[],issues=[],summary='Empty.')
    with pytest.raises(ValueError,match='every policy'):validate_semantic(result,{'required'},set(),set())
    with pytest.raises(ValidationError,match='actionable issue'):
        SemanticAudit(decision='needs_review',rules=[{'rule_id':'r','result':'review','reason':'Unclear.'}],issues=[],summary='Unclear.')
    result=SemanticAudit(decision='needs_review',rules=[{'rule_id':'r','result':'review','reason':'Unclear.'}],issues=[dict(issue_id='i',rule_id='r',severity='review',kind='context',explanation='Missing context.',source_segment_ids=['invented'],final_segment_ids=[])],summary='Unclear.')
    with pytest.raises(ValueError,match='invents'):validate_semantic(result,{'r'},{'s0'},set())


def test_media_audit_uses_final_bytes_and_stronger_asr(audit_case,monkeypatch):
    from clip_pipeline.audit_media import inspect_media
    from clip_pipeline.config import PipelineConfig
    ws,m,d,_,transcript=audit_case
    artifact=read_json_model(d/m.artifacts['render.story.rates.story'].path,StoryArtifact)
    config=read_json_model(d/m.artifacts['config'].path,PipelineConfig)
    seen={}
    def transcribe(source,media,settings):
        seen.update(source=source,model=settings.media.whisper_model)
        return transcript.model_copy(update={'source_sha256':source.media_sha256,'duration_seconds':4.0,'segments':transcript.segments[:1]}),{}
    monkeypatch.setattr('clip_pipeline.audit_media.transcribe',transcribe)
    monkeypatch.setattr('clip_pipeline.audit_media.validate_output',lambda *a:{'duration':4.0})
    result=inspect_media(d/m.artifacts['render.story.rates.final_media'].path,artifact,config,d/'render')
    assert seen['source'].media_sha256==artifact.final.media.sha256 and seen['model']=='small'
    assert result.transcript.source_sha256!=transcript.source_sha256


def test_caption_bounds(tmp_path):
    from clip_pipeline.audit_media import caption_text
    from clip_pipeline.media_common import MediaError
    p=tmp_path/'a.ass';p.write_text('Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{\\b1}Rates affect stocks.')
    assert caption_text(p,2)=='[0.00–2.00] Rates affect stocks.'
    with pytest.raises(MediaError):caption_text(p,1)


def test_context_overflow_is_nonbypassable(audit_case):
    from clip_pipeline.config import load_inputs
    from clip_pipeline.audit_runner import audit_inputs
    _,m,d,plan,_=audit_case
    _,_,_,_,policy,_=audit_inputs(m,d)
    def overflow(*args):raise FoundryError('context_budget_exceeded')
    result=audit_item('story','rates',plan.sequences[0].stories[0],{'source_context':[],'final_audio':[]},policy,overflow)
    assert result.decision=='reject' and result.deterministic_issues[0].issue_id=='audit_context_budget'


def test_duplicate_market_links_group_without_losing_evidence(media_case):
    from test_clip_pipeline_enrichment import inputs,evaluator,DAY
    from clip_pipeline.selection import select_stories
    from clip_pipeline.audit import grouped_associations,display_metadata
    config,source,transcript,policy,markets,knowledge=inputs(media_case)
    call,_=evaluator(transcript,policy)
    plan,_=select_stories(config,source,transcript,policy,markets,call,knowledge=knowledge,assessed_on=DAY)
    story=plan.sequences[0].stories[0]
    groups=grouped_associations(story.enrichment)
    supplier=next(g for g in groups if g['market_id']=='xyz:EXAMPLE')
    assert len(supplier['evidence_paths'])==2
    assert {p['basis'] for p in supplier['evidence_paths']}=={'category_exposure','relationship'}
    display=display_metadata(story)
    assert len(display['editorial_associations_not_speaker_claims'])==2
    assert 'enrichment' not in display


def test_selected_source_excludes_words_outside_cut(media_case):
    from clip_pipeline.audit import selected_source
    transcript=media_case[3]
    selected=selected_source(transcript,0.5,2.0)
    assert selected[0]['text']=='affect stocks.'
    assert selected[0]['start']==0.5


def test_karaoke_events_are_collapsed_without_erasing_repeated_speech(tmp_path):
    from clip_pipeline.audit_media import caption_text
    p=tmp_path/'captions.ass'
    p.write_text('Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\b1}Rates affect stocks.\n'
                 'Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Rates {\\b1}affect stocks.\n'
                 'Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,Rates affect stocks.\n')
    assert caption_text(p,4).splitlines()==['[0.00–2.00] Rates affect stocks.','[3.00–4.00] Rates affect stocks.']


def test_cli_audit_status_and_unapproved_promote(audit_case,capsys):
    from clip_pipeline.__main__ import main
    m=finish(audit_case)
    args=['--workspace',str(audit_case[0]),'--run-id',m.run_id]
    assert main(['status',*args])==3
    response=json.loads(capsys.readouterr().out)
    assert not response['approved'] and response['audit_bundle'] and response['review_template']
    assert main(['promote',*args])==2
    assert 'invalid_artifact' in capsys.readouterr().err


def test_removed_market_and_wrong_knowledge_are_nonbypassable(media_case):
    from test_clip_pipeline_enrichment import inputs,evaluator,DAY
    from clip_pipeline.selection import select_stories
    from clip_pipeline.audit import metadata_issues
    config,source,transcript,policy,markets,knowledge=inputs(media_case)
    call,_=evaluator(transcript,policy)
    plan,_=select_stories(config,source,transcript,policy,markets,call,knowledge=knowledge,assessed_on=DAY)
    story=plan.sequences[0].stories[0]
    assert not metadata_issues(story,markets,knowledge)
    assert metadata_issues(story,markets,None)[0].severity=='reject'
    removed=markets.model_copy(update={'markets':[]})
    issues=metadata_issues(story,removed,knowledge,markets)
    assert issues[0].issue_id=='market_metadata_changed' and issues[0].severity=='reject'


def test_audit_termination_preserves_checkpoint(audit_case):
    import signal
    ws,m,*_=audit_case
    old=signal.getsignal(signal.SIGTERM)
    def terminate(*args,**kwargs):
        assert callable(signal.getsignal(signal.SIGTERM))
        signal.raise_signal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt):execute_audit(ws,m.run_id,worker=terminate)
    assert signal.getsignal(signal.SIGTERM)==old
    saved=inspect_run(ws,m.run_id)
    assert saved.state=='interrupted' and saved.outcome is None
    assert finish(audit_case).stage=='audited'
