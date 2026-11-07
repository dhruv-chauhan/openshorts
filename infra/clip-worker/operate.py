"""Launch one explicit worker job through Azure Run Command, with real exit status."""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess


def main():
    parser=argparse.ArgumentParser()
    for name in ['group','vm','registry','image','account','job-id','job-blob']:
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    body=Path(__file__).with_name('run-job.sh').read_text()
    parameters=[args.registry,args.image,args.account,args.job_id,args.job_blob]
    script="cat > /srv/clip/run-job.sh <<'CLIP_WORKER_SCRIPT'\n"+body+"\nCLIP_WORKER_SCRIPT\nbash /srv/clip/run-job.sh "+shlex.join(parameters)+"\ncode=$?\nprintf '\\nCLIP_EXIT_CODE=%s\\n' \"$code\"\nexit \"$code\"\n"
    raw=subprocess.check_output(['az','vm','run-command','invoke','-g',args.group,'-n',args.vm,'--command-id','RunShellScript','--scripts',script,'-o','json'],text=True)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(raw)
    text='\n'.join(v.get('message','') for v in json.loads(raw).get('value',[]))
    matches=re.findall(r'CLIP_EXIT_CODE=(\d+)',text)
    if not matches:raise RuntimeError('no worker exit status; inspect the private command result')
    code=int(matches[-1]);print(json.dumps({'job_id':args.job_id,'exit_code':code}))
    return code
if __name__=='__main__':raise SystemExit(main())
