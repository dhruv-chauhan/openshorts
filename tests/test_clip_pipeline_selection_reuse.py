"""Exact request reuse never reuses a gate outcome or skips current validation."""
import json
import pytest
from clip_pipeline import selection_prompts
from clip_pipeline.artifacts import ArtifactError,json_bytes
from clip_pipeline.config import load_inputs
from clip_pipeline.selection_runner import prepare_selection,execute_selection
from test_clip_pipeline_config import config_data  # noqa: F401
from test_clip_pipeline_media import media_case  # noqa: F401
from test_clip_pipeline_selection import scenario,model_call


def parent_case(media_case,tmp_path, reject_rule=None):
    config,source,transcript=scenario(media_case,['Rates affect stock market profits.'])
    policy,_=load_inputs(config);call,calls=model_call(transcript,policy,reject_rule=reject_rule)
    class Backend:
        def __init__(self,*a,**k):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def generate(self,*,stage,system,payload,response_model,validate):
            return call(stage,system,payload,response_model,validate)
    paths=[]
    for name,value in [('config',config),('source',source),('transcript',transcript)]:
        path=tmp_path/(name+'.json');path.write_bytes(json_bytes(value));paths.append(path)
    parent,d=prepare_selection(*paths)
    parent=execute_selection(config.workspace,parent.run_id,backend_factory=Backend)
    return config,paths,parent,d,Backend,calls


def test_only_changed_check_is_paid_again(media_case,tmp_path,monkeypatch):
    c,paths,parent,d,backend,calls=parent_case(media_case,tmp_path)
    monkeypatch.setattr(selection_prompts,'CHECK',selection_prompts.CHECK+'\nAdditional source-fidelity check.')
    child,child_dir=prepare_selection(*paths,reuse_run_id=parent.run_id);calls.clear()
    result=execute_selection(c.workspace,child.run_id,backend_factory=backend)
    assert result.state=='checkpointed' and result.outcome is None
    assert calls==['SelectionReview']
    reused=json.loads((child_dir/result.artifacts['reused_calls'].path).read_text())
    assert reused and all(r['parent_run_id']==parent.run_id for r in reused)
    assert 'reuse_selection' in result.input_artifact_keys


def test_changed_model_settings_refuse_reuse(media_case,tmp_path):
    c,paths,parent,d,_,_=parent_case(media_case,tmp_path)
    data=c.model_dump();data['foundry']['max_output_tokens']+=1;paths[0].write_text(json.dumps(data))
    with pytest.raises(ArtifactError,match='identical provider'):prepare_selection(*paths,reuse_run_id=parent.run_id)


def test_changed_parent_manifest_refuses_reuse(media_case,tmp_path):
    c,paths,parent,d,backend,_=parent_case(media_case,tmp_path)
    child,_=prepare_selection(*paths,reuse_run_id=parent.run_id)
    path=d/'manifest.json';path.write_text(path.read_text()+'\n')
    with pytest.raises(ArtifactError,match='parent changed'):
        execute_selection(c.workspace,child.run_id,backend_factory=backend)
