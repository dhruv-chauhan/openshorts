"""Render one explicitly selected span, retaining clean and captioned artifacts."""
from pathlib import Path

from .artifacts import atomic_write, json_bytes
from .contracts import FinalArtifact, StorySpec, StoryArtifact
from .media_common import MediaError, command, probe, reference, validate_output
from .transcript import legacy_transcript, validate_transcript


def render_one_clip(media_path: Path, source, transcript, request, config,
                    directory: Path, artifact_root: Path):
    validate_transcript(transcript, source, config.media)
    span = request.span
    is_story = isinstance(request, StorySpec)
    rate = request.playback_rate if is_story else 1.0
    duration = (span.end - span.start) / rate
    valid_duration = (duration <= config.stories.max_story_seconds if is_story else
                      config.selection.min_seconds <= duration <= config.selection.max_seconds)
    if span.end > source.duration_seconds or not valid_duration:
        raise MediaError("invalid_render_span")
    if config.media.hook_overlay:
        raise MediaError("unaudited_hook_not_supported")
    directory.mkdir(parents=True, exist_ok=True)
    from ffmpeg_utils import cut_clip, video_encode_args, QUALITY, METADATA_SCRUB
    import main
    import layout_ranges
    import subtitles

    cut, framed, clean = [directory / name for name in ("cut.mp4", "reframed.mp4", "clean.mp4")]
    cut_clip(str(media_path), str(cut), span.start, span.end, 1)
    original = probe(cut)
    already_vertical = abs(original["width"] / original["height"] - 9 / 16) < 0.01
    force = "WIDE" if config.media.layout == "preserve_full_width" and not already_vertical else None
    if not main.render_clip(str(cut), str(framed), output_format="vertical", force_strategy=force):
        raise MediaError("reframe_failed")
    # Normalize even already-vertical passthroughs; the legacy renderer has a floor,
    # whereas this profile promises exact geometry, square pixels and AAC audio.
    geometry = "scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1"
    tempo = []
    if rate != 1.0:
        geometry += f",setpts=(PTS-STARTPTS)/{rate}"
        tempo = ["-af", f"asetpts=PTS-STARTPTS,atempo={rate}"]
    # Legacy scene concatenation can leave duplicate tail frames. A story's
    # output boundary is its requested source interval after rate conversion.
    duration_limit = ["-t", f"{duration:.9f}"] if is_story else []
    command(["ffmpeg", "-y", "-i", framed, "-map", "0:v:0", "-map", "0:a:0",
             "-vf", geometry, *tempo,
             *video_encode_args(QUALITY), "-threads", "2", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", *METADATA_SCRUB, "-movflags", "+faststart", *duration_limit, clean])
    ranges = layout_ranges.read(str(framed))
    if is_story:
        from .stories import story_transcript, retime_layout, story_timeline
        ranges = retime_layout(ranges, rate, duration)
        atomic_write(directory / "timeline.json", json_bytes(story_timeline(request, transcript)))
    atomic_write(directory / "layout-ranges.json", json_bytes(ranges))
    final = clean
    files = {"clean_media": clean, "layout_ranges": directory / "layout-ranges.json"}
    captions_ref = None
    caption_layout = None
    if config.media.captions:
        ass, srt = directory / "captions.ass", directory / "captions.srt"
        legacy = story_transcript(transcript, request) if is_story else legacy_transcript(transcript)
        caption_start, caption_end = (0.0, duration) if is_story else (span.start, span.end)
        style = subtitles.AUTO_CAPTION_STYLE
        if config.media.caption_style is not None:
            if config.media.layout != 'preserve_full_width':
                raise MediaError('caption_safe_area_requires_full_width')
            from .captions import generate
            # Coordinates follow the delivered picture, not a guessed footer height.
            local = legacy if is_story else {'segments': [
                {'words': [{**w, 'start': w['start']-span.start, 'end': w['end']-span.start}
                           for w in s['words'] if w['start'] >= span.start and w['end'] <= span.end]}
                for s in legacy['segments']]}
            caption_layout = generate(local, config.media.caption_style,
                                      original['width'], original['height'], ass, srt)
        else:
            if not subtitles.generate_ass(legacy, caption_start, caption_end, str(ass),
                    split_ranges=layout_ranges.split_ranges(ranges),
                    max_chars=style["max_chars"], max_duration=style["max_duration"],
                    alignment=style["alignment"], fontsize=style["font_size"],
                    font_name=style["font_name"], font_color=style["font_color"],
                    border_color=style["border_color"], border_width=style["border_width"],
                    highlight_color=style["highlight_color"], effect=style["effect"],
                    base_opacity=style["base_opacity"], uppercase=style["uppercase"]):
                raise MediaError("no_caption_words_in_span")
            subtitles.generate_srt(legacy, caption_start, caption_end, str(srt),
                                   max_chars=style["max_chars"], max_duration=style["max_duration"])
        final = directory / "captioned.mp4"
        subtitles.burn_subtitles(str(clean), str(ass), str(final),
            alignment=style["alignment"], fontsize=style["font_size"], font_name=style["font_name"],
            font_color=style["font_color"], border_color=style["border_color"], border_width=style["border_width"])
        files.update(captions_ass=ass, captions_srt=srt)
        captions_ref = reference(ass, artifact_root)
    info = validate_output(final, duration)
    result = FinalArtifact(clip_id=request.story_id if is_story else "manual-001", source_sha256=source.media_sha256,
        source_span=span, media=reference(final, artifact_root), captions=captions_ref,
        audio_channels=info["audio_channels"], duration_seconds=info["duration"])
    atomic_write(directory / "final.json", json_bytes(result))
    atomic_write(directory / "render-report.json", json_bytes({
        "selection_method": "supplied_story_plan" if is_story else "manual", "approved": False, "audit_performed": False,
        "playback_rate": rate,
        "source_quality_review": original["height"] < 1920 or original["width"] < 1080,
        "boundary_word_review": any(w.start < boundary < w.end for s in transcript.segments
                                    for w in s.words for boundary in (span.start, span.end)),
        "transcript_timing_warnings": transcript.timing_warnings,
        "layout": config.media.layout, "captions": config.media.captions,
        "caption_layout": caption_layout,
        "final_media": result.media.model_dump(), "media_checks": info,
    }))
    files.update(final_media=final, final=directory / "final.json", render_report=directory / "render-report.json")
    if is_story:
        files["timeline"] = directory / "timeline.json"
        artifact = StoryArtifact(story=request, final=result, clean_media=reference(clean, artifact_root),
            layout_ranges=reference(files["layout_ranges"], artifact_root),
            timeline=reference(files["timeline"], artifact_root),
            render_report=reference(files["render_report"], artifact_root),
            captions_srt=reference(files["captions_srt"], artifact_root) if "captions_srt" in files else None)
        atomic_write(directory / "story.json", json_bytes(artifact))
        files["story"] = directory / "story.json"
    # Only intermediates created by this invocation are removed; source/ASR stay.
    cut.unlink(missing_ok=True)
    framed.unlink(missing_ok=True)
    return files
