"""Source-bound transcript imports and a cache separate from render settings."""
from __future__ import annotations

from importlib.metadata import version
import math
import os
from pathlib import Path
import re

from .artifacts import atomic_write, digest, json_bytes, read_json_model
from .contracts import Segment, TranscriptArtifact, Word
from .media_common import MediaError, file_hash

ADAPTER_VERSION = "2"


def timed_words(raw_words):
    """Coalesce instantaneous ASR tokens; never manufacture a time interval.

    A group uses its members' observed outer bounds. All-instantaneous segments
    and overlapping/reversed timestamps remain errors. Imported transcripts do
    not pass through this normalizer and must already satisfy the contract.
    """
    result, pending, grouped = [], [], 0
    previous_end = 0.0
    for raw in raw_words:
        start, end = float(raw["start"]), float(raw["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < previous_end or end < start:
            raise MediaError("asr_invalid_word_timing")
        previous_end = end
        text = raw["word"].strip()
        if not text:
            raise MediaError("asr_empty_word")
        if end == start:
            pending.append((text, start))
            grouped += 1
            continue
        if pending:
            text = " ".join([p[0] for p in pending] + [text])
            start = pending[0][1]
            pending = []
        result.append(Word(word=text, start=start, end=end))
    if pending:
        if not result:
            raise MediaError("asr_missing_word_durations")
        last = result.pop()
        result.append(Word(word=" ".join([last.word] + [p[0] for p in pending]),
                           start=last.start, end=pending[-1][1]))
    return result, grouped


def _text_key(value):
    # Decimal points, minus signs and percent/currency symbols change meaning.
    return re.sub(r"\s", "", value, flags=re.UNICODE).casefold()


def validate_transcript(transcript, source, media):
    if transcript.source_sha256 != source.media_sha256 or transcript.source_offset_seconds != 0:
        raise MediaError("transcript_source_mismatch")
    if abs(transcript.duration_seconds - source.duration_seconds) > 0.05:
        raise MediaError("transcript_duration_mismatch")
    if transcript.language != media.language:
        raise MediaError("transcript_language_mismatch")
    if transcript.timing != "word":
        raise MediaError("word_timing_required")
    for segment in transcript.segments:
        if segment.end > source.duration_seconds:
            raise MediaError("transcript_outside_source")
        if _text_key(segment.text) != _text_key(" ".join(w.word for w in segment.words)):
            raise MediaError("transcript_word_text_mismatch")
    return transcript


def legacy_transcript(transcript):
    return {"language": transcript.language, "text": " ".join(s.text for s in transcript.segments),
            "segments": [{"start": s.start, "end": s.end, "text": s.text,
                          # Contracts normalize whitespace; legacy caption helpers
                          # use a leading space to distinguish words from fragments.
                          "words": [{"word": " " + w.word, "start": w.start, "end": w.end} for w in s.words]}
                         for s in transcript.segments]}


def model_identity(media):
    from faster_whisper.utils import download_model
    path = Path(media.whisper_model)
    if not path.is_dir():
        path = Path(download_model(media.whisper_model))
    identity = digest({p.relative_to(path).as_posix(): file_hash(p)
                       for p in sorted(path.rglob("*")) if p.is_file()})
    return path, identity


def asr_settings(media, model_sha):
    from subtitles import WHISPER_TRANSCRIBE_PARAMS
    return {"adapter": ADAPTER_VERSION, "model_sha256": model_sha,
            "model": media.whisper_model, "device": media.whisper_device,
            "compute": media.whisper_compute, "expected_language": media.language,
            "parameters": WHISPER_TRANSCRIBE_PARAMS,
            "packages": {p: version(p) for p in ("faster-whisper", "ctranslate2", "av", "tokenizers")}}


def cache_key(source, settings):
    return digest({"source_sha256": source.media_sha256, "asr": settings})


def transcribe(source, media_path: Path, config, *, supplied: Path | None = None):
    cache = Path(config.workspace) / "cache" / "transcripts"
    cache.mkdir(parents=True, exist_ok=True)
    if supplied is not None:
        transcript = validate_transcript(read_json_model(supplied, TranscriptArtifact), source, config.media)
        settings = {"adapter": ADAPTER_VERSION, "import_sha256": file_hash(supplied)}
    else:
        model_path, model_sha = model_identity(config.media)
        settings = asr_settings(config.media, model_sha)
    key = cache_key(source, settings)
    cached = cache / (key + ".json")
    checksum = cache / (key + ".sha256")
    if cached.exists() and checksum.exists():
        if file_hash(cached) != checksum.read_text().strip():
            raise MediaError("transcript_cache_corrupt")
        result = validate_transcript(read_json_model(cached, TranscriptArtifact), source, config.media)
        return result, {"cache_key": key, "reused": True, "settings": settings}
    if supplied is None:
        import transcribe_backends
        os.environ["WHISPER_MODEL"] = str(model_path)
        try:
            raw = transcribe_backends.transcribe_media(str(media_path))
            if not raw.get("segments"):
                raise MediaError("source_has_no_usable_speech")
            if transcribe_backends._whisper_device() != config.media.whisper_device:
                raise MediaError("asr_device_fallback")
            segments, grouped = [], 0
            for i, segment in enumerate(raw["segments"]):
                words, count = timed_words(segment.get("words", []))
                grouped += count
                segments.append(Segment(segment_id=f"s{i:06d}", start=float(segment["start"]),
                                        end=float(segment["end"]), text=segment["text"], words=words))
            transcript = TranscriptArtifact(source_sha256=source.media_sha256,
                duration_seconds=source.duration_seconds, language=raw["language"], backend="faster-whisper",
                model_version=f"{config.media.whisper_model}@{settings['model_sha256']};{version('faster-whisper')}",
                timing="word", segments=segments,
                timing_warnings=["zero_duration_words_grouped"] if grouped else [])
            validate_transcript(transcript, source, config.media)
        finally:
            transcribe_backends.release_models()
    atomic_write(cached, json_bytes(transcript))
    atomic_write(checksum, (file_hash(cached) + "\n").encode())
    return transcript, {"cache_key": key, "reused": False, "settings": settings}
