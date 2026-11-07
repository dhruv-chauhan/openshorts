"""Private, immutable Blob objects; consumers discover only commit manifests."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4

from .artifacts import ArtifactError, ArtifactStore, atomic_write, digest, json_bytes, read_json_model
from .audit_contracts import AuditBundle, HumanReview
from .audit_runner import audit_inputs
from .audit import display_metadata
from .contracts import RunManifest, StoryArtifact, SequenceManifest
from .enrichment import temporal_reasons
from .enrichment_contracts import KnowledgeSnapshot
from .markets import fetch_markets
from .review import validate_review
from .execution import termination


class StorageError(RuntimeError):
    pass


class BlobSink:
    def __init__(self, config, *, client=None):
        self.config=config;self.credential=None;self.owns_client=client is None
        if config.blob is None:raise ArtifactError('Blob destination must be configured')
        if client is None:
            from azure.identity import AzureCliCredential, ManagedIdentityCredential
            from azure.storage.blob import BlobServiceClient
            self.credential=(AzureCliCredential() if config.foundry.auth_mode=='developer_identity' else
                ManagedIdentityCredential(client_id=config.foundry.managed_identity_client_id))
            client=BlobServiceClient(config.blob.account_url,credential=self.credential,retry_total=3,
                connection_timeout=15,read_timeout=60,logging_enable=False)
        self.client=client

    def __enter__(self):return self
    def __exit__(self,*args):
        if self.owns_client:self.client.close()
        if self.credential:self.credential.close()

    def ensure_private(self, container):
        from azure.core.exceptions import ResourceExistsError
        client=self.client.get_container_client(container)
        try:client.create_container()
        except ResourceExistsError:pass
        if client.get_container_properties().get('public_access') is not None:
            raise StorageError('destination_container_is_public')

    def put(self,container,key,*,path=None,data=None,sha256,size_bytes):
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings
        client=self.client.get_blob_client(container,key)
        content_type='video/mp4' if key.endswith('.mp4') else 'application/json' if key.endswith('.json') else 'text/plain'
        stream=path.open('rb') if path is not None else io.BytesIO(data)
        try:
            try:client.upload_blob(stream,overwrite=False,metadata={'sha256':sha256},length=size_bytes,
                validate_content=True,content_settings=ContentSettings(content_type=content_type))
            except ResourceExistsError:pass
        finally:stream.close()
        # Do not trust a caller-supplied metadata checksum or a partial upload.
        h=hashlib.sha256();size=0
        for chunk in client.download_blob().chunks():h.update(chunk);size+=len(chunk)
        if h.hexdigest()!=sha256 or size!=size_bytes:raise StorageError('remote_content_mismatch')


def promotion_check(config, markets, plan, knowledge, cache, *, fetch=fetch_markets, now=None):
    now=now or datetime.now(timezone.utc)
    settings=config.markets
    if settings.mode=='api':settings=settings.model_copy(update={'max_cache_age_seconds':0})
    current,meta=fetch(settings,cache)
    old={m.market_id:m for m in markets.markets};new={m.market_id:m for m in current.markets}
    ids={i for q in plan.sequences for s in q.stories for i in s.matched_market_ids}
    ids.update(a.market_id for q in plan.sequences for s in q.stories if s.enrichment
               for a in s.enrichment.associations if a.decision=='supported')
    if any(i not in old or i not in new or not new[i].enabled or old[i]!=new[i] for i in ids):
        raise ArtifactError('affected market eligibility changed; new audit and review required')
    if knowledge is not None:
        from .enrichment import load_knowledge
        latest=load_knowledge(config.enrichment)
        if latest is None or digest(latest)!=digest(knowledge):raise ArtifactError('knowledge changed; new audit required')
        records={m.record_id:m for e in knowledge.entities for m in e.memberships}
        records.update({r.record_id:r for r in knowledge.relationships})
        for q in plan.sequences:
            for s in q.stories:
                if not s.enrichment:continue
                for a in s.enrichment.associations:
                    if a.decision!='supported' or a.basis=='direct':continue
                    if any(key not in records or temporal_reasons(records[key],s.enrichment.classification,
                            config.enrichment,now.date().isoformat(),relationship=a.basis=='relationship') for key in a.knowledge_record_ids):
                        raise ArtifactError('supported knowledge expired/conflicted; new audit required')
    return current,meta


def promote(workspace,run_id,*,sink_factory=BlobSink,fetch=fetch_markets):
    from .runner import inspect_run
    store=ArtifactStore(Path(workspace))
    with store.lock(), termination():
        manifest=inspect_run(store.workspace,run_id);directory=store.run_dir(run_id)
        if manifest.execution_kind!='story_audit' or manifest.stage not in {'reviewed','stored'}:
            raise ArtifactError('promotion requires a completed human review')
        if manifest.stage=='stored':return manifest
        def read(key,model):return read_json_model(directory/manifest.artifacts[key].path,model)
        config,plan,source,transcript,policy,markets=audit_inputs(manifest,directory)
        if config.blob is None:raise ArtifactError('Blob destination must be configured')
        bundle=read('audit_bundle',AuditBundle);review=read('human_review',HumanReview)
        approved=validate_review(manifest,bundle,review,plan)
        if not approved:raise ArtifactError('no fully approved sequences to promote')
        approved_plan=plan.model_copy(update={'sequences':[s for s in plan.sequences if s.sequence_id in approved]})
        knowledge=read('render.enrichment',KnowledgeSnapshot) if 'render.enrichment' in manifest.artifacts else None
        current,refresh=promotion_check(config,markets,approved_plan,knowledge,store.workspace/'cache/promotion-markets',fetch=fetch)
        model_provenance={'provider':config.foundry.provider,
            'deployments':{stage:getattr(config.foundry,stage+'_deployment') for stage in ('relevance','selection','audit')},
            'audit_prompts_sha256':manifest.artifacts['audit_prompts'].sha256,
            'audit_config_sha256':manifest.artifacts['config'].sha256,
            'render_config_sha256':manifest.artifacts['render.config'].sha256}
        def save(**changes):
            nonlocal manifest
            manifest=RunManifest.model_validate(manifest.model_dump()|changes|{'updated_at':datetime.now(timezone.utc)})
            atomic_write(directory/'manifest.json',json_bytes(manifest))
        if 'promotion_plan' not in manifest.artifacts:
            prefix='approved/'+source.media_sha256+'/'+run_id+'/'+digest(review)
            objects=[];sequences=[]
            def content(key,value):
                raw=json_bytes(value);ref={'key':key,'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)}
                objects.append(ref|{'value':value});return ref
            for seq in plan.sequences:
                if seq.sequence_id not in approved:continue
                stories=[]
                for story in seq.stories:
                    artifact=read('render.story.'+story.story_id+'.story',StoryArtifact)
                    key=prefix+'/stories/'+story.story_id+'/clip.mp4'
                    ref={'key':key,'sha256':artifact.final.media.sha256,'size_bytes':artifact.final.media.size_bytes}
                    objects.append(ref|{'artifact_key':'render.story.'+story.story_id+'.final_media'})
                    captions=None
                    if artifact.final.captions:
                        captions={'key':prefix+'/stories/'+story.story_id+'/captions.ass',
                                  'sha256':artifact.final.captions.sha256,'size_bytes':artifact.final.captions.size_bytes}
                        objects.append(captions|{'artifact_key':'render.story.'+story.story_id+'.captions_ass'})
                    stories.append({'story_id':story.story_id,'media':ref,'captions':captions,'metadata':display_metadata(story),
                        'duration_seconds':artifact.final.duration_seconds,'playback_rate':story.playback_rate,
                        'source_span':story.span.model_dump(),'story_audit':next(i.model_dump() for i in bundle.stories if i.item_id==story.story_id)})
                seq_value={'schema_version':'1','approved':True,'sequence_id':seq.sequence_id,'title':seq.title,
                    'source':source.model_dump(mode='json'),'ordered_stories':stories,
                    'audit_sha256':digest(bundle),'review_sha256':digest(review),
                    'sequence_audit':next(i.model_dump() for i in bundle.sequences if i.item_id==seq.sequence_id),
                    'model_provenance':model_provenance,
                    'knowledge_sha256':digest(knowledge) if knowledge else None,'policy_sha256':digest(policy)}
                sequences.append(content(prefix+'/sequences/'+seq.sequence_id+'.json',seq_value))
            review_ref=content(prefix+'/review.json',review.model_dump(mode='json'))
            commit={'schema_version':'1','approved':True,'run_id':run_id,'source_sha256':source.media_sha256,
                'sequences':sequences,'review':review_ref,'audit_sha256':digest(bundle),
                'market_snapshot_sha256':digest(current),'market_mode':config.markets.mode,
                'development_market_universe':config.markets.mode!='api',
                'excluded_sequence_ids':[q.sequence_id for q in plan.sequences if q.sequence_id not in approved]}
            publication={'objects':objects,'commit':commit,'commit_key':prefix+'/commit.json',
                         'audit_sha256':digest(bundle),'review_sha256':digest(review),'market_check':refresh}
            ref=store.write_artifact(run_id,'promotion-plan-'+uuid4().hex+'.json',publication)
            save(artifacts=manifest.artifacts|{'promotion_plan':ref})
        publication=json.loads((directory/manifest.artifacts['promotion_plan'].path).read_text())
        if publication['audit_sha256']!=digest(bundle) or publication['review_sha256']!=digest(review):
            raise ArtifactError('promotion cannot mix audit/review revisions')
        save(state='running',reason_code='promotion_started')
        try:
            with sink_factory(config) as sink:
                sink.ensure_private(config.blob.runs_container);sink.ensure_private(config.blob.approved_container)
                # Store provenance in the run container without exporting credentials/config.
                evidence=json_bytes({'audit':bundle.model_dump(),'review':review.model_dump(mode='json'),
                    'policy':policy.model_dump(),'markets':markets.model_dump(),
                    'knowledge':knowledge.model_dump() if knowledge else None,
                    'model_provenance':model_provenance,
                    'final_media_inspections':{s.story_id:json.loads((directory/manifest.artifacts['inspection.'+s.story_id].path).read_text())
                                              for q in approved_plan.sequences for s in q.stories},
                    'source_transcript':transcript.model_dump(),'source':source.model_dump(mode='json')})
                sink.put(config.blob.runs_container,'runs/'+run_id+'/audit-'+digest(bundle)+'.json',data=evidence,
                         sha256=hashlib.sha256(evidence).hexdigest(),size_bytes=len(evidence))
                for obj in publication['objects']:
                    kwargs={'path':directory/manifest.artifacts[obj['artifact_key']].path} if 'artifact_key' in obj else {'data':json_bytes(obj['value'])}
                    sink.put(config.blob.approved_container,obj['key'],sha256=obj['sha256'],size_bytes=obj['size_bytes'],**kwargs)
                # Check again after large uploads, immediately before the discovery marker.
                promotion_check(config,markets,approved_plan,knowledge,store.workspace/'cache/promotion-markets',fetch=fetch)
                # Rehash local inputs before publishing; partial or changed media never becomes discoverable.
                inspect_run(store.workspace,run_id)
                raw=json_bytes(publication['commit'])
                sink.put(config.blob.approved_container,publication['commit_key'],data=raw,
                         sha256=hashlib.sha256(raw).hexdigest(),size_bytes=len(raw))
            receipt=store.write_artifact(run_id,'publication-'+uuid4().hex+'.json',dict(
                container=config.blob.approved_container,key=publication['commit_key'],sha256=hashlib.sha256(raw).hexdigest()))
            save(artifacts=manifest.artifacts|{'publication':receipt},stage='stored',state='finished',outcome='completed',reason_code='approved_sequences_stored')
            return inspect_run(store.workspace,run_id)
        except KeyboardInterrupt:
            save(state='interrupted',outcome=None,reason_code='promotion_interrupted')
            raise
        except Exception as exc:
            save(state='retryable',outcome=None,reason_code='promotion_failed')
            if isinstance(exc,(ArtifactError,StorageError)):raise
            raise StorageError('storage_operation_failed') from None
