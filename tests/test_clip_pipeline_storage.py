"""Approval gates, refreshed eligibility, immutable uploads and commit-last recovery."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceExistsError

from clip_pipeline.artifacts import ArtifactError, json_bytes, read_json_model
from clip_pipeline.audit_runner import audit_inputs
from clip_pipeline.config import PipelineConfig
from clip_pipeline.contracts import MarketSnapshot
from clip_pipeline.review import apply_review
from clip_pipeline.runner import inspect_run
from clip_pipeline.storage import BlobSink, StorageError, promote, promotion_check
from test_clip_pipeline_config import config_data, config_path  # noqa: F401
from test_clip_pipeline_media import media_case  # noqa: F401
from test_clip_pipeline_stories import story_case  # noqa: F401
from test_clip_pipeline_audit import audit_case, finish, bundle_for, human_decisions  # noqa: F401


class Sink:
    def __init__(self):self.objects={};self.writes=[];self.fail_at=None;self.count=0
    def __call__(self,config):return self
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def ensure_private(self,name):pass
    def put(self,container,key,*,sha256,size_bytes,path=None,data=None):
        self.count+=1
        if self.count==self.fail_at:raise RuntimeError('interrupted upload')
        raw=path.read_bytes() if path else data
        assert hashlib.sha256(raw).hexdigest()==sha256 and len(raw)==size_bytes
        address=(container,key)
        if address in self.objects:assert self.objects[address]==raw
        self.objects[address]=raw;self.writes.append(address)


def reviewed(case,tmp_path):
    m=finish(case);review=human_decisions(m,bundle_for(case,m));p=tmp_path/'review.json';p.write_bytes(json_bytes(review))
    return apply_review(case[0],m.run_id,p)


def test_commit_last_retry_idempotent_and_review_frozen(audit_case,tmp_path):
    m=reviewed(audit_case,tmp_path);sink=Sink();sink.fail_at=3
    with pytest.raises(StorageError,match='operation_failed'):promote(audit_case[0],m.run_id,sink_factory=sink)
    assert not any(key.endswith('/commit.json') for _,key in sink.objects)
    assert inspect_run(audit_case[0],m.run_id).state=='retryable'
    with pytest.raises(ArtifactError,match='publication already started'):
        apply_review(audit_case[0],m.run_id,tmp_path/'review.json')
    sink.fail_at=None
    result=promote(audit_case[0],m.run_id,sink_factory=sink)
    assert result.stage=='stored' and result.outcome=='completed'
    assert sink.writes[-1][1].endswith('/commit.json')
    commit=json.loads(sink.objects[sink.writes[-1]])
    assert commit['approved'] and commit['development_market_universe']
    for ref in commit['sequences']:
        seq=json.loads(sink.objects[('approved',ref['key'])])
        assert [s['story_id'] for s in seq['ordered_stories']]==['rates','earnings']
        for story in seq['ordered_stories']:assert ('approved',story['media']['key']) in sink.objects
    count=sink.count
    assert promote(audit_case[0],m.run_id,sink_factory=sink)==result and sink.count==count


def test_unreviewed_and_pending_cannot_publish(audit_case,tmp_path):
    m=finish(audit_case);sink=Sink()
    with pytest.raises(ArtifactError,match='completed human review'):promote(audit_case[0],m.run_id,sink_factory=sink)
    review=human_decisions(m,bundle_for(audit_case,m));data=review.model_dump(mode='json')
    for item in data['items']:item['decision']='pending'
    p=tmp_path/'pending.json';p.write_bytes(json_bytes(data));apply_review(audit_case[0],m.run_id,p)
    with pytest.raises(ArtifactError,match='no fully approved'):promote(audit_case[0],m.run_id,sink_factory=sink)
    assert not sink.objects


def test_refresh_failure_after_upload_never_commits(audit_case,tmp_path):
    m=reviewed(audit_case,tmp_path);sink=Sink();calls=[]
    config,plan,source,transcript,policy,markets=audit_inputs(m,audit_case[2])
    def fetch(*args):
        calls.append(1)
        if len(calls)==2:raise RuntimeError('market service unavailable')
        return markets,{}
    with pytest.raises(StorageError):promote(audit_case[0],m.run_id,sink_factory=sink,fetch=fetch)
    assert sink.objects and not any(k.endswith('/commit.json') for _,k in sink.objects)


def test_approval_refresh_has_no_stale_fallback_and_rejects_removed_market(audit_case):
    m=finish(audit_case);config,plan,_,_,_,markets=audit_inputs(m,audit_case[2])
    data=plan.model_dump();data['sequences'][0]['stories'][0]['matched_market_ids']=[markets.markets[0].market_id]
    plan=type(plan).model_validate(data)
    config=config.model_copy(update={'markets':config.markets.model_copy(update={'mode':'api','url':'https://example.invalid/markets','path':None,'max_cache_age_seconds':3600})})
    def fetch(settings,*args):
        assert settings.max_cache_age_seconds==0
        return markets.model_copy(update={'markets':markets.markets[1:]}),{}
    with pytest.raises(ArtifactError,match='eligibility changed'):promotion_check(config,markets,plan,None,Path('/unused'),fetch=fetch)


def test_blob_existing_bytes_verified_and_public_container_refused(config_data):
    config_data['blob']={'account_url':'https://example.blob.core.windows.net','runs_container':'runs','approved_container':'approved'}
    config=PipelineConfig.model_validate(config_data)
    class Blob:
        raw=None
        def upload_blob(self,stream,**kwargs):
            assert kwargs['overwrite'] is False
            if self.raw is not None:raise ResourceExistsError('exists')
            self.raw=stream.read()
        def download_blob(self):return SimpleNamespace(chunks=lambda:iter([self.raw]))
    class Container:
        public=None
        def create_container(self):raise ResourceExistsError('exists')
        def get_container_properties(self):return {'public_access':self.public}
    blob=Blob();container=Container()
    sink=BlobSink(config,client=SimpleNamespace(get_blob_client=lambda *a:blob,get_container_client=lambda *a:container))
    sink.ensure_private('approved')
    kwargs=dict(data=b'content',sha256=hashlib.sha256(b'content').hexdigest(),size_bytes=7)
    sink.put('approved','one',**kwargs);sink.put('approved','one',**kwargs)
    blob.raw=b'corrupted'
    with pytest.raises(StorageError,match='mismatch'):sink.put('approved','one',**kwargs)
    container.public='blob'
    with pytest.raises(StorageError,match='public'):sink.ensure_private('approved')


def test_stale_or_changed_supported_knowledge_blocks_promotion(media_case,tmp_path):
    from datetime import datetime,timezone
    from test_clip_pipeline_enrichment import inputs,evaluator,DAY
    from clip_pipeline.selection import select_stories
    config,source,transcript,policy,markets,knowledge=inputs(media_case)
    call,_=evaluator(transcript,policy)
    plan,_=select_stories(config,source,transcript,policy,markets,call,knowledge=knowledge,assessed_on=DAY)
    fetch=lambda *a:(markets,{})
    with pytest.raises(ArtifactError,match='expired/conflicted'):
        promotion_check(config,markets,plan,knowledge,tmp_path,fetch=fetch,now=datetime(2028,1,1,tzinfo=timezone.utc))
    data=knowledge.model_dump();data['version']='changed'
    Path(config.enrichment.path).write_bytes(json_bytes(data))
    with pytest.raises(ArtifactError,match='knowledge changed'):
        promotion_check(config,markets,plan,knowledge,tmp_path,fetch=fetch)


def test_upload_interrupt_is_resumable_without_commit(audit_case,tmp_path):
    m=reviewed(audit_case,tmp_path)
    class Interrupted(Sink):
        def put(self,*args,**kwargs):raise KeyboardInterrupt
    sink=Interrupted()
    with pytest.raises(KeyboardInterrupt):promote(audit_case[0],m.run_id,sink_factory=sink)
    assert inspect_run(audit_case[0],m.run_id).state=='interrupted' and not sink.objects
    assert promote(audit_case[0],m.run_id,sink_factory=Sink()).stage=='stored'
