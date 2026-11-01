"""Fresh process entry point. Apply the profile before any optional media import."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from pathlib import Path
import sys

from .artifacts import atomic_write, json_bytes, read_json_model
from .config import load_config
from .contracts import ManualRenderRequest, SourceArtifact, SourceInput, TranscriptArtifact, StoryPlan
from .profile import activate_media_profile


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["ingest", "transcribe", "render", "render-story"])
    parser.add_argument("--story-id")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    for name in ("submission", "source", "media", "transcript", "request", "supplied"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    activate_media_profile(config.media)
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with redirect_stdout(sys.stderr):
            if args.action == "ingest":
                from .ingest import ingest
                source, path, reused = ingest(read_json_model(args.submission, SourceInput), directory, config)
                atomic_write(directory / "source.json", json_bytes(source))
                atomic_write(directory / "ingest.json", json_bytes({"reused": reused}))
                files = {"source": directory / "source.json", "source_media": path,
                         "ingest": directory / "ingest.json"}
            else:
                source = read_json_model(args.source, SourceArtifact)
                from .media_common import file_hash, MediaError
                if file_hash(args.media) != source.media_sha256:
                    raise MediaError("source_media_hash_mismatch")
                if args.action == "transcribe":
                    from .transcript import transcribe
                    transcript, report = transcribe(source, args.media, config, supplied=args.supplied)
                    atomic_write(directory / "transcript.json", json_bytes(transcript))
                    atomic_write(directory / "transcription.json", json_bytes(report))
                    files = {"transcript": directory / "transcript.json", "transcription": directory / "transcription.json"}
                else:
                    from .render import render_one_clip
                    transcript = read_json_model(args.transcript, TranscriptArtifact)
                    if args.action == "render-story":
                        from .stories import all_stories, validate_plan_config, validate_plan_transcript
                        plan = read_json_model(args.request, StoryPlan)
                        validate_plan_config(plan, config)
                        validate_plan_transcript(plan, source, transcript, config)
                        request = next(s for s in all_stories(plan) if s.story_id == args.story_id)
                    else:
                        request = read_json_model(args.request, ManualRenderRequest)
                    files = render_one_clip(args.media, source, transcript,
                        request, config, directory, args.config.parent)
        atomic_write(directory / "result.json", json_bytes({k: str(p.resolve()) for k, p in files.items()}))
        return 0
    except Exception as exc:
        from .media_common import MediaError
        import traceback
        traceback.print_exc(file=sys.stderr)
        code = str(exc) if isinstance(exc, MediaError) else "media_stage_failed"
        atomic_write(directory / "error.json", json_bytes({"error": code}))
        return 4


if __name__ == "__main__":
    sys.exit(main())
