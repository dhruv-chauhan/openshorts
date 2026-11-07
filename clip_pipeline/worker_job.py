"""One measured CLI invocation; private logs and optional verified checkpoint backup."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import sys
import time

from .artifacts import ArtifactError, ArtifactStore, atomic_write, digest, json_bytes
from .execution import termination

ALLOWED = {'source','select','render-stories','audit','resume','status','preflight'}


def execute_job(job, root):
    with ArtifactStore(Path(root)).lock():
        return _execute_job(job,root)


def _execute_job(job, root):
    job_id=job['job_id'];args=job['args']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',job_id) or not args or args[0] not in ALLOWED or not all(isinstance(a,str) for a in args):
        raise ArtifactError('invalid worker job')
    directory=Path(root)/job_id;directory.mkdir(parents=True,exist_ok=True)
    request=directory/'job.json';result_path=directory/'result.json'
    if request.exists() and json.loads(request.read_text()) != job:
        raise ArtifactError('job ID already binds different input')
    if result_path.exists():
        return json.loads(result_path.read_text())
    atomic_write(request,json_bytes(job))
    start=time.monotonic();before=resource.getrusage(resource.RUSAGE_CHILDREN)
    with termination(), (directory/'stdout.log').open('wb') as out, (directory/'stderr.log').open('wb') as err:
        child=subprocess.Popen([sys.executable,'-m','clip_pipeline',*args],stdout=out,stderr=err,start_new_session=True)
        try:
            code=child.wait()
        except KeyboardInterrupt:
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
            code=130
    after=resource.getrusage(resource.RUSAGE_CHILDREN)
    try:response=json.loads((directory/'stdout.log').read_text())
    except (ValueError,OSError):response={}
    metrics={'TimeGenerated':datetime.now(timezone.utc).isoformat(),'JobId':job_id,'Command':args[0],
        'ExitCode':code,'ElapsedSeconds':time.monotonic()-start,
        'CpuSeconds':after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,
        'MaxRssBytes':after.ru_maxrss*(1 if sys.platform=='darwin' else 1024),
        'DiskFreeBytes':shutil.disk_usage(directory).free}
    result={'job_sha256':digest(job),'response':response,'metrics':metrics,'approved':False}
    if response.get('manifest'):
        manifest_path=Path(response['manifest'])
        manifest=json.loads(manifest_path.read_text())
        refs=manifest['artifacts']
        def artifact(key):return json.loads((manifest_path.parent/refs[key]['path']).read_text())
        source_key='source' if 'source' in refs else 'render.source' if 'render.source' in refs else None
        usage=artifact('usage') if 'usage' in refs else {}
        result['pipeline_measurements']={
            'source_seconds':artifact(source_key)['duration_seconds'] if source_key else None,
            'output_seconds':sum(artifact(k)['duration_seconds'] for k in refs if k.endswith('.final')),
            'model_tokens_reported_cumulative':sum(a.get('total_tokens') or 0 for a in usage.get('attempts',[])),
            'model_calls_reserved_cumulative':usage.get('reserved_calls',0)}
    if response.get('manifest') and job.get('backup',False):
        from .checkpoints import backup_run
        try:
            result['checkpoint']=backup_run(Path(response['manifest']).parent.parent.parent,response['run_id'],
                include_source=job.get('include_source',False))
        except Exception as exc:
            result['backup_error']=type(exc).__name__ # detailed data remain in local artifacts
    if job.get('monitor'):
        try:send_metrics(job['monitor'],metrics);result['monitor_sent']=True
        except Exception as exc:result['monitor_error']=type(exc).__name__
    atomic_write(result_path,json_bytes(result))
    return result


def send_metrics(settings, metrics):
    from urllib.parse import urlsplit
    import httpx
    from azure.identity import ManagedIdentityCredential
    endpoint=settings['endpoint'].rstrip('/')
    parsed=urlsplit(endpoint)
    if parsed.scheme!='https' or not (parsed.hostname or '').endswith('.ingest.monitor.azure.com') or parsed.path or parsed.query or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ArtifactError('invalid monitor endpoint')
    if not re.fullmatch(r'dcr-[a-f0-9]{32}',settings['rule_id']):raise ArtifactError('invalid monitor rule')
    with ManagedIdentityCredential() as credential:
        token=credential.get_token('https://monitor.azure.com/.default').token
        with httpx.Client(timeout=30,follow_redirects=False) as client:
            response=client.post(endpoint+'/dataCollectionRules/'+settings['rule_id']+'/streams/Custom-ClipWorker?api-version=2023-01-01',
                json=[metrics],headers={'Authorization':'Bearer '+token})
            response.raise_for_status()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--job',type=Path,required=True);parser.add_argument('--root',type=Path,required=True);args=parser.parse_args()
    result=execute_job(json.loads(args.job.read_text()),args.root)
    print(json.dumps({'job_id':result['metrics']['JobId'],'exit_code':result['metrics']['ExitCode'],'approved':False}))
    return result['metrics']['ExitCode']

if __name__=='__main__':sys.exit(main())
