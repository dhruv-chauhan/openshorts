"""Scoped identity smoke inside the actual container; never approves media."""
import argparse
import hashlib
from importlib.metadata import distributions
import json
from pathlib import Path
from uuid import uuid4
from .config import load_config
from .foundry import preflight
from .markets import fetch_markets
from .storage import BlobSink


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    config=load_config(args.config)
    assert config.foundry.auth_mode=='managed_identity', 'worker probe requires managed identity'
    assert 'google-genai' not in {d.metadata['Name'].lower() for d in distributions()}
    markets,_=fetch_markets(config.markets,Path(config.workspace)/'cache/markets')
    result={'approved':False,'google_sdk_installed':False,'identity_mode':'managed_identity',
        'market_mode':config.markets.mode,'market_count':len(markets.markets),'foundry':preflight(config)}
    raw=b'{"synthetic_identity_probe":true,"approved":false}\n'
    with BlobSink(config) as sink:
        sink.ensure_private(config.blob.runs_container);sink.ensure_private(config.blob.approved_container)
        key='identity-smoke/'+uuid4().hex+'.json'
        kwargs={'data':raw,'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)}
        sink.put(config.blob.runs_container,key,**kwargs);sink.put(config.blob.runs_container,key,**kwargs)
    result.update(blob_private=True,blob_upload_download_verified=True,blob_immutable_retry_verified=True)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'ok':result['foundry']['ok'],'blob':True,'approved':False}))
    return 0 if result['foundry']['ok'] else 1
if __name__=='__main__':raise SystemExit(main())
