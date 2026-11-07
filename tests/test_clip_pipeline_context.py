"""Context resolution requires exact evidence, full coverage and independent agreement."""
import pytest
from clip_pipeline.context_resolution import resolve_context
from clip_pipeline.selection import windows_for
from clip_pipeline.selection_contracts import RelevanceResponse, ContextResolution, ContextResolutionReview
from clip_pipeline.selection_prompts import snapshot
from clip_pipeline.config import load_inputs
from clip_pipeline.foundry import FoundryError
from test_clip_pipeline_config import config_data  # noqa: F401
from test_clip_pipeline_media import media_case  # noqa: F401
from test_clip_pipeline_selection import scenario


def setup_case(media_case):
    config,source,transcript=scenario(media_case,['Its borrowing costs affect profits.','Financing costs matter here.','This discussion concerns stock market earnings.'])
    policy,markets=load_inputs(config);windows=windows_for(transcript,config.selection)
    decisions=RelevanceResponse.model_validate({'windows':[{
        'window_id':w['window_id'],'decision':'review' if i==0 else 'eligible',
        'topic_path':None if i==0 else 'equity_macro','matched_market_ids':[],
        'connection':'Stock earnings','evidence':[] if i==0 else [quote(transcript,1)],
        'continues_before':True,'continues_after':True,'reason':'Unclear antecedent' if i==0 else 'Equity earnings'
    } for i,w in enumerate(windows)]}).windows
    return windows,decisions,transcript,markets


def quote(transcript,i):
    s=transcript.segments[i]
    return {'segment_id':s.segment_id,'start':s.start,'end':s.end,'quote':s.text}


def resolver(transcript,decisions,*,mutation=None,veto=False,error=None):
    def call(stage,system,payload,model,validate):
        if error:raise FoundryError(error)
        if model is ContextResolution:
            item=decisions[0].model_dump()|{'decision':'eligible','topic_path':'equity_macro',
                 'evidence':[quote(transcript,0)],'context_evidence':[quote(transcript,2)],
                 'connection':'The adjacent speaker establishes equity earnings as the subject.'}
            if mutation=='invented_quote':item['context_evidence'][0]['quote']='Invented statement'
            if mutation=='no_adjacent':item['context_evidence']=[quote(transcript,0)]
            if mutation=='no_local':item['evidence']=[]
            if mutation=='invented_market':item.update(topic_path='allowed_market',matched_market_ids=['xyz:EXAMPLE'])
            data={'windows':[] if mutation=='missing' else [item,item] if mutation=='duplicate' else [item]}
        elif model is ContextResolutionReview:
            data={'windows':[{'window_id':decisions[0].window_id,'verdict':'unresolved' if veto else 'confirmed',
                             'evidence':[quote(transcript,2)],'reason':'Context checked independently'}]}
            if mutation=='check_missing':data['windows']=[]
            if mutation=='check_no_adjacent':data['windows'][0]['evidence']=[quote(transcript,0)]
        else:raise AssertionError(model)
        result=model.model_validate(data);validate(result);return result
    return call


@pytest.mark.parametrize('veto',[False,True])
def test_context_needs_evidence_and_independent_confirmation(media_case,veto):
    w,d,t,m=setup_case(media_case)
    effective,records=resolve_context(w,d,t,m,{},snapshot(),resolver(t,d,veto=veto),enriched=False)
    assert effective[0].decision==('review' if veto else 'eligible')
    assert records[0]['resolved_window_ids']==([] if veto else [d[0].window_id])
    assert d[0].decision=='review'  # Original evidence/decision is immutable.
    assert effective[1]==d[1]


@pytest.mark.parametrize('mutation',['invented_quote','no_adjacent','no_local','invented_market','missing','duplicate','check_missing','check_no_adjacent'])
def test_invalid_context_resolution_cannot_clear_review(media_case,mutation):
    w,d,t,m=setup_case(media_case)
    with pytest.raises(ValueError):
        resolve_context(w,d,t,m,{},snapshot(),resolver(t,d,mutation=mutation),enriched=False)


def test_capacity_limit_preserves_review_and_provider_failure_is_not_no_match(media_case):
    w,d,t,m=setup_case(media_case)
    effective,records=resolve_context(w,d,t,m,{},snapshot(),resolver(t,d,error='context_budget_exceeded'),enriched=False)
    assert effective==d and records[0]['reason']=='context_budget_exceeded'
    with pytest.raises(FoundryError):
        resolve_context(w,d,t,m,{},snapshot(),resolver(t,d,error='authentication_failed'),enriched=False)


@pytest.mark.parametrize('veto',[False,True])
def test_whole_video_hold_clears_only_after_verified_context(media_case,veto):
    from clip_pipeline.selection import select_stories
    from test_clip_pipeline_selection import model_call
    config,source,transcript=scenario(media_case,['Its borrowing costs affect profits.','Financing costs matter here.','This discussion concerns stock market earnings.'])
    policy,markets=load_inputs(config)
    base,_=model_call(transcript,policy)
    _,decisions,_,_=setup_case(media_case)
    context=resolver(transcript,decisions,veto=veto)
    def call(stage,system,payload,model,validate):
        if model in (ContextResolution,ContextResolutionReview):
            return context(stage,system,payload,model,validate)
        if model is RelevanceResponse:
            result=RelevanceResponse(windows=decisions);validate(result);return result
        return base(stage,system,payload,model,validate)
    plan,report=select_stories(config,source,transcript,policy,markets,call)
    assert bool(plan.sequences) is (not veto)
    assert report['outcome']==('needs_review' if veto else 'selected')
    assert report['initial_windows'][0]['decision']=='review'
    assert report['windows'][0]['decision']==('review' if veto else 'eligible')


def test_old_prompt_bindings_remain_readable():
    from clip_pipeline.selection_prompts import binding
    from clip_pipeline.artifacts import digest
    old={'version':'previous','relevance':'r','proposal':'p','check':'c','rank':'k',
         'classification':'classify','association':'associate'}
    assert binding(old)==digest(['previous','r','p','c','k','classify','associate'])
    assert binding(old|{'context_resolve':'resolve','context_check':'verify'})!=binding(old)
