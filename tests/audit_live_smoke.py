"""Opt-in paid auditor calibration on synthetic positives and genuine violations."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from clip_pipeline.audit import AUDIT, validate_semantic
from clip_pipeline.audit_contracts import SemanticAudit
from clip_pipeline.config import load_config, load_inputs
from clip_pipeline.foundry import FoundryBackend


def cases():
    base='Example Corporation reported third-quarter growth of 12 percent. These figures are unconfirmed.'
    market={'market_id':'xyz:EXAMPLE','underlying_name':'Example Corporation','aliases':['Example'], 'asset_type':'equity','enabled':True}
    def case(name,text,final=None,caption=None,metadata=None,expected=('pass',)):
        return {'name':name,'expected':list(expected),'payload':{'kind':'story',
            'markets':{'markets':[market]},'knowledge':None,'published_at':'2026-01-10T00:00:00Z',
            'proposed_metadata_not_evidence':metadata or {'title':'Company analysis','subject':'Example Corporation','matched_market_ids':['xyz:EXAMPLE']},
            'source_context':[{'segment_id':'s0','text':text,'start':0.0,'end':15.0}],
            'selected_source':[{'segment_id':'s0','text':final or text,'start':0.0,'end':15.0}],
            'final_audio':[{'segment_id':'f0','text':final or text,'start':0.0,'end':15.0}],
            'caption_sidecar':caption or final or text}}
    base_cases = [
        case('attributed_uncertainty_and_relative_quarter',base),
        case('anonymous_first_person',"I think Example Corporation's borrowing costs will squeeze its profits, but lower interest rates could offset that."),
        case('unlisted_supporting_company','Example Corporation buys computing services from Private Models. Rising service prices could increase Example Corporation costs and squeeze profits.'),
        case('caption_number_changed',base,caption=base.replace('12 percent','120 percent'),expected=('reject','needs_review')),
        case('qualification_removed','Example Corporation profits could rise, but only if borrowing costs fall.',final='Example Corporation profits could rise.',expected=('reject',)),
        case('invented_direction','Example Corporation management handled the launch poorly. I am not making any stock-price prediction.',metadata={'title':'Management criticism','subject':'Example Corporation','speaker':'Speaker A','stance':'bearish','matched_market_ids':['xyz:EXAMPLE']},expected=('reject',)),
        case('garbled_final_number',base,final=base.replace('12 percent','[unclear number]'),expected=('needs_review','reject')),
    ]
    direct=case('direct_relevance_without_direction',
        'Example Corporation explained that its operating costs include equipment and electricity.',
        metadata={'title':'Operating costs explained','subject':'Example Corporation',
                  'matched_market_ids':['xyz:EXAMPLE'],'categories':[{'category_id':'operating_costs','evidence_segment_ids':['s0']}]})
    unsupported=case('unsupported_equipment_demand_inference',
        'Example Corporation revenue growth slowed. We have no information about its equipment purchases.',
        metadata={'title':'Company growth discussion','subject':'Example Corporation','matched_market_ids':['xyz:EXAMPLE'],
            'editorial_associations_not_speaker_claims':[{'market_id':'xyz:EXAMPLE','exposure_direction':'negative',
                'evidence_paths':[{'basis':'direct','mechanism':'Slower revenue growth establishes falling equipment purchases.',
                    'evidence_segment_ids':['s0'],'knowledge_record_ids':[]}]}]},expected=('reject',))
    inferred=case('supported_editorial_relationship_not_spoken_stance',
        'On 2026-10-29, Private Models signed a contract to double computing-service purchases from its existing supplier. The spending amount was not disclosed.',
        metadata={'title':'Buyer expands computing-service purchases','subject':'Private Models','matched_market_ids':[],
            'editorial_associations_not_speaker_claims':[{'market_id':'xyz:EXAMPLE','exposure_direction':'positive',
                'evidence_paths':[{'basis':'relationship',
                    'mechanism':'The announced contract increases purchases of the supplier core service. Reviewed knowledge identifies Example Corporation as this supplier; the amount and stock-price effect remain unknown.',
                    'evidence_segment_ids':['s0'],'knowledge_record_ids':['buyer.supplier']}]}]})
    inferred['payload']['knowledge']={'version':'synthetic-calibration', 'sources':[{'source_id':'contract','url':'https://example.invalid/contract',
        'summary':'Synthetic reviewed contract: Example Corporation is Private Models sole existing computing-services supplier during the stated period.',
        'verified_on':'2026-11-01'}], 'relationships':[{'record_id':'buyer.supplier','from_entity':'buyer','to_entity':'supplier',
        'kind':'customer','explanation':'Buyer purchases computing services exclusively from supplier. This is the supplier core revenue-generating service.',
        'source_ids':['contract'],'status':'reviewed','verified_on':'2026-11-01','valid_from':'2026-01-01','valid_through':'2026-11-01'}],
        'entities':[{'entity_id':'buyer','name':'Private Models','market_ids':[]},
                    {'entity_id':'supplier','name':'Example Corporation','market_ids':['xyz:EXAMPLE']}], 'categories':[]}
    inferred['payload']['published_at']='2026-10-30T00:00:00Z'
    return base_cases+[direct,unsupported,inferred]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--case',action='append',choices=[c['name'] for c in cases()],help='Run only these named calibration cases');args=parser.parse_args()
    config=load_config(args.config);policy,_=load_inputs(config)
    report={'synthetic':True,'approved':False,'cases':[]}
    with FoundryBackend(config) as backend:
        for item in cases():
            if args.case and item['name'] not in args.case:continue
            payload=item['payload']|{'policy':policy.model_dump()}
            result=backend.generate(stage='audit',system=AUDIT,payload=payload,response_model=SemanticAudit,
                validate=lambda value:validate_semantic(value,{r.rule_id for r in policy.rules},{'s0'},{'f0'}))
            report['cases'].append({'name':item['name'],'expected':item['expected'],'decision':result.decision,
                'matched':result.decision in item['expected'],'result':result.model_dump()})
            args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
    report['passed']=all(c['matched'] for c in report['cases']);args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'cases':[{k:c[k] for k in ('name','decision','matched')} for c in report['cases']]}))
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
