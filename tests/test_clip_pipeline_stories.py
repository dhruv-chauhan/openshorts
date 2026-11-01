"""Story boundaries, time mappings, immutable plans and per-story recovery."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from clip_pipeline.artifacts import ArtifactError, digest, json_bytes, read_json_model
from clip_pipeline.contracts import (StoryPlan, StorySpec, StoryArtifact, FinalArtifact,
                                     TranscriptArtifact, SequenceManifest)
from clip_pipeline.execution import execute_stories
from clip_pipeline.media_common import MediaError, reference
from clip_pipeline.runner import prepare_run, inspect_run
from clip_pipeline.stories import (validate_plan_config, validate_plan_transcript, output_time,
                                   story_transcript, retime_layout, story_timeline)
from clip_pipeline.report import story_preview
from test_clip_pipeline_config import config_data, config_path  # noqa: F401
from test_clip_pipeline_media import media_case  # noqa: F401


@pytest.fixture
def story_case(media_case, tmp_path):
    config, media, source, transcript = media_case
    data = transcript.model_dump()
    data['segments'].append({'segment_id': 's1', 'start': 6.0, 'end': 8.0,
        'text': 'Earnings also matter.', 'words': [
            {'word': 'Earnings', 'start': 6.0, 'end': 6.7},
            {'word': 'also', 'start': 6.7, 'end': 7.0},
            {'word': 'matter.', 'start': 7.0, 'end': 8.0}]})
    transcript = TranscriptArtifact.model_validate(data)
    plan = StoryPlan.model_validate({'source': source.origin.model_dump(), 'source_sha256': source.media_sha256,
        'transcript_sha256': digest(transcript), 'sequences': [{'sequence_id': 'discussion', 'title': 'Market context',
        'stories': [{'story_id': 'rates', 'title': 'Borrowing costs', 'span': {'start': 0.0, 'end': 4.0}},
                    {'story_id': 'earnings', 'title': 'Earnings', 'span': {'start': 5.0, 'end': 10.0},
                     'playback_rate': 1.1, 'evidence': [{'segment_id': 's1', 'start': 6.0, 'end': 8.0,
                                                       'quote': 'Earnings also matter.'}]}]}]})
    config_file, plan_file = tmp_path / 'config.json', tmp_path / 'plan.json'
    config_file.write_bytes(json_bytes(config)); plan_file.write_bytes(json_bytes(plan))
    return config, config_file, plan_file, plan, source, transcript


@pytest.mark.parametrize('mutation', ['duplicate', 'reversed', 'overlap', 'rate', 'nan', 'traversal'])
def test_invalid_story_plans(story_case, mutation):
    data = story_case[3].model_dump()
    stories = data['sequences'][0]['stories']
    if mutation == 'duplicate': stories[1]['story_id'] = stories[0]['story_id']
    if mutation == 'reversed': stories.reverse()
    if mutation == 'overlap': stories[1]['span']['start'] = 3.0
    if mutation == 'rate': stories[1]['playback_rate'] = 1.05
    if mutation == 'nan': stories[1]['span']['end'] = float('nan')
    if mutation == 'traversal': stories[1]['story_id'] = '../escape'
    with pytest.raises(ValidationError): StoryPlan.model_validate(data)


def test_limits_fail_before_workspace_and_short_complete_story_is_allowed(story_case):
    config, config_file, plan_file, plan, _, _ = story_case
    validate_plan_config(plan, config)  # first story is shorter than legacy selection.min_seconds
    data = config.model_dump(); data['stories']['max_stories_per_sequence'] = 1
    config_file.write_bytes(json_bytes(data))
    with pytest.raises(ArtifactError, match='count'):
        prepare_run(config_file, story_plan_path=plan_file)
    assert not Path(config.workspace).exists()


def test_hash_evidence_and_boundary_validation(story_case):
    config, _, _, plan, source, transcript = story_case
    validate_plan_transcript(plan, source, transcript, config)
    for field, code in [('source_sha256', 'story_source_hash_mismatch'),
                        ('transcript_sha256', 'story_transcript_hash_mismatch')]:
        with pytest.raises(MediaError, match=code):
            validate_plan_transcript(plan.model_copy(update={field: 'f' * 64}), source, transcript, config)
    data = plan.model_dump(); data['sequences'][0]['stories'][0]['span']['start'] = 0.1
    with pytest.raises(MediaError, match='inside_word'):
        validate_plan_transcript(StoryPlan.model_validate(data), source, transcript, config)
    data = plan.model_dump(); data['sequences'][0]['stories'][1]['evidence'][0]['quote'] = 'Invented'
    with pytest.raises(MediaError, match='quote_mismatch'):
        validate_plan_transcript(StoryPlan.model_validate(data), source, transcript, config)


def test_rate_maps_captions_layout_and_evidence_without_changing_source(story_case):
    _, _, _, plan, _, transcript = story_case
    before = digest(transcript)
    story = plan.sequences[0].stories[1]
    local = story_transcript(transcript, story)
    assert local['segments'][0]['words'][0]['start'] == pytest.approx(1 / 1.1)
    assert local['segments'][0]['words'][-1]['end'] == pytest.approx(3 / 1.1)
    assert retime_layout([{'start': 0.0, 'end': 5.0, 'layout': 'track'}], 1.1, 5/1.1)[0]['end'] == pytest.approx(5/1.1)
    timeline = story_timeline(story, transcript)
    assert timeline['evidence'][0]['output']['start'] == pytest.approx(1/1.1)
    assert digest(transcript) == before
    with pytest.raises(ValueError): output_time(4.0, story)


def fake_worker(story_case, monkeypatch, *, fail_second=False):
    _, _, _, plan, source, transcript = story_case
    calls = []
    fail = [fail_second]
    def worker(action, work, config_path, config, **inputs):
        work.mkdir(parents=True)
        calls.append((action, inputs.get('story_id')))
        if action == 'ingest':
            values = {'source': source, 'source_media': b'original media', 'ingest': {'reused': False}}
        elif action == 'transcribe':
            values = {'transcript': transcript, 'transcription': {'reused': False}}
        else:
            if inputs['story_id'] == 'earnings' and fail[0]:
                fail[0] = False
                raise MediaError('test_render_failed')
            story = next(s for seq in plan.sequences for s in seq.stories if s.story_id == inputs['story_id'])
            values = {'final_media': b'rendered', 'clean_media': b'clean', 'layout_ranges': [],
                      'timeline': story_timeline(story, transcript), 'render_report': {'approved': False}}
        files = {}
        for key, value in values.items():
            p = work / key; p.write_bytes(value if isinstance(value, bytes) else json_bytes(value)); files[key] = p
        if action == 'render-story':
            root = config_path.parent
            final = FinalArtifact(clip_id=story.story_id, source_sha256=source.media_sha256, source_span=story.span,
                media=reference(files['final_media'], root), audio_channels=2,
                duration_seconds=(story.span.end-story.span.start)/story.playback_rate)
            result = StoryArtifact(story=story, final=final,
                **{k: reference(files[k], root) for k in ('clean_media','layout_ranges','timeline','render_report')})
            for key, value in [('final', final), ('story', result)]:
                p = work / key; p.write_bytes(json_bytes(value)); files[key] = p
        return files
    monkeypatch.setattr('clip_pipeline.execution._worker', worker)
    return calls


def test_per_story_pause_failure_retry_and_pinned_inputs(story_case, monkeypatch):
    config, config_file, plan_file, plan, _, _ = story_case
    calls = fake_worker(story_case, monkeypatch, fail_second=True)
    manifest, directory = prepare_run(config_file, story_plan_path=plan_file)
    workspace = Path(config.workspace)
    paused = execute_stories(workspace, manifest.run_id, stop_after_stories=1)
    assert paused.state == 'interrupted' and paused.story_progress['rates'].state == 'rendered'
    assert 'sequence_index' not in paused.artifacts
    earlier = paused.artifacts['story.rates.final_media']
    with pytest.raises(MediaError, match='test_render_failed'): execute_stories(workspace, manifest.run_id)
    failed = inspect_run(workspace, manifest.run_id)
    assert failed.state == 'retryable' and failed.outcome is None
    assert failed.story_progress['earnings'].state == 'failed'
    assert 'sequence_index' not in failed.artifacts
    plan_file.write_text('{}')  # resume never rereads mutable original inputs
    complete = execute_stories(workspace, manifest.run_id)
    assert complete.state == 'checkpointed' and complete.outcome is None
    assert complete.artifacts['story.rates.final_media'] == earlier
    assert complete.story_progress['rates'].attempts == 1 and complete.story_progress['earnings'].attempts == 2
    sequence = read_json_model(directory / complete.artifacts['sequence.discussion'].path, SequenceManifest)
    assert [s.story.story_id for s in sequence.stories] == ['rates', 'earnings']
    assert not sequence.approved
    assert execute_stories(workspace, manifest.run_id) == complete
    assert calls.count(('render-story', 'rates')) == 1
    (directory / earlier.path).write_bytes(b'corrupt')
    with pytest.raises(ArtifactError, match='checksum'): execute_stories(workspace, manifest.run_id)


def test_rate_change_requires_new_run_and_no_completed_story_carryover(story_case, monkeypatch):
    config, config_file, plan_file, plan, _, _ = story_case
    fake_worker(story_case, monkeypatch)
    old, _ = prepare_run(config_file, story_plan_path=plan_file)
    execute_stories(Path(config.workspace), old.run_id, stop_after_stories=1)
    data = plan.model_dump(); data['sequences'][0]['stories'][0]['playback_rate'] = 1.1
    plan_file.write_bytes(json_bytes(data))
    new, _ = prepare_run(config_file, story_plan_path=plan_file)
    assert old.input_fingerprint != new.input_fingerprint
    assert all(p.state == 'pending' for p in new.story_progress.values())


def test_empty_manual_plan_is_not_a_no_match_decision(story_case, monkeypatch):
    config, config_file, plan_file, plan, _, _ = story_case
    calls = fake_worker(story_case, monkeypatch)
    plan_file.write_bytes(json_bytes(plan.model_copy(update={'sequences': []})))
    m, directory = prepare_run(config_file, story_plan_path=plan_file)
    result = execute_stories(Path(config.workspace), m.run_id)
    assert result.state == 'checkpointed' and result.outcome is None
    assert not any(a == 'render-story' for a, _ in calls)
    index = json.loads((directory / result.artifacts['sequence_index'].path).read_text())
    assert index['sequences'] == [] and not index['approved']


def test_preview_escapes_metadata_and_uses_local_separate_media(story_case, monkeypatch):
    config, config_file, plan_file, _, _, _ = story_case
    fake_worker(story_case, monkeypatch)
    m, directory = prepare_run(config_file, story_plan_path=plan_file)
    result = execute_stories(Path(config.workspace), m.run_id)
    sequence = read_json_model(directory / result.artifacts['sequence.discussion'].path, SequenceManifest)
    data = sequence.model_dump()
    data['sequence']['title'] = '<script>alert(1)</script>'
    sequence = SequenceManifest.model_validate(data)
    plan = read_json_model(directory / m.artifacts['story_plan'].path, StoryPlan)
    html = story_preview(plan, [sequence])
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert html.count('<video ') == 2 and 'Next story' in html and 'NOT APPROVED' in html


def test_cli_prepare_status_and_invalid_count(story_case, capsys):
    from clip_pipeline.__main__ import main
    config, config_file, plan_file, _, _, _ = story_case
    assert main(['render-stories','--config',str(config_file),'--plan',str(plan_file),
                 '--stop-after-stories','0']) == 2
    assert not Path(config.workspace).exists()
    capsys.readouterr()
    assert main(['render-stories','--config',str(config_file),'--plan',str(plan_file),'--prepare-only']) == 0
    response = json.loads(capsys.readouterr().out)
    assert response['execution_kind']=='manual_stories' and response['state']=='prepared'
    assert response['sequence_index'] is None and response['final_media'] is None
    assert main(['status','--workspace',str(config.workspace),'--run-id',response['run_id']]) == 0
    assert json.loads(capsys.readouterr().out)['story_progress']['rates']['state']=='pending'


def test_pinned_plan_mutation_is_rejected_before_reuse(story_case, monkeypatch):
    config, config_file, plan_file, _, _, _ = story_case
    fake_worker(story_case, monkeypatch)
    m, directory = prepare_run(config_file, story_plan_path=plan_file)
    execute_stories(Path(config.workspace), m.run_id, stop_after_stories=1)
    pinned = directory / m.artifacts['story_plan'].path
    data = json.loads(pinned.read_text()); data['sequences'][0]['stories'][0]['playback_rate'] = 1.1
    pinned.write_bytes(json_bytes(data))
    with pytest.raises(ArtifactError, match='checksum'):
        execute_stories(Path(config.workspace), m.run_id)
