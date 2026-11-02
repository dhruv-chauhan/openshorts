"""Offline final-audio audit smoke; no Foundry calls or human approval."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, '/app')
from clip_pipeline.artifacts import read_json_model
from clip_pipeline.audit_contracts import MediaInspection

config_path, render_workspace, run_id, output = sys.argv[1:5]
root=Path(output);root.mkdir(parents=True,exist_ok=True)
config=json.loads(Path(config_path).read_text())
config['workspace']=str(root/'workspace')
# Tiny is used only for generated CI speech; the production audit default is small.
config['audit']={'whisper_model':'tiny'}
config_file=root/'config.json';config_file.write_text(json.dumps(config))


def cli(args):
    p=subprocess.run([sys.executable,'-m','clip_pipeline',*map(str,args)],capture_output=True,text=True,timeout=900)
    if p.returncode:raise RuntimeError(p.stdout+p.stderr)
    return json.loads(p.stdout)


result=cli(['audit','--config',config_file,'--render-workspace',render_workspace,'--render-run-id',run_id,'--media-only'])
manifest_path=Path(result['manifest']);manifest=json.loads(manifest_path.read_text())
assert manifest['state']=='interrupted' and manifest['reason_code']=='audit_media_ready'
assert manifest['outcome'] is None and not result['approved']
refs={k:v for k,v in manifest['artifacts'].items() if k.startswith('inspection.')}
assert refs
for key,ref in refs.items():
    inspection=read_json_model(manifest_path.parent/ref['path'],MediaInspection)
    assert not inspection.issues and all(inspection.checks.values())
    final=manifest['artifacts']['render.story.'+inspection.story_id+'.final_media']
    assert inspection.final_sha256==final['sha256']==inspection.transcript.source_sha256
    source=json.loads((manifest_path.parent/manifest['artifacts']['render.source']['path']).read_text())
    assert inspection.transcript.source_sha256!=source['media_sha256']
    assert inspection.transcription['settings']['model']=='tiny' and inspection.transcript.segments
    assert inspection.captions_text
    assert not inspection.transcription['reused']
resumed=cli(['resume','--workspace',config['workspace'],'--run-id',result['run_id'],'--audit-only','--media-only'])
again=json.loads(manifest_path.read_text())
assert refs=={k:v for k,v in again['artifacts'].items() if k.startswith('inspection.')}
assert not any(k.startswith('audit_call.') for k in again['artifacts'])
assert 'audit_bundle' not in again['artifacts'] and 'human_review' not in again['artifacts']
try:google_sdk=importlib.util.find_spec('google.genai') is not None
except ModuleNotFoundError:google_sdk=False
assert not google_sdk
report=dict(ok=True,real_final_audio_asr=True,independent_of_source_transcript=True,
    inspections_reused=True,google_sdk_installed=False,foundry_called=False,approved=False,run=result)
(root/'audit-smoke-report.json').write_text(json.dumps(report,indent=2)+'\n')
print('Final-audio media smoke passed')
