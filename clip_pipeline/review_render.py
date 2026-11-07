"""Explicit quarantine renders never release a withheld selection or permit approval."""
import json
from pathlib import Path

from .artifacts import ArtifactError, ArtifactStore, digest, read_json_model
from .contracts import StoryPlan, StorySequence, SourceArtifact, TranscriptArtifact
from .selection_contracts import ProposalResponse
from .stories import validate_plan_config, validate_plan_transcript


def review_inputs_for(config, run_id):
    from .runner import inspect_run
    from .selection import _story_spec
    store = ArtifactStore(Path(config.workspace))
    manifest = inspect_run(store.workspace, run_id)
    if manifest.execution_kind != 'topic_selection' or manifest.stage != 'selected':
        raise ArtifactError('review rendering requires a completed selection')
    directory = store.run_dir(run_id)
    def path(key):
        return directory / manifest.artifacts[key].path
    original = read_json_model(path('story_plan'), StoryPlan)
    report = json.loads(path('selection_report').read_text())
    if report['outcome'] != 'needs_review' or not report['coverage_complete']:
        raise ArtifactError('review rendering requires held proposals with complete source coverage')
    sequences = [StorySequence(sequence_id=q.sequence_id, title=q.title,
                  stories=[_story_spec(s) for s in q.stories])
                 for response in report['proposals']
                 for q in ProposalResponse.model_validate(response).sequences]
    if not sequences:
        raise ArtifactError('no held proposals to render')
    plan = StoryPlan.model_validate(original.model_dump() | {'sequences': sequences, 'review_only': True})
    validate_plan_config(plan, config)
    validate_plan_transcript(plan, read_json_model(path('source'), SourceArtifact),
                             read_json_model(path('transcript'), TranscriptArtifact), config)
    return plan, path('transcript'), {
        'selection_review_report': ('selection-review-report.json', report),
        'selection_review_manifest': ('selection-review-manifest.json', manifest)}


def validate_review_binding(plan, manifest, directory):
    if not plan.review_only:
        return
    ref = manifest.artifacts.get('selection_review_report')
    if ref is None or 'selection_review_report' not in (manifest.input_artifact_keys or []):
        raise ArtifactError('review-only render requires its pinned selection report')
    report = json.loads((directory / ref.path).read_text())
    if digest(report) != plan.selection_binding.get('report') or report.get('outcome') != 'needs_review':
        raise ArtifactError('review-only selection report mismatch')


def review_only_issues(plan):
    if not plan.review_only:
        return []
    from .audit_contracts import AuditIssue
    return [AuditIssue(issue_id='selection_review_hold', rule_id='selection_gate',
        severity='reject', kind='context', source_segment_ids=[], final_segment_ids=[],
        explanation='Review-only render of withheld proposals. Selection findings remain unresolved; '
                    'this diagnostic media cannot be approved or published. Resolve selection and render a new plan.')]
