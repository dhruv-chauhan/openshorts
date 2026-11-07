"""Evidence, typography, and lexical checks keep uncertainty visible."""
from pathlib import Path
import json
import pytest
from clip_pipeline.artifacts import ArtifactError, digest, read_json_model
from clip_pipeline.config import CaptionConfig
from clip_pipeline.captions import generate, layout, phrases
from clip_pipeline.caption_audit import disagreements
from clip_pipeline.contracts import TranscriptArtifact, StoryArtifact
from clip_pipeline.corrections import corrected_transcript, correction_issues
from clip_pipeline.audit_contracts import MediaInspection
from clip_pipeline.audit import display_metadata
from clip_pipeline.media_common import MediaError
from test_clip_pipeline_config import config_data  # noqa: F401
from test_clip_pipeline_media import media_case  # noqa: F401
from test_clip_pipeline_stories import story_case  # noqa: F401
from test_clip_pipeline_audit import audit_case  # noqa: F401
from test_clip_pipeline_enrichment import inputs, evaluator
from clip_pipeline.selection import select_stories


def test_caption_layout_measures_text_below_source_and_above_app_ui(tmp_path):
    style=CaptionConfig();font,box=layout(style,1920,1080)
    words=[{'word':w,'start':float(i)*.3,'end':float(i+1)*.3}
           for i,w in enumerate('Borrowing costs affect earnings. This qualification must remain visible.'.split())]
    rows=phrases(words,font,box,style)
    assert ' '.join(' '.join(r['lines']) for r in rows)==' '.join(w['word'] for w in words)
    assert box['top']>box['content_bottom'] and box['bottom']<=1920-style.bottom_safe_margin
    for row in rows:
        assert len(row['lines'])<=2 and all(font.getlength(line)<=box['width'] for line in row['lines'])
    ass,srt=tmp_path/'captions.ass',tmp_path/'captions.srt'
    generate({'segments':[{'words':words}]},style,1920,1080,ass,srt)
    assert 'PlayResX: 1080' in ass.read_text() and 'PlayResY: 1920' in ass.read_text()
    assert 'Borrowing costs' in ass.read_text() and r'\t(' not in ass.read_text()
    with pytest.raises(MediaError,match='safe_area'):layout(style,1080,1920)
    with pytest.raises(MediaError,match='safe_width'):
        phrases([{'word':'W'*100,'start':0.,'end':1.}],font,box,style)


def test_coalesced_asr_unit_wraps_without_invented_word_times():
    style=CaptionConfig();font,box=layout(style,1920,1080)
    text='The company explains its operating costs.'
    rows=phrases([{'word':text,'start':1.,'end':2.}],font,box,style)
    assert len(rows)==1 and rows[0]['start']==1. and rows[0]['end']==2.
    assert ' '.join(rows[0]['lines'])==text and len(rows[0]['lines'])==2


def test_caption_asr_disagreement_keeps_numbers_and_words(media_case):
    transcript=media_case[3]
    assert not disagreements('[0.00–2.00] RATES affect stocks!',transcript)
    diff=disagreements('[0.00–2.00] Rates affect bonds.',transcript)
    assert diff[0]['caption']=='bonds' and diff[0]['final_asr']=='stocks'
    diff=disagreements('[0.00–2.00] Rates affect 120%.',transcript)
    assert '120%' in diff[0]['caption']


def correction_case(case):
    ws,m,d,_,original=case
    target=read_json_model(d/m.artifacts['render.story.rates.story'].path,StoryArtifact)
    data=original.model_dump();data['source_sha256']=target.final.media.sha256
    data['duration_seconds']=target.final.duration_seconds;data['segments']=data['segments'][:1]
    data['segments'][0]['words'][-1]['word']='profits.'
    data['segments'][0]['text']='Rates affect profits.'
    independent=TranscriptArtifact.model_validate(data)
    inspection=MediaInspection(story_id=target.story.story_id,final_sha256=target.final.media.sha256,
        checks={},issues=[],transcript=independent,captions_text='')
    return original,target,inspection,d/'render'/target.final.media.path,


def test_correction_is_bound_timed_and_pending_review(audit_case):
    original,target,inspection,media=correction_case(audit_case)
    segment=original.segments[0];index=len(segment.words)-1
    patch={'segment_id':segment.segment_id,'word_index':index,'before':segment.words[index].word,'after':'profits.'}
    before=digest(original);result=corrected_transcript(original,target,inspection,media,patch)
    assert digest(original)==before and digest(result)!=before
    assert result.segments[0].words[-1].start==segment.words[-1].start
    assert result.corrections[0].status=='pending_review'
    assert correction_issues(result,0,4)[0].severity=='review'
    assert not correction_issues(result,5,10)
    with pytest.raises(ArtifactError,match='aligned'):
        corrected_transcript(original,target,inspection,media,patch|{'after':'dividends.'})
    with pytest.raises(ArtifactError,match='mismatch'):
        corrected_transcript(original,target,inspection,media,patch|{'before':'wrong'})
    with pytest.raises(ArtifactError,match='bind'):
        corrected_transcript(original,target,inspection.model_copy(update={'final_sha256':'f'*64}),media,patch)


def test_classification_mechanisms_never_leak_into_public_copy(media_case):
    config,source,transcript,policy,markets,k=inputs(media_case)
    original,_=evaluator(transcript,policy)
    def call(stage,system,payload,model,validate):
        if model.__name__=='SelectionReview':
            assert all('enrichment' not in s for q in payload['proposals']['sequences'] for s in q['stories'])
        return original(stage,system,payload,model,validate)
    plan,_=select_stories(config,source,transcript,policy,markets,call,knowledge=k,assessed_on='2026-11-01')
    story=plan.sequences[0].stories[0]
    assert len(story.enrichment.assessment_history)==2
    public=display_metadata(story)
    assert all(set(c)=={'category_id','evidence_segment_ids'} for c in public['categories'])
    assert 'stance' not in public


def test_provider_failure_retains_cli_checkpoint_for_backup(media_case,tmp_path,monkeypatch,capsys):
    from clip_pipeline.__main__ import main
    from clip_pipeline.foundry import FoundryError
    from clip_pipeline.artifacts import json_bytes
    config,_,source,transcript=media_case
    paths=[]
    for name,value in [('config',config),('source',source),('transcript',transcript)]:
        path=tmp_path/(name+'.json');path.write_bytes(json_bytes(value));paths.append(path)
    def fail(*args,**kwargs):raise FoundryError('invalid_response')
    monkeypatch.setattr('clip_pipeline.selection_runner.execute_selection',fail)
    assert main(['select','--config',str(paths[0]),'--source-artifact',str(paths[1]),'--transcript',str(paths[2])])==4
    result=json.loads(capsys.readouterr().out)
    assert result['error']=='invalid_response' and Path(result['manifest']).exists()
    assert Path(result['manifest']).parent.name==result['run_id'] and result['approved'] is False


def test_independent_association_veto_cannot_be_overridden(media_case):
    from clip_pipeline.enrichment import VERIFY
    config,source,transcript,policy,markets,k=inputs(media_case)
    original,_=evaluator(transcript,policy)
    def call(stage,system,payload,model,validate):
        result=original(stage,system,payload,model,validate)
        if system==VERIFY:
            result=model.model_validate({'associations':[a.model_dump()|{'decision':'unsupported',
                'exposure_direction':'unclear','reason':'No source-established material effect.'} for a in result.associations]})
            validate(result)
        return result
    plan,report=select_stories(config,source,transcript,policy,markets,call,knowledge=k,assessed_on='2026-11-01')
    assert not plan.sequences and report['outcome']=='completed_no_matches'
