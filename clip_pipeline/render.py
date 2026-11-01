"""Render one explicitly selected span, retaining clean and captioned artifacts."""
from pathlib import Path

from .artifacts import atomic_write, json_bytes
from .contracts import FinalArtifact
from .media_common import MediaError, command, probe, reference, validate_output
from .transcript import legacy_transcript, validate_transcript


def render_one_clip(media_path: Path, source, transcript, request, config,
                    directory: Path, artifact_root: Path):
    validate_transcript(transcript, source, config.media)
    span = request.span
    duration = span.end - span.start
    if span.end > source.duration_seconds or not config.selection.min_seconds <= duration <= config.selection.max_seconds:
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
    command(["ffmpeg", "-y", "-i", framed, "-map", "0:v:0", "-map", "0:a:0",
             "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
             *video_encode_args(QUALITY), "-threads", "2", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", *METADATA_SCRUB, "-movflags", "+faststart", clean])
    ranges = layout_ranges.read(str(framed))
    atomic_write(directory / "layout-ranges.json", json_bytes(ranges))
    final = clean
    files = {"clean_media": clean, "layout_ranges": directory / "layout-ranges.json"}
    captions_ref = None
    if config.media.captions:
        ass, srt = directory / "captions.ass", directory / "captions.srt"
        legacy = legacy_transcript(transcript)
        style = subtitles.AUTO_CAPTION_STYLE
        if not subtitles.generate_ass(legacy, span.start, span.end, str(ass),
                split_ranges=layout_ranges.split_ranges(ranges),
                max_chars=style["max_chars"], max_duration=style["max_duration"],
                alignment=style["alignment"], fontsize=style["font_size"],
                font_name=style["font_name"], font_color=style["font_color"],
                border_color=style["border_color"], border_width=style["border_width"],
                highlight_color=style["highlight_color"], effect=style["effect"],
                base_opacity=style["base_opacity"], uppercase=style["uppercase"]):
            raise MediaError("no_caption_words_in_span")
        subtitles.generate_srt(legacy, span.start, span.end, str(srt),
                               max_chars=style["max_chars"], max_duration=style["max_duration"])
        final = directory / "captioned.mp4"
        subtitles.burn_subtitles(str(clean), str(ass), str(final),
            alignment=style["alignment"], fontsize=style["font_size"], font_name=style["font_name"],
            font_color=style["font_color"], border_color=style["border_color"], border_width=style["border_width"])
        files.update(captions_ass=ass, captions_srt=srt)
        captions_ref = reference(ass, artifact_root)
    info = validate_output(final, duration)
    result = FinalArtifact(clip_id="manual-001", source_sha256=source.media_sha256,
        source_span=span, media=reference(final, artifact_root), captions=captions_ref,
        audio_channels=info["audio_channels"], duration_seconds=info["duration"])
    atomic_write(directory / "final.json", json_bytes(result))
    atomic_write(directory / "render-report.json", json_bytes({
        "selection_method": "manual", "approved": False, "audit_performed": False,
        "source_quality_review": original["height"] < 1920 or original["width"] < 1080,
        "boundary_word_review": any(w.start < boundary < w.end for s in transcript.segments
                                    for w in s.words for boundary in (span.start, span.end)),
        "transcript_timing_warnings": transcript.timing_warnings,
        "layout": config.media.layout, "captions": config.media.captions,
        "final_media": result.media.model_dump(), "media_checks": info,
    }))
    files.update(final_media=final, final=directory / "final.json", render_report=directory / "render-report.json")
    # Only intermediates created by this invocation are removed; source/ASR stay.
    cut.unlink(missing_ok=True)
    framed.unlink(missing_ok=True)
    return files
