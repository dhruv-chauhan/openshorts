"""Pure story-plan validation and source-to-story timestamp mappings."""
from __future__ import annotations

import re
from .artifacts import ArtifactError, digest
from .media_common import MediaError
from .transcript import validate_transcript


def all_stories(plan):
    return [story for sequence in plan.sequences for story in sequence.stories]


def validate_plan_config(plan, config):
    if config.media.hook_overlay:
        raise ArtifactError("story plans do not contain audited hook copy")
    if len(plan.sequences) > config.stories.max_sequences:
        raise ArtifactError("sequence count exceeds configured limit")
    for sequence in plan.sequences:
        if len(sequence.stories) > config.stories.max_stories_per_sequence:
            raise ArtifactError("story count exceeds configured limit")
        durations = [(s.span.end - s.span.start) / s.playback_rate for s in sequence.stories]
        if any(d > config.stories.max_story_seconds for d in durations):
            raise ArtifactError("story duration exceeds configured limit; do not truncate")
        if sum(durations) > config.stories.max_sequence_seconds:
            raise ArtifactError("sequence duration exceeds configured limit; do not drop context")


def validate_plan_source(plan, source):
    if plan.source_sha256 != source.media_sha256:
        raise MediaError("story_source_hash_mismatch")
    if any(s.span.end > source.duration_seconds for s in all_stories(plan)):
        raise MediaError("story_span_exceeds_source")


def validate_plan_transcript(plan, source, transcript, config):
    validate_plan_source(plan, source)
    validate_transcript(transcript, source, config.media)
    if digest(transcript) != plan.transcript_sha256:
        raise MediaError("story_transcript_hash_mismatch")
    segments = {s.segment_id: s for s in transcript.segments}
    normal = lambda text: re.sub(r"\s+", "", text).casefold()
    for story in all_stories(plan):
        words = [w for s in transcript.segments for w in s.words
                 if w.end > story.span.start and w.start < story.span.end]
        if not words:
            raise MediaError("story_has_no_words")
        if any(w.start < story.span.start or w.end > story.span.end for w in words):
            raise MediaError("story_boundary_inside_word")
        if story.enrichment:
            inside = {s.segment_id for s in transcript.segments if s.words[0].start >= story.span.start
                      and s.words[-1].end <= story.span.end}
            metadata = story.enrichment
            items = [*metadata.classification.subjects, *metadata.classification.categories, *metadata.associations]
            if any(not set(item.evidence_segment_ids).issubset(inside) for item in items):
                raise MediaError("enrichment_evidence_outside_story")
        for evidence in story.evidence + story.context:
            segment = segments.get(evidence.segment_id)
            if (segment is None or evidence.start < segment.start or evidence.end > segment.end):
                raise MediaError("story_evidence_bounds")
            selected = [w for w in segment.words if w.start >= evidence.start and w.end <= evidence.end]
            if normal(evidence.quote) not in normal(" ".join(w.word for w in selected)):
                raise MediaError("story_evidence_quote_mismatch")


def output_time(source_time, story):
    if not story.span.start <= source_time <= story.span.end:
        raise ValueError("source time outside story")
    return (source_time - story.span.start) / story.playback_rate


def story_transcript(transcript, story):
    """Legacy caption shape on the output timeline; source transcript is immutable."""
    segments = []
    for segment in transcript.segments:
        words = [{"word": " " + w.word, "start": output_time(w.start, story),
                  "end": output_time(w.end, story)} for w in segment.words
                 if w.start >= story.span.start and w.end <= story.span.end]
        if words:
            segments.append({"start": words[0]["start"], "end": words[-1]["end"],
                             "text": " ".join(w["word"].strip() for w in words), "words": words})
    return {"language": transcript.language, "segments": segments,
            "text": " ".join(s["text"] for s in segments)}


def retime_layout(ranges, rate, duration):
    return [{**r, "start": max(0.0, r["start"] / rate), "end": min(duration, r["end"] / rate)}
            for r in ranges if min(duration, r["end"] / rate) > max(0.0, r["start"] / rate)]


