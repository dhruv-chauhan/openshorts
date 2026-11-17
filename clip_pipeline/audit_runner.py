"""Immutable audit imports, media inspection, durable inference, and reviewer packets."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from uuid import uuid4

from . import __version__
from .artifacts import ArtifactError, ArtifactStore, atomic_write, digest, json_bytes, read_json_model
from .config import load_config, load_inputs, PipelineConfig
from .contracts import RunManifest, StoryPlan, SourceArtifact, TranscriptArtifact, MarketSnapshot, TopicPolicy, StoryArtifact, SequenceManifest
from .audit_contracts import AuditBundle, MediaInspection, SemanticAudit, ItemAudit, AuditIssue
from .audit import AUDIT, PROMPT_VERSION, audit_item, source_context, selected_source, final_segments, display_metadata, metadata_issues
from .foundry import FoundryBackend
from .markets import fetch_markets
from .media_common import reference, disk_check
from .execution import _worker, termination
from .stories import all_stories, validate_story_checkpoint, validate_plan_transcript, validate_plan_config


def audit_prompts(config):
    if config.asset_policy is not None:
        from .asset_audit import prompt_snapshot
        return prompt_snapshot()
    return {'version': PROMPT_VERSION, 'audit': AUDIT}


def prepare_audit(config_path, render_workspace, render_run_id):
    config=load_config(config_path);policy,_=load_inputs(config)
    original_store=ArtifactStore(Path(render_workspace))
    original=original_store.read_manifest(render_run_id)
    if original.execution_kind!='manual_stories' or original.state!='checkpointed' or original.stage!='rendered':
        raise ArtifactError('audit requires a complete separate-story render checkpoint')
    original_dir=original_store.run_dir(render_run_id)
    original_config=read_json_model(original_dir/original.artifacts['config'].path,PipelineConfig)
    # Read-only import is portable between host/container paths; never resumes or mutates the render.
    validate_story_checkpoint(original,original_dir,original_config)
    plan=read_json_model(original_dir/original.artifacts['story_plan'].path,StoryPlan)
    validate_plan_config(plan,config)
    if not plan.sequences:raise ArtifactError('empty plans cannot enter final-media approval')
    store=ArtifactStore(Path(config.workspace))
    with store.lock():
        run_id=uuid4().hex;directory=store.run_dir(run_id);directory.mkdir(parents=True)
        markets,provenance=fetch_markets(config.markets,store.workspace/'cache/markets')
        inputs={'config':config,'policy':policy,'submission':original.source,'markets':markets,
                'market_fetch':provenance,'render_manifest':original,
                'audit_prompts':audit_prompts(config)}
        refs={k:store.write_artifact(run_id,k+'.json',v) for k,v in inputs.items()}
        needed={k for k in original.artifacts if k not in {'source_media','supplied_transcript'}}
        total=sum(original.artifacts[k].size_bytes for k in needed)
        disk_check(directory,config.limits.min_free_disk_bytes+total)
        for key in sorted(needed):
            old=original.artifacts[key];target=directory/'render'/old.path;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(original_dir/old.path,target)
            ref=reference(target,directory)
            if ref.sha256!=old.sha256 or ref.size_bytes!=old.size_bytes:raise ArtifactError('render changed during audit import')
            refs['render.'+key]=ref
        now=datetime.now(timezone.utc)
        manifest=RunManifest(implementation_version=__version__,run_id=run_id,created_at=now,updated_at=now,
            execution_kind='story_audit',source=original.source,artifacts=refs,input_artifact_keys=list(refs),
            input_fingerprint=digest({k:v.sha256 for k,v in refs.items()}),reason_code='audit_prepared')
        atomic_write(directory/'manifest.json',json_bytes(manifest))
        return manifest,directory


def audit_binding(manifest):
    return {k:manifest.artifacts[k].sha256 for k in [*manifest.input_artifact_keys,
            *sorted(k for k in manifest.artifacts if k.startswith('inspection.')),
            *(['release_plan'] if 'release_plan' in manifest.artifacts else [])]}


def audit_inputs(manifest,directory):
    def read(key,model):return read_json_model(directory/manifest.artifacts[key].path,model)
    return (read('config',PipelineConfig),read('render.story_plan',StoryPlan),read('render.source',SourceArtifact),
            read('render.transcript',TranscriptArtifact),read('policy',TopicPolicy),read('markets',MarketSnapshot))


def execute_audit(workspace,run_id,*,backend_factory=FoundryBackend,worker=_worker,media_only=False):
    from .runner import inspect_run
    store=ArtifactStore(Path(workspace))
    with store.lock(), termination():
        manifest=inspect_run(store.workspace,run_id)
        if manifest.execution_kind!='story_audit':raise ArtifactError('not an audit run')
        if manifest.stage in {'audited','reviewed','stored'}:return manifest
        directory=store.run_dir(run_id)
        def path(key):return directory/manifest.artifacts[key].path
        config,plan,source,transcript,policy,markets=audit_inputs(manifest,directory)
        validate_plan_transcript(plan,source,transcript,config)
        if json.loads(path('audit_prompts').read_text())!=audit_prompts(config):
            raise ArtifactError('audit implementation changed; prepare a new audit')
        def save(**changes):
            nonlocal manifest
            manifest=RunManifest.model_validate(manifest.model_dump()|changes|{'updated_at':datetime.now(timezone.utc)})
            atomic_write(directory/'manifest.json',json_bytes(manifest))
        def artifact(key,value):
            save(artifacts=manifest.artifacts|{key:store.write_artifact(run_id,key+'-'+uuid4().hex+'.json',value)})
        save(state='running',stage='rendered',outcome=None,reason_code='audit_started')
        try:
            inspections={}
            for story in all_stories(plan):
                key='inspection.'+story.story_id
                if key not in manifest.artifacts:
                    target=read_json_model(path('render.story.'+story.story_id+'.story'),StoryArtifact)
                    output=worker('audit-story',directory/'stages'/('inspect-'+uuid4().hex),path('config'),config,
                        media=directory/'render'/target.final.media.path,request=path('render.story.'+story.story_id+'.story'),
                        source=directory/'render')
                    if 'inspection' not in output:raise ArtifactError('missing final-media inspection')
                    inspection=read_json_model(output['inspection'],MediaInspection)
                    if inspection.story_id!=story.story_id or inspection.final_sha256!=target.final.media.sha256:
                        raise ArtifactError('inspection targets different media')
                    if inspection.transcript and inspection.transcript.source_sha256!=target.final.media.sha256:
                        raise ArtifactError('final ASR is not bound to delivered bytes')
                    artifact(key,inspection)
                inspections[story.story_id]=read_json_model(path(key),MediaInspection)
            if media_only:
                save(state='interrupted',reason_code='audit_media_ready')
                return manifest
            usage=json.loads(path('usage').read_text()) if 'usage' in manifest.artifacts else dict(reserved_calls=0,reserved_tokens=0,attempts=[])
            previous=usage['attempts']
            def sink(calls,tokens,attempts):artifact('usage',dict(reserved_calls=calls,reserved_tokens=tokens,attempts=previous+attempts))
            knowledge=json.loads(path('render.enrichment').read_text()) if 'render.enrichment' in manifest.artifacts else None
            previous_markets=read_json_model(path('render.markets'),MarketSnapshot) if 'render.markets' in manifest.artifacts else None
            with backend_factory(config,prior_calls=usage['reserved_calls'],prior_tokens=usage['reserved_tokens'],budget_sink=sink) as backend:
                def call(stage,system,payload,model,validate):
                    key='audit_call.'+digest({'stage':stage,'system':system,'payload':payload,'schema':model.model_json_schema()})
                    if key in manifest.artifacts:
                        value=read_json_model(path(key),model);validate(value);return value
                    value=backend.generate(stage=stage,system=system,payload=payload,response_model=model,validate=validate)
                    artifact(key,value);return value
                story_results=[];sequence_results=[]
                shared={'markets':markets.model_dump(),'knowledge':knowledge,'published_at':source.published_at.isoformat() if source.published_at else None}
                for story in all_stories(plan):
                    target=read_json_model(path('render.story.'+story.story_id+'.story'),StoryArtifact)
                    inspection=inspections[story.story_id]
                    payload=shared|{'proposed_metadata_not_evidence':display_metadata(story),
                        'source_context':source_context(transcript,story.span.start,story.span.end,config.audit.context_padding_seconds),
                        'selected_source':selected_source(transcript,story.span.start,story.span.end),
                        'final_audio':final_segments(inspection),'caption_sidecar':inspection.captions_text}
                    from .review_render import review_only_issues
                    from .corrections import correction_issues
                    deterministic=[*inspection.issues,*metadata_issues(story,markets,knowledge,previous_markets),
                                   *review_only_issues(plan),*correction_issues(transcript,story.span.start,story.span.end)]
                    if config.asset_policy is not None:
                        from .asset_audit import relation_issues
                        deterministic += relation_issues(story, transcript, markets, config)
                    if not inspection.transcript:
                        deterministic.append(AuditIssue(issue_id='final_asr_missing',rule_id='audit_complete',severity='reject',kind='media',
                            explanation='Final audio could not be audited; correct media and rerun.',source_segment_ids=[],final_segment_ids=[]))
                        result=ItemAudit(item_id=story.story_id,kind='story',target_sha256=digest(target),
                            semantic=SemanticAudit(decision='pass',rules=[],issues=[],summary='Semantic audit unavailable.'),deterministic_issues=deterministic)
                    elif config.asset_policy is not None:
                        from .asset_audit import story_audit
                        result = story_audit(story, target, inspection, transcript, markets, config, call, deterministic)
                    else:
                        result=audit_item('story',story.story_id,target,payload,policy,call,deterministic)
                    story_results.append(result)
                if config.asset_policy is not None:
                    from .asset_audit import release_sequences, sequence_audit
                    release = release_sequences(plan, story_results)
                    artifact('release_plan', release)
                for seq in plan.sequences:
                    target=read_json_model(path('render.sequence.'+seq.sequence_id),SequenceManifest)
                    if config.asset_policy is not None:
                        sequence_results.append(sequence_audit(seq, target, story_results, plan))
                        continue
                    payload=shared|{'proposed_metadata_not_evidence':[display_metadata(s) for s in seq.stories],
                        'source_context':source_context(transcript,seq.stories[0].span.start,seq.stories[-1].span.end,config.audit.context_padding_seconds),
                        'omitted_source_intervals':[{'start':a.span.end,'end':b.span.start} for a,b in zip(seq.stories,seq.stories[1:]) if b.span.start>a.span.end],
                        'final_audio':[x for s in seq.stories for x in final_segments(inspections[s.story_id],s.story_id+':')],
                        'caption_sidecars':{s.story_id:inspections[s.story_id].captions_text for s in seq.stories}}
                    deterministic=review_only_issues(plan) + correction_issues(transcript,seq.stories[0].span.start,seq.stories[-1].span.end)
                    if any(not inspections[s.story_id].transcript for s in seq.stories):
                        deterministic += [AuditIssue(issue_id='sequence_audio_incomplete',rule_id='audit_complete',severity='reject',kind='media',
                            explanation='One or more final story transcripts are missing.',source_segment_ids=[],final_segment_ids=[])]
                    result=audit_item('sequence',seq.sequence_id,target,payload,policy,call,deterministic)
                    sequence_results.append(result)
                if config.asset_policy is not None:
                    originals = {q.sequence_id for q in plan.sequences}
                    for seq in release.sequences:
                        if seq.sequence_id in originals: continue
                        stories = [read_json_model(path('render.story.'+s.story_id+'.story'), StoryArtifact) for s in seq.stories]
                        target = SequenceManifest(plan_sha256=digest(release), source_sha256=plan.source_sha256,
                            transcript_sha256=plan.transcript_sha256, sequence=seq, stories=stories,
                            duration_seconds=sum(s.final.duration_seconds for s in stories))
                        artifact('render.sequence.'+seq.sequence_id, target)
                        sequence_results.append(sequence_audit(seq, target, story_results, plan))
            bundle=AuditBundle(binding=audit_binding(manifest),stories=story_results,sequences=sequence_results)
            artifact('audit_bundle',bundle)
            from .review import review_template
            from .report import audit_preview
            if config.asset_policy is None:
                artifact('review_template',review_template(manifest,bundle))
            preview=directory/('audit-preview-'+uuid4().hex+'.html')
            atomic_write(preview,audit_preview(plan,bundle,manifest,directory).encode())
            save(artifacts=manifest.artifacts|{'preview':reference(preview,directory)},state='checkpointed',stage='audited',
                 reason_code='policy_decision_ready' if config.asset_policy is not None else 'human_review_required')
            return inspect_run(store.workspace,run_id)
        except KeyboardInterrupt:
            save(state='interrupted',outcome=None,reason_code='audit_interrupted');raise
        except Exception:
            save(state='retryable',outcome=None,reason_code='audit_failed');raise


def validate_audit_checkpoint(manifest,directory,config):
    if manifest.stage not in {'audited','reviewed','stored'}:return
    if 'audit_bundle' not in manifest.artifacts:raise ArtifactError('missing audit bundle')
    bundle=read_json_model(directory/manifest.artifacts['audit_bundle'].path,AuditBundle)
    if bundle.binding!=audit_binding(manifest):raise ArtifactError('audit input binding mismatch')
    _,plan,_,transcript,_,_=audit_inputs(manifest,directory)
    sequences = list(plan.sequences)
    if config.asset_policy is not None:
        from .asset_audit import validate_release
        release = validate_release(manifest, directory, bundle, plan)
        original_ids = {q.sequence_id for q in sequences}
        sequences += [q for q in release.sequences if q.sequence_id not in original_ids]
    for kind,items,expected in [('story',bundle.stories,{s.story_id for s in all_stories(plan)}),
                               ('sequence',bundle.sequences,{q.sequence_id for q in sequences})]:
        if len(items)!=len(expected) or {i.item_id for i in items}!=expected:raise ArtifactError('incomplete audit coverage')
        for item in items:
            from .review_render import review_only_issues
            if any(issue not in item.deterministic_issues for issue in review_only_issues(plan)):
                raise ArtifactError('review-only audit hold missing')
            from .corrections import correction_issues
            targets = {s.story_id:(s.span.start,s.span.end) for s in all_stories(plan)} if kind=='story' else {
                q.sequence_id:(q.stories[0].span.start,q.stories[-1].span.end) for q in sequences}
            if (kind == 'story' or config.asset_policy is None) and any(issue not in item.deterministic_issues for issue in correction_issues(transcript,*targets[item.item_id])):
                raise ArtifactError('transcript correction review missing')
            key='render.story.'+item.item_id+'.story' if kind=='story' else 'render.sequence.'+item.item_id
            model=StoryArtifact if kind=='story' else SequenceManifest
            if item.kind!=kind or item.target_sha256!=digest(read_json_model(directory/manifest.artifacts[key].path,model)):
                raise ArtifactError('audit target mismatch')
    if manifest.stage in {'reviewed','stored'}:
        if config.asset_policy is not None:
            from .asset_approval import validate_decision
            from .asset_contracts import AutomatedDecision
            decision = read_json_model(directory/manifest.artifacts['automated_decision'].path, AutomatedDecision)
            validate_decision(manifest, bundle, decision, plan, config, directory)
            return
        from .review import validate_review
        from .audit_contracts import HumanReview
        review=read_json_model(directory/manifest.artifacts['human_review'].path,HumanReview)
        validate_review(manifest,bundle,review,plan)
