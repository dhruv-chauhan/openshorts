"""Inspect the delivered bytes and independently transcribe their audio in a worker."""
from pathlib import Path
import re
from .audit_contracts import AuditIssue, MediaInspection
from .contracts import SourceArtifact, SourceInput
from .media_common import MediaError, file_hash, validate_output
from .transcript import transcribe


def caption_text(path, duration):
    text = path.read_text()
    lines = []
    def seconds(value):
        h,m,s = value.split(':')
        return int(h)*3600 + int(m)*60 + float(s)
    for line in text.splitlines():
        if line.startswith('Dialogue:'):
            parts = line.split(',',9)
            if len(parts) != 10 or not 0 <= seconds(parts[1]) < seconds(parts[2]) <= duration + .2:
                raise MediaError('caption_event_bounds')
            visible=re.sub(r'\{[^}]*\}', '', parts[-1]).replace('\\N',' ')
            start,end=seconds(parts[1]),seconds(parts[2])
            # Adjacent karaoke highlights repeat visible text, not speech.
            if lines and lines[-1][2]==visible and abs(lines[-1][1]-start)<.02:
                lines[-1]=(lines[-1][0],end,visible)
            else:
                lines.append((start,end,visible))
    if not lines:
        raise MediaError('missing_caption_events')
    return '\n'.join(f'[{start:.2f}–{end:.2f}] {text}' for start,end,text in lines)


def inspect_media(media, artifact, config, render_root):
    if file_hash(media) != artifact.final.media.sha256:
        raise MediaError('audit_final_hash_mismatch')
    issues, checks, captions = [], {}, ''
    def issue(code, kind='media'):
        issues.append(AuditIssue(issue_id=code,rule_id='delivery_integrity',severity='reject',kind=kind,
            explanation=code,source_segment_ids=[],final_segment_ids=[]))
    try:
        info=validate_output(media, (artifact.story.span.end-artifact.story.span.start)/artifact.story.playback_rate)
        checks['format_duration_decode']=True
    except MediaError as exc:
        if str(exc) not in {'invalid_delivery_format','render_duration_mismatch','media_command_failed',
                            'source_has_no_video','invalid_media_duration'}:
            raise
        issue(str(exc));checks['format_duration_decode']=False
        return MediaInspection(story_id=artifact.story.story_id, final_sha256=artifact.final.media.sha256,
            checks=checks,issues=issues,transcript=None,captions_text='')
    if artifact.final.captions:
        path=render_root/artifact.final.captions.path
        if file_hash(path)!=artifact.final.captions.sha256:raise MediaError('audit_caption_hash_mismatch')
        try:
            captions=caption_text(path,info['duration']);checks['caption_bounds']=True
        except (ValueError,MediaError):
            checks['caption_bounds']=False;issue('caption_event_bounds','caption')
    elif config.media.captions:
        checks['caption_bounds']=False;issue('missing_captions','caption')
    final_source=SourceArtifact(source_id=artifact.story.story_id,
        origin=SourceInput(kind='local',locator=str(media.resolve())),
        media_sha256=artifact.final.media.sha256,duration_seconds=info['duration'])
    audit_config=config.model_copy(update={'media':config.media.model_copy(update={'whisper_model':config.audit.whisper_model})})
    # Never supply or crop the source transcript here. Cache identity includes final bytes and ASR settings.
    transcript, transcription=transcribe(final_source,media,audit_config)
    checks['final_audio_transcribed']=True
    return MediaInspection(story_id=artifact.story.story_id,final_sha256=artifact.final.media.sha256,
        checks=checks,issues=issues,transcript=transcript,transcription=transcription,captions_text=captions)
