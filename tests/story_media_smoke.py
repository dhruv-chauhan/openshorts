"""Real story rendering, signal interruption and resume inside the media image."""
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, '/app')
from clip_pipeline.artifacts import digest, read_json_model
from clip_pipeline.contracts import TranscriptArtifact
from clip_pipeline.media_common import validate_output

config_path, source, transcript_path, root = map(Path, sys.argv[1:5])
root.mkdir(parents=True, exist_ok=True)
config = json.loads(config_path.read_text())
transcript = read_json_model(transcript_path, TranscriptArtifact)
words = [w for s in transcript.segments for w in s.words]
mid = len(words) // 2
# The fixture is generated speech; pick two disjoint observed word ranges.
plan = {'schema_version':'1', 'source':{'kind':'local','locator':str(source)},
        'source_sha256':transcript.source_sha256, 'transcript_sha256':digest(transcript),
        'sequences':[{'sequence_id':'economy','title':'Economic factors', 'stories':[
            {'story_id':'first','title':'First explanation','span':{'start':words[0].start,'end':words[mid-1].end}},
            {'story_id':'second','title':'Further explanation','span':{'start':words[mid].start,'end':words[-1].end},
             'playback_rate':1.1}]}]}
plan_path = root/'plan.json'; plan_path.write_text(json.dumps(plan))


def cli(args):
    process = subprocess.run([sys.executable,'-m','clip_pipeline',*map(str,args)],capture_output=True,text=True,timeout=900)
    if process.returncode:
        raise RuntimeError(process.stdout + process.stderr)
    return json.loads(process.stdout)


paused = cli(['render-stories','--config',config_path,'--plan',plan_path,'--transcript',transcript_path,
              '--stop-after-stories','1'])
manifest_path = Path(paused['manifest'])
manifest = json.loads(manifest_path.read_text())
assert manifest['state']=='interrupted' and manifest['story_progress']['first']['state']=='rendered'
assert 'sequence_index' not in manifest['artifacts']
first_ref = manifest['artifacts']['story.first.final_media']
first_path = manifest_path.parent/first_ref['path']
first_mtime = first_path.stat().st_mtime_ns
resume_args=['resume','--workspace',config['workspace'],'--run-id',paused['run_id'],'--stories-only']
# Kill the controller while a real media subprocess is running for the second story.
with (root/'interrupted.log').open('w') as log:
    process = subprocess.Popen([sys.executable,'-m','clip_pipeline',*resume_args],stdout=log,stderr=log)
    deadline=time.monotonic()+120
    descendants=[]
    while time.monotonic()<deadline:
        manifest=json.loads(manifest_path.read_text())
        child_list=Path(f'/proc/{process.pid}/task/{process.pid}/children')
        descendants=child_list.read_text().split() if child_list.exists() else []
        if manifest['story_progress']['second']['state']=='running' and descendants:
            # The renderer has entered its fresh worker; signal before completion.
            break
        if process.poll() is not None:
            raise AssertionError('second story completed before interruption test')
        time.sleep(.05)
    else:
        process.kill(); process.wait(); raise AssertionError('worker did not start')
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=15)==130
for child in descendants:
    assert not Path(f'/proc/{child}').exists(), 'media child survived controller interruption'
manifest=json.loads(manifest_path.read_text())
assert manifest['state']=='interrupted' and manifest['story_progress']['second']['state']=='interrupted'
assert 'sequence_index' not in manifest['artifacts']
complete=cli(resume_args)
manifest=json.loads(manifest_path.read_text())
assert manifest['artifacts']['story.first.final_media']==first_ref and first_path.stat().st_mtime_ns==first_mtime
assert manifest['story_progress']['first']['attempts']==1 and manifest['story_progress']['second']['attempts']==2
sequence=json.loads((manifest_path.parent/manifest['artifacts']['sequence.economy']['path']).read_text())
assert [a['story']['story_id'] for a in sequence['stories']]==['first','second']
assert not sequence['approved'] and complete['final_media'] is None
assert len({a['final']['media']['path'] for a in sequence['stories']})==2
for artifact in sequence['stories']:
    spec=artifact['story']; duration=(spec['span']['end']-spec['span']['start'])/spec['playback_rate']
    validate_output(manifest_path.parent/artifact['final']['media']['path'],duration)
    timeline=json.loads((manifest_path.parent/artifact['timeline']['path']).read_text())
    assert abs(timeline['expected_duration_seconds']-duration)<1e-6
    assert all(0<=w['output']['start']<w['output']['end']<=duration for w in timeline['words'])
    srt=(manifest_path.parent/artifact['captions_srt']['path']).read_text()
    stamps=re.findall(r'(\d+):(\d+):(\d+),(\d+)',srt)
    times=[int(h)*3600+int(m)*60+int(s)+int(ms)/1000 for h,m,s,ms in stamps]
    assert times and min(times)>=0 and max(times)<=duration+0.05
    streams=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',
                      str(manifest_path.parent/artifact['final']['media']['path'])]))['streams']
    video=next(s for s in streams if s['codec_type']=='video')
    audio=next(s for s in streams if s['codec_type']=='audio')
    assert abs(float(video['start_time'])-float(audio['start_time']))<0.1
    assert abs(float(video['duration'])-float(audio['duration']))<0.2
# A new rate creates new pending work; it cannot inherit the old sequence approval or files.
plan['sequences'][0]['stories'][0]['playback_rate']=1.1
plan_path.write_text(json.dumps(plan))
changed=cli(['render-stories','--config',config_path,'--plan',plan_path,'--transcript',transcript_path,'--prepare-only'])
assert changed['input_fingerprint']!=complete['input_fingerprint']
assert all(p['state']=='pending' for p in changed['story_progress'].values())
subprocess.run([sys.executable,'/app/tests/audit_media_smoke.py',str(config_path),
    config['workspace'],complete['run_id'],str(root/'audit')],check=True,timeout=900)
(root/'story-smoke-report.json').write_text(json.dumps({'ok':True,'real_media':True,
    'separate_outputs':True,'signal_interruption_and_resume':True,'completed_story_unchanged':True,
    'rate_change_invalidated_outputs':True,'final_audio_audit_smoke_passed':True,'rates':[1.0,1.1],'run':complete},indent=2)+'\n')
print('Story media smoke passed')
