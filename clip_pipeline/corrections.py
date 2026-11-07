"""Propose a timed word correction from independent delivered-audio evidence."""
import argparse
import json
from pathlib import Path

from .artifacts import ArtifactError, digest, json_bytes, read_json_model
from .contracts import TranscriptArtifact, StoryArtifact, TranscriptCorrection
from .audit_contracts import MediaInspection, AuditIssue
from .media_common import file_hash


def corrected_transcript(original, artifact, inspection, media, patch):
    if (original.source_sha256 != artifact.final.source_sha256 or
        inspection.final_sha256 != artifact.final.media.sha256 or
        inspection.story_id != artifact.story.story_id or
        file_hash(media) != artifact.final.media.sha256 or inspection.transcript is None or
        inspection.transcript.source_sha256 != artifact.final.media.sha256):
        raise ArtifactError('correction evidence does not bind source and delivered audio')
    segment = next((s for s in original.segments if s.segment_id == patch['segment_id']), None)
    index = patch['word_index']
    if segment is None or not isinstance(index, int) or not 0 <= index < len(segment.words):
        raise ArtifactError('unknown correction target')
    word = segment.words[index]
    if word.word != patch['before'] or not artifact.story.span.start <= word.start < word.end <= artifact.story.span.end:
        raise ArtifactError('correction original word/span mismatch')
    # Punctuation/case may vary; lexical replacement must be observed, not guessed.
    from .caption_audit import tokens
    after = patch['after']
    if len(tokens(after)) != 1 or tokens(after) == tokens(word.word):
        raise ArtifactError('correction must replace one lexical word')
    start = (word.start - artifact.story.span.start) / artifact.story.playback_rate
    end = (word.end - artifact.story.span.start) / artifact.story.playback_rate
    matches = [s for s in inspection.transcript.segments for w in s.words
               if tokens(w.word) == tokens(after) and w.start <= end+1 and w.end >= start-1]
    if not matches:
        raise ArtifactError('replacement lacks aligned independent audio evidence')
    evidence = matches[0]
    correction = TranscriptCorrection(original_transcript_sha256=digest(original),
        segment_id=segment.segment_id, word_index=index, start=word.start, end=word.end,
        before=word.word, after=after, evidence_media_sha256=artifact.final.media.sha256,
        evidence_transcript_sha256=digest(inspection.transcript), evidence_segment_id=evidence.segment_id,
        evidence_quote=evidence.text, evidence_model=inspection.transcript.model_version)
    data = original.model_dump()
    target = next(s for s in data['segments'] if s['segment_id'] == segment.segment_id)
    target['words'][index]['word'] = after
    target['text'] = ' '.join(w['word'] for w in target['words'])
    data['corrections'] = [c.model_dump() for c in original.corrections] + [correction.model_dump()]
    return TranscriptArtifact.model_validate(data)


def correction_issues(transcript, start, end):
    return [AuditIssue(issue_id='transcript_correction_'+str(i), rule_id='transcript_correction_review',
        severity='review', kind='caption', source_segment_ids=[c.segment_id], final_segment_ids=[],
        explanation=f'Proposed correction {c.before!r} to {c.after!r} retains timing and independent audio evidence. '
                    'Confirm against the audio in full-playback review; no human verification has been recorded.')
        for i,c in enumerate(transcript.corrections) if c.start < end and c.end > start]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('transcript','story','inspection','media','patch','output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    original = read_json_model(args.transcript, TranscriptArtifact)
    story = read_json_model(args.story, StoryArtifact)
    inspection = read_json_model(args.inspection, MediaInspection)
    result = corrected_transcript(original, story, inspection, args.media, json.loads(args.patch.read_text()))
    # A new directory preserves all prior files and the complete evidence packet.
    args.output.mkdir(parents=True, exist_ok=False)
    for name,value in [('original',original),('story',story),('inspection',inspection),('transcript',result)]:
        (args.output/(name+'.json')).write_bytes(json_bytes(value))
    print(json.dumps({'transcript':str((args.output/'transcript.json').resolve()),
                      'approved':False,'correction_review_required':True}))


if __name__ == '__main__':
    main()
