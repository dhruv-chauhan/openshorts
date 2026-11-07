"""Human review bound to exact audits, media, ordering and displayed metadata."""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from .artifacts import ArtifactError, ArtifactStore, atomic_write, digest, json_bytes, read_json_model
from .audit_contracts import AuditBundle, HumanReview
from .contracts import RunManifest, StoryPlan


def review_template(manifest,bundle):
    return dict(schema_version='1',run_id=manifest.run_id,audit_sha256=digest(bundle),reviewer_kind='human',
        reviewer='',reviewed_at=None,items=[dict(item_id=i.item_id,kind=i.kind,target_sha256=i.target_sha256,
            decision='pending',watched_entire_item=False,resolutions=[],notes='') for i in [*bundle.stories,*bundle.sequences]])


def validate_review(manifest,bundle,review,plan):
    if review.run_id!=manifest.run_id or review.audit_sha256!=digest(bundle):
        raise ArtifactError('review does not match this audit')
    if review.reviewed_at < manifest.created_at or review.reviewed_at > datetime.now(timezone.utc):
        raise ArtifactError('invalid review timestamp')
    expected={(i.kind,i.item_id):i for i in [*bundle.stories,*bundle.sequences]}
    seen=set();approved=set()
    for item in review.items:
        key=(item.kind,item.item_id)
        if key in seen or key not in expected:raise ArtifactError('unknown or duplicate review item')
        seen.add(key);audit=expected[key]
        if item.target_sha256!=audit.target_sha256:raise ArtifactError('review target changed')
        issues={i.issue_id:i for i in [*audit.semantic.issues,*audit.deterministic_issues]}
        if len(issues)!=len(audit.semantic.issues)+len(audit.deterministic_issues):raise ArtifactError('ambiguous audit issue IDs')
        resolved={r.issue_id for r in item.resolutions}
        if len(resolved)!=len(item.resolutions) or not resolved.issubset(issues):raise ArtifactError('invalid issue resolution')
        if item.decision=='approve':
            if plan.review_only:raise ArtifactError('review-only renders cannot be approved; resolve selection and create a new render')
            if not item.watched_entire_item:raise ArtifactError('full visual/audio review required')
            if audit.decision=='reject':raise ArtifactError('hard audit failure cannot be overridden')
            if resolved!={i.issue_id for i in issues.values() if i.severity=='review'}:
                raise ArtifactError('every review issue needs an evidence-backed resolution')
            approved.add(key)
    if seen!=set(expected):raise ArtifactError('review must account for every story and sequence')
    for sequence in plan.sequences:
        if ('sequence',sequence.sequence_id) in approved and any(('story',s.story_id) not in approved for s in sequence.stories):
            raise ArtifactError('sequence approval requires every planned story approval')
    return sorted(item_id for kind,item_id in approved if kind=='sequence')


def apply_review(workspace,run_id,review_path):
    from .runner import inspect_run
    store=ArtifactStore(Path(workspace))
    with store.lock():
        manifest=inspect_run(store.workspace,run_id);directory=store.run_dir(run_id)
        if manifest.execution_kind!='story_audit' or manifest.stage not in {'audited','reviewed'}:
            raise ArtifactError('review requires a completed audit and cannot change published output')
        if 'promotion_plan' in manifest.artifacts:
            raise ArtifactError('publication already started; revised decisions require a new audit run')
        bundle=read_json_model(directory/manifest.artifacts['audit_bundle'].path,AuditBundle)
        plan=read_json_model(directory/manifest.artifacts['render.story_plan'].path,StoryPlan)
        review=read_json_model(review_path,HumanReview)
        validate_review(manifest,bundle,review,plan)
        ref=store.write_artifact(run_id,'human-review-'+uuid4().hex+'.json',review)
        updated=RunManifest.model_validate(manifest.model_dump()|{'artifacts':manifest.artifacts|{'human_review':ref},
            'stage':'reviewed','state':'checkpointed','outcome':None,'reason_code':'human_review_recorded',
            'updated_at':datetime.now(timezone.utc)})
        atomic_write(directory/'manifest.json',json_bytes(updated))
        return inspect_run(store.workspace,run_id)
