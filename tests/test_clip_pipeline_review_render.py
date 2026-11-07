"""Held previews carry a permanent approval block, not a selection override."""
import pytest
from pathlib import Path
from clip_pipeline.artifacts import ArtifactError, read_json_model, digest
from clip_pipeline.contracts import StoryPlan
from clip_pipeline.runner import prepare_run, inspect_run
from clip_pipeline.review_render import review_only_issues
from clip_pipeline.review import validate_review
from test_clip_pipeline_config import config_data  # noqa: F401
from test_clip_pipeline_media import media_case  # noqa: F401
from test_clip_pipeline_stories import story_case  # noqa: F401
from test_clip_pipeline_selection_reuse import parent_case
from test_clip_pipeline_audit import audit_case, finish, bundle_for, human_decisions  # noqa: F401


def test_held_plan_stays_empty_and_preview_pins_original_report(media_case,tmp_path):
    config,paths,parent,d,_,_=parent_case(media_case,tmp_path,reject_rule='material_numbers')
    before=(d/'manifest.json').read_bytes()
    original=read_json_model(d/parent.artifacts['story_plan'].path,StoryPlan)
    assert not original.sequences
    render,rd=prepare_run(paths[0],review_selection_run_id=parent.run_id)
    plan=read_json_model(rd/render.artifacts['story_plan'].path,StoryPlan)
    assert plan.review_only and len(plan.sequences)==1 and not plan.approved
    assert plan.selection_binding==original.selection_binding
    assert render.artifacts['selection_review_report'].sha256==parent.artifacts['selection_report'].sha256
    assert inspect_run(Path(config.workspace),render.run_id)==render
    assert (d/'manifest.json').read_bytes()==before
    issues=review_only_issues(plan)
    assert len(issues)==1 and issues[0].severity=='reject'
    (rd/render.artifacts['selection_review_report'].path).write_text('{}')
    with pytest.raises(ArtifactError):inspect_run(Path(config.workspace),render.run_id)


def test_selected_plan_cannot_be_mislabeled_review_render(media_case,tmp_path):
    _,paths,parent,_,_,_=parent_case(media_case,tmp_path)
    with pytest.raises(ArtifactError,match='held proposals'):
        prepare_run(paths[0],review_selection_run_id=parent.run_id)


def test_even_passing_audit_cannot_approve_review_only_plan(audit_case):
    result=finish(audit_case);bundle=bundle_for(audit_case,result)
    plan=audit_case[3].model_copy(update={'review_only':True})
    review=human_decisions(result,bundle)
    with pytest.raises(ArtifactError,match='review-only'):
        validate_review(result,bundle,review,plan)


def test_default_plan_hash_preserves_old_checkpoints(story_case):
    plan=story_case[3]
    assert 'review_only' not in plan.model_dump()
    assert digest(plan)==digest(plan.model_dump())


def test_independent_audit_retains_hold_even_when_model_passes(audit_case,monkeypatch):
    from clip_pipeline import audit_runner
    original=audit_runner.audit_inputs
    def inputs(*args):
        config,plan,*rest=original(*args)
        return (config,plan.model_copy(update={'review_only':True}),*rest)
    monkeypatch.setattr(audit_runner,'audit_inputs',inputs)
    result=finish(audit_case);bundle=bundle_for(audit_case,result)
    for item in [*bundle.stories,*bundle.sequences]:
        assert item.semantic.decision=='pass'
        assert item.decision=='reject'
        assert any(i.issue_id=='selection_review_hold' for i in item.deterministic_issues)


def test_audit_validation_detects_removed_review_hold(audit_case,monkeypatch):
    from clip_pipeline import audit_runner
    result=finish(audit_case)
    original=audit_runner.audit_inputs
    def inputs(*args):
        config,plan,*rest=original(*args)
        return (config,plan.model_copy(update={'review_only':True}),*rest)
    monkeypatch.setattr(audit_runner,'audit_inputs',inputs)
    with pytest.raises(ArtifactError,match='hold missing'):
        inspect_run(audit_case[0],result.run_id)