def story_timeline(story, transcript):
    def mapped(e):
        return {"source": e.model_dump(), "output": {"start": output_time(e.start, story),
                                                       "end": output_time(e.end, story)}}
    return {"schema_version": "1", "source_span": story.span.model_dump(),
            "playback_rate": story.playback_rate,
            "expected_duration_seconds": output_time(story.span.end, story),
            "words": [{"word": w.word, "segment_id": s.segment_id,
                       "source": {"start": w.start, "end": w.end},
                       "output": {"start": output_time(w.start, story), "end": output_time(w.end, story)}}
                      for s in transcript.segments for w in s.words
                      if w.start >= story.span.start and w.end <= story.span.end],
            "evidence": [mapped(e) for e in story.evidence],
            "context_source_only": [e.model_dump() for e in story.context],
            "timing_warnings": transcript.timing_warnings}


def validate_story_checkpoint(manifest, directory, config):
    """A rendered progress marker must bind every nested output to the manifest."""
    from .artifacts import read_json_model
    from .contracts import StoryArtifact, StoryPlan, SequenceManifest, FinalArtifact
    if "story_plan" not in (manifest.input_artifact_keys or []):
        raise ArtifactError("story plan is not a pinned input")
    plan = read_json_model(directory / manifest.artifacts["story_plan"].path, StoryPlan)
    from .review_render import validate_review_binding
    validate_review_binding(plan, manifest, directory)
    validate_plan_config(plan, config)
    if 'enrichment' in plan.selection_binding:
        from .enrichment_contracts import KnowledgeSnapshot
        ref = manifest.artifacts.get('enrichment')
        if ref is None or digest(read_json_model(directory / ref.path, KnowledgeSnapshot)) != plan.selection_binding['enrichment']:
            raise ArtifactError("story knowledge snapshot mismatch")
    if plan.source != manifest.source or set(manifest.story_progress) != {s.story_id for s in all_stories(plan)}:
        raise ArtifactError("story checkpoint identities do not match the plan")
    rendered = {}
    for story in all_stories(plan):
        progress = manifest.story_progress[story.story_id]
        if progress.state != "rendered":
            continue
        prefix = f"story.{story.story_id}."
        if progress.artifact_key != prefix + "story":
            raise ArtifactError("unexpected story artifact key")
        result = read_json_model(directory / manifest.artifacts[progress.artifact_key].path, StoryArtifact)
        if result.story != story or result.final.source_sha256 != plan.source_sha256:
            raise ArtifactError("story artifact does not match pinned request")
        final_ref = manifest.artifacts.get(prefix + "final")
        if final_ref is None or read_json_model(directory / final_ref.path, FinalArtifact) != result.final:
            raise ArtifactError("story final metadata mismatch")
        nested = {"final_media": result.final.media, "clean_media": result.clean_media,
                  "layout_ranges": result.layout_ranges, "timeline": result.timeline,
                  "render_report": result.render_report}
        if result.final.captions:
            nested["captions_ass"] = result.final.captions
        if result.captions_srt:
            nested["captions_srt"] = result.captions_srt
        if any(manifest.artifacts.get(prefix + key) != ref for key, ref in nested.items()):
            raise ArtifactError("untracked story artifact reference")
        rendered[story.story_id] = result
    if manifest.state == "checkpointed":
        if "preview" not in manifest.artifacts or "sequence_index" not in manifest.artifacts:
            raise ArtifactError("completed stories need an index and preview")
        for sequence in plan.sequences:
            key = "sequence." + sequence.sequence_id
            if key not in manifest.artifacts:
                raise ArtifactError("missing sequence manifest")
            result = read_json_model(directory / manifest.artifacts[key].path, SequenceManifest)
            if (result.plan_sha256 != digest(plan) or result.source_sha256 != plan.source_sha256
                    or result.transcript_sha256 != plan.transcript_sha256 or result.sequence != sequence
                    or result.stories != [rendered[s.story_id] for s in sequence.stories]):
                raise ArtifactError("sequence does not match pinned plan and story outputs")
