"""Source checkpoints, private recovery, and measured single-worker jobs."""
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from clip_pipeline.artifacts import ArtifactError,json_bytes
from clip_pipeline.checkpoints import backup_run,restore_run
from clip_pipeline.config import PipelineConfig
from clip_pipeline.runner import prepare_run,inspect_run
from clip_pipeline.source_runner import execute_source
from clip_pipeline.worker_job import execute_job,send_metrics
from test_clip_pipeline_config import config_data,config_path  # noqa:F401
from test_clip_pipeline_media import media_case  # noqa:F401
from test_clip_pipeline_stories import story_case,fake_worker  # noqa:F401
from test_clip_pipeline_storage import Sink


@pytest.fixture
def source_case(story_case,monkeypatch):
    config,config_file,_,_,source,_=story_case
    data=config.model_dump();data['blob']={'account_url':'https://example.blob.core.windows.net','runs_container':'runs','approved_container':'approved'}
    config=PipelineConfig.model_validate(data);config_file.write_bytes(json_bytes(config))
    calls=fake_worker(story_case,monkeypatch)
    from clip_pipeline import execution
    m,d=prepare_run(config_file,source.origin,source_only=True)
    return config,m,d,calls,execution._worker


def test_source_resumes_after_transcription_interrupt(source_case):
    c,m,d,calls,worker=source_case;attempts=[]
    def interrupted(action,*args,**kwargs):
        if action=='transcribe' and not attempts:
            attempts.append(1);raise KeyboardInterrupt
        return worker(action,*args,**kwargs)
    with pytest.raises(KeyboardInterrupt):execute_source(c.workspace,m.run_id,worker=interrupted)
    saved=inspect_run(Path(c.workspace),m.run_id)
    assert saved.stage=='ingested' and saved.state=='interrupted'
    result=execute_source(c.workspace,m.run_id,worker=interrupted)
    assert result.stage=='transcribed' and result.outcome is None and result.state=='checkpointed'
    assert calls==[('ingest',None),('transcribe',None)]
    assert execute_source(c.workspace,m.run_id,worker=lambda *a:pytest.fail('repeated IO'))==result


class ArchiveSink(Sink):
    def __init__(self):
        super().__init__()
        self.client=SimpleNamespace(get_blob_client=lambda container,key:SimpleNamespace(
            download_blob=lambda:SimpleNamespace(chunks=lambda:iter([self.objects[(container,key)]]))))


@pytest.mark.parametrize('include_source',[False,True])
def test_source_backup_restore_exact_bytes(source_case,include_source):
    c,m,d,calls,worker=source_case
    complete=execute_source(c.workspace,m.run_id,worker=worker);sink=ArchiveSink()
    receipt=backup_run(c.workspace,m.run_id,include_source=include_source,sink_factory=sink)
    assert sink.writes[-1][1].endswith('/checkpoint.json') and not receipt['approved']
    raw=(d/'manifest.json').read_bytes()
    source_file=Path(m.source.locator)
    shutil.rmtree(d)
    if not include_source:
        with pytest.raises(ArtifactError,match='exact source media'):restore_run(c,receipt,sink_factory=sink)
        assert not d.exists()
    restored=restore_run(c,receipt,source_media=source_file,sink_factory=sink)
    assert restored==complete and (d/'manifest.json').read_bytes()==raw
    with pytest.raises(ArtifactError,match='never overwrites'):restore_run(c,receipt,sink_factory=sink)


def test_corrupt_remote_checkpoint_never_restores(source_case):
    c,m,d,calls,worker=source_case
    execute_source(c.workspace,m.run_id,worker=worker);sink=ArchiveSink()
    receipt=backup_run(c.workspace,m.run_id,include_source=True,sink_factory=sink)
    shutil.rmtree(d)
    address=next(k for k in sink.objects if '/objects/' in k[1]);sink.objects[address]=b'bad'
    with pytest.raises(ArtifactError,match='checksum'):restore_run(c,receipt,sink_factory=sink)
    assert not d.exists() and not list(Path(c.workspace).glob('.restore-*'))


def test_backup_interruption_has_no_discovery_marker(source_case):
    c,m,d,calls,worker=source_case
    execute_source(c.workspace,m.run_id,worker=worker);sink=ArchiveSink();sink.fail_at=2
    with pytest.raises(RuntimeError):backup_run(c.workspace,m.run_id,sink_factory=sink)
    assert not any(key.endswith('/checkpoint.json') for _,key in sink.objects)


def test_worker_job_is_bound_measured_and_cannot_approve(config_path,tmp_path):
    root=tmp_path/'jobs';job={'job_id':'validation','args':['status','--workspace',str(tmp_path/'missing'),'--run-id','a'*32]}
    result=execute_job(job,root)
    assert result['metrics']['ExitCode']!=0 and result['metrics']['ElapsedSeconds']>0
    assert not result['approved'] and execute_job(job,root)==result
    with pytest.raises(ArtifactError,match='different input'):execute_job(job|{'args':['preflight']},root)
    with pytest.raises(ArtifactError,match='invalid worker'):execute_job({'job_id':'publish','args':['promote']},root)
    with pytest.raises(ArtifactError,match='invalid monitor'):send_metrics({'endpoint':'https://example.invalid','rule_id':'dcr-'+'a'*32},{})


def test_unknown_metadata_is_not_a_display_assertion(story_case):
    from clip_pipeline.audit import display_metadata
    story=story_case[3].sequences[0].stories[0]
    display=display_metadata(story)
    assert 'speaker' not in display and 'stance' not in display and 'timeframe' not in display
    assert display['title']==story.title


def test_verified_cache_import_preserves_origin_and_rejects_changed_bytes(media_case,tmp_path):
    from clip_pipeline.contracts import SourceInput
    from clip_pipeline.ingest import seed_source,ingest
    from clip_pipeline.media_common import MediaError
    config,path,source,_=media_case
    origin=SourceInput(kind='youtube',locator='https://www.youtube.com/watch?v=abc12345678')
    source=source.model_copy(update={'origin':origin})
    result=seed_source(config,source,path)
    restored,_,reused=ingest(origin,tmp_path/'cached',config)
    assert reused and restored==source and not result['approved']
    path.write_bytes(b'changed')
    with pytest.raises(MediaError,match='mismatch'):seed_source(config,source,path)
