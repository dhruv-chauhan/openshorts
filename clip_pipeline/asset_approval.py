"""Explicit automated authority. Never creates a HumanReview or playback assertion."""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from .artifacts import ArtifactError, ArtifactStore, atomic_write, digest, json_bytes, read_json_model
from .asset_contracts import AutomatedDecision
from .audit_contracts import AuditBundle
from .contracts import RunManifest, StoryPlan
from .config import PipelineConfig
from .asset_audit import validate_release


def decision_inputs(manifest, bundle):
    return {'run_id': manifest.run_id, 'audit_sha256': digest(bundle),
            'policy_sha256': manifest.artifacts['policy'].sha256,
            'config_sha256': manifest.artifacts['config'].sha256,
            'targets': {i.kind+':'+i.item_id: i.target_sha256 for i in [*bundle.stories, *bundle.sequences]}}


def validate_decision(manifest, bundle, decision, plan, config, directory):
    if config.asset_policy is None or plan.review_only:
        raise ArtifactError('automatic approval is unavailable for this profile or quarantine')
    expected = decision_inputs(manifest, bundle)
    if any(getattr(decision, k) != v for k, v in expected.items()):
        raise ArtifactError('automated decision evidence changed')
    if decision.decided_at < manifest.created_at or decision.decided_at > datetime.now(timezone.utc):
        raise ArtifactError('invalid automated decision timestamp')
    release = validate_release(manifest, directory, bundle, plan)
    allowed = sorted(q.sequence_id for q in release.sequences)
    if sorted(decision.approved_sequence_ids) != allowed:
        raise ArtifactError('automated decision must match the evaluated release set')
    by_id = {i.item_id: i for i in bundle.stories}
    for q in release.sequences:
        for s in q.stories:
            if not s.matched_market_ids or not s.asset_relations or by_id[s.story_id].decision == 'reject':
                raise ArtifactError('cannot approve an unmapped or rejected story')
    return allowed


def apply_decision(workspace, run_id, decision_path):
    from .runner import inspect_run
    store = ArtifactStore(Path(workspace))
    with store.lock():
        manifest = inspect_run(store.workspace, run_id); directory = store.run_dir(run_id)
        if manifest.execution_kind != 'story_audit' or manifest.stage not in ('audited', 'reviewed'):
            raise ArtifactError('decision requires an unpublished completed audit')
        def read(key, model): return read_json_model(directory/manifest.artifacts[key].path, model)
        decision = read_json_model(decision_path, AutomatedDecision)
        if 'automated_decision' in manifest.artifacts:
            if read('automated_decision', AutomatedDecision) != decision:
                raise ArtifactError('automated decision is immutable')
            return manifest
        if 'promotion_plan' in manifest.artifacts: raise ArtifactError('publication already started')
        validate_decision(manifest, read('audit_bundle', AuditBundle), decision,
                          read('render.story_plan', StoryPlan), read('config', PipelineConfig), directory)
        ref = store.write_artifact(run_id, 'automated-decision-'+uuid4().hex+'.json', decision)
        updated = RunManifest.model_validate(manifest.model_dump() | {
            'artifacts': manifest.artifacts | {'automated_decision': ref}, 'stage': 'reviewed',
            'state': 'checkpointed', 'reason_code': 'automated_policy_decision_recorded',
            'updated_at': datetime.now(timezone.utc)})
        atomic_write(directory/'manifest.json', json_bytes(updated))
        return inspect_run(store.workspace, run_id)
