"""Opt-in paid prompt evaluation; synthetic text only, never a media artifact."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clip_pipeline.config import load_config, load_inputs
from clip_pipeline.foundry import FoundryBackend
from clip_pipeline.markets import fetch_markets
from clip_pipeline.selection_contracts import RelevanceResponse
from clip_pipeline.selection_prompts import RELEVANCE, PROMPT_VERSION
from clip_pipeline.selection import check_markets


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    config=load_config(args.config);policy,_=load_inputs(config)
    markets,meta=fetch_markets(config.markets,Path(config.workspace)/'cache/markets')
    cases=json.loads((Path(__file__).parent/'fixtures/clip_pipeline/editorial_cases.json').read_text())['cases']
    items=[{'window_id':c['id'],'segments':[{'segment_id':c['id'],'start':0.0,'end':30.0,'text':c['text']}]} for c in cases]
    payload={'policy':policy.model_dump(),'markets':[[m.market_id,m.underlying_name,m.aliases] for m in markets.markets if m.enabled],
             'markets_columns':['market_id','underlying_name','aliases'],'items':items}
    def validate(result):
        if len(result.windows)!=len(cases) or {w.window_id for w in result.windows}!={c['id'] for c in cases}:
            raise ValueError('missing test windows')
        by_id={c['id']:c for c in cases}
        for w in result.windows:
            check_markets(w.matched_market_ids,markets,w.topic_path)
            for e in w.evidence:
                if e.segment_id!=w.window_id or e.quote not in by_id[w.window_id]['text'] or e.start<0 or e.end>30:
                    raise ValueError('invented test evidence')
    with FoundryBackend(config) as backend:
        result=backend.generate(stage='relevance',system=RELEVANCE,payload=payload,response_model=RelevanceResponse,validate=validate)
        by_id={w.window_id:w for w in result.windows}
        checks=[{'case':c['id'],'expected':c['expected'],'actual':by_id[c['id']].decision,
                 'pass':by_id[c['id']].decision==c['expected'] and (c['path'] is None or by_id[c['id']].topic_path==c['path'])} for c in cases]
        report={'synthetic':True,'approved':False,'prompt_version':PROMPT_VERSION,'market_fetch':meta,
                'checks':checks,'response':result.model_dump(),'usage':backend.usage_report()}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(checks))
    return 0 if all(c['pass'] for c in checks) else 1


if __name__=='__main__':raise SystemExit(main())
