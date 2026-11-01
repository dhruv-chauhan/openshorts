"""Phase C controller, source binding, cache invalidation and bounded acquisition."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from clip_pipeline.artifacts import ArtifactError, json_bytes, read_json_model
from clip_pipeline.config import PipelineConfig
from clip_pipeline.contracts import (ManualRenderRequest, SourceArtifact, SourceInput,
                                     TimeSpan, TranscriptArtifact)
from clip_pipeline.execution import execute_manual
from clip_pipeline.ingest import ingest
from clip_pipeline.media_common import MediaError, file_hash
from clip_pipeline.runner import inspect_run, prepare_run
from clip_pipeline.transcript import cache_key, legacy_transcript, timed_words, transcribe, validate_transcript
from test_clip_pipeline_config import config_data, config_path  # noqa: F401


@pytest.fixture
def media_case(tmp_path, config_data, monkeypatch):
    config_data["limits"] = {"min_free_disk_bytes": 1, "max_source_bytes": 1000}
    config_data["selection"] = {"min_seconds": 5.0, "max_seconds": 10.0}
    config = PipelineConfig.model_validate(config_data)
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"original media")
    origin = SourceInput(kind="local", locator=str(source_path))
    source = SourceArtifact(source_id="test", origin=origin, media_sha256=file_hash(source_path), duration_seconds=10.0)
    transcript = TranscriptArtifact.model_validate({
        "source_sha256": source.media_sha256, "duration_seconds": 10.0, "language": "en",
        "backend": "test", "model_version": "fixture", "timing": "word",
        "segments": [{"segment_id": "s0", "start": 0.0, "end": 2.0, "text": "Rates affect stocks.",
                      "words": [{"word": "Rates", "start": 0.0, "end": 0.5},
                                {"word": "affect", "start": 0.5, "end": 1.0},
                                {"word": "stocks.", "start": 1.0, "end": 2.0}]}],
    })
    monkeypatch.setattr("clip_pipeline.ingest.probe", lambda _: {"duration": 10.0, "audio_channels": 2})
    return config, source_path, source, transcript


def test_ingest_reuses_content_and_invalidates_same_name_changed_bytes(media_case, tmp_path):
    config, path, source, _ = media_case
    first, first_path, reused = ingest(source.origin, tmp_path / "a", config)
    assert not reused and first.media_sha256 == source.media_sha256
    second, _, reused = ingest(source.origin, tmp_path / "b", config)
    assert reused and second.media_sha256 == first.media_sha256
    path.write_bytes(b"different bytes same filename")
    changed, _, reused = ingest(source.origin, tmp_path / "c", config)
    assert not reused and changed.media_sha256 != first.media_sha256
    assert first_path.read_bytes() == b"original media"


def test_source_cache_corruption_fails_closed(media_case, tmp_path):
    config, _, source, _ = media_case
    artifact, _, _ = ingest(source.origin, tmp_path / "a", config)
    blob = Path(config.workspace) / "cache/sources" / artifact.media_sha256 / "source.media"
    blob.write_bytes(b"corrupted")
    with pytest.raises(MediaError, match="source_cache_corrupt"):
        ingest(source.origin, tmp_path / "b", config)


@pytest.mark.parametrize("info,code", [({"duration": 15000.0, "audio_channels": 2}, "source_duration_limit"),
                                     ({"duration": 10.0, "audio_channels": 0}, "source_has_no_audio")])
def test_source_limits_and_unsupported_audio(media_case, tmp_path, monkeypatch, info, code):
    config, _, source, _ = media_case
    monkeypatch.setattr("clip_pipeline.ingest.probe", lambda _: info)
    with pytest.raises(MediaError, match=code):
        ingest(source.origin, tmp_path / "a", config)


def test_local_size_limit_before_copy(media_case, tmp_path):
    config, path, source, _ = media_case
    path.write_bytes(b"x" * 1001)
    with pytest.raises(MediaError, match="invalid_local_source"):
        ingest(source.origin, tmp_path / "a", config)
    assert not (tmp_path / "a/source.media").exists()


def test_youtube_uses_shared_ssrf_guard_and_bounds_total_stream_bytes(media_case, tmp_path, monkeypatch):
    from clip_pipeline.ingest import download_youtube
    import ssrf_guard
    config, _, _, _ = media_case
    seen = {}
    class Downloader:
        def __init__(self, options):
            seen.update(options)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def extract_info(self, url, download):
            assert ssrf_guard.active()
            seen["match_filter"]({"duration": 10.0}, incomplete=False)
            progress = seen["progress_hooks"][0]
            progress({"filename": "video", "downloaded_bytes": 600})
            progress({"filename": "audio", "downloaded_bytes": 600})
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=Downloader))
    monkeypatch.setattr("security_utils.assert_public_url", lambda url: url)
    with pytest.raises(MediaError, match="source_size_limit"):
        download_youtube(SourceInput(kind="youtube", locator="https://youtu.be/abcdefghijk"), tmp_path, config)
    assert seen["noplaylist"] and seen["proxy"] == "" and seen["concurrent_fragment_downloads"] == 1
    assert seen["extractor_args"]["youtube"]["player_client"] == ["default", "mweb"]
    for info in ({"is_live": True}, {"duration": 15000.0}, {}):
        with pytest.raises(MediaError):
            seen["match_filter"](info, incomplete=False)


def test_youtube_download_once_then_validated_source_cache(media_case, tmp_path, monkeypatch):
    config, _, _, _ = media_case
    source = SourceInput(kind="youtube", locator="https://youtu.be/abcdefghijk")
    calls = []
    def download(origin, directory, config):
        calls.append(origin)
        p = directory / "download.mp4"
        p.write_bytes(b"downloaded")
        return p, None
    monkeypatch.setattr("clip_pipeline.ingest.download_youtube", download)
    first, _, _ = ingest(source, tmp_path / "first", config)
    second, _, reused = ingest(source, tmp_path / "second", config)
    assert reused and first == second and len(calls) == 1


def test_import_cache_and_word_boundary_compatibility(media_case, tmp_path):
    config, path, source, transcript = media_case
    supplied = tmp_path / "transcript.json"
    supplied.write_bytes(json_bytes(transcript))
    first, report = transcribe(source, path, config, supplied=supplied)
    assert not report["reused"]
    second, report = transcribe(source, path, config, supplied=supplied)
    assert report["reused"] and first == second
    from subtitles import _collect_word_blocks
    words = [w for block in _collect_word_blocks(legacy_transcript(first), 0, 5) for w in block]
    assert [w["word"].strip() for w in words] == ["Rates", "affect", "stocks."]
    cached = Path(config.workspace) / "cache/transcripts" / (report["cache_key"] + ".json")
    cached.write_bytes(cached.read_bytes() + b" ")
    with pytest.raises(MediaError, match="transcript_cache_corrupt"):
        transcribe(source, path, config, supplied=supplied)


@pytest.mark.parametrize("change,code", [
    ({"source_sha256": "0" * 64}, "transcript_source_mismatch"),
    ({"duration_seconds": 20.0}, "transcript_duration_mismatch"),
    ({"language": "de"}, "transcript_language_mismatch"),
    ({"timing": "segment"}, "word_timing_required"),
])
def test_import_binding(media_case, change, code):
    config, _, source, transcript = media_case
    changed = TranscriptArtifact.model_validate({**transcript.model_dump(), **change})
    with pytest.raises(MediaError, match=code):
        validate_transcript(changed, source, config.media)


def test_word_text_consistency(media_case):
    config, _, source, transcript = media_case
    data = transcript.model_dump()
    data["segments"][0]["text"] = "Invented claim."
    with pytest.raises(MediaError, match="transcript_word_text_mismatch"):
        validate_transcript(TranscriptArtifact.model_validate(data), source, config.media)


@pytest.mark.parametrize("text,word", [("1.2", "12"), ("-2%", "2%"), ("2%", "2"), ("$2", "2")])
def test_word_text_consistency_preserves_financial_punctuation(media_case, text, word):
    config, _, source, transcript = media_case
    data = transcript.model_dump()
    data["segments"][0].update(text=text, words=[{"word": word, "start": 0.0, "end": 2.0}])
    with pytest.raises(MediaError, match="transcript_word_text_mismatch"):
        validate_transcript(TranscriptArtifact.model_validate(data), source, config.media)


def test_instantaneous_asr_words_keep_text_and_observed_group_bounds():
    words, grouped = timed_words([
        {"word": " I", "start": 8.0, "end": 8.08},
        {"word": " don't", "start": 8.08, "end": 8.08},
        {"word": " know", "start": 8.08, "end": 8.4},
        {"word": " yet.", "start": 8.4, "end": 8.4},
    ])
    assert grouped == 2
    assert [(w.word, w.start, w.end) for w in words] == [("I", 8.0, 8.08), ("don't know yet.", 8.08, 8.4)]


@pytest.mark.parametrize("words,reason", [
    ([{"word": "hello", "start": 1.0, "end": 1.0}], "asr_missing_word_durations"),
    ([{"word": "hello", "start": 2.0, "end": 1.0}], "asr_invalid_word_timing"),
    ([{"word": "hello", "start": 0.0, "end": 1.0},
      {"word": "world", "start": 0.8, "end": 1.3}], "asr_invalid_word_timing"),
])
def test_grouping_does_not_repair_unusable_word_timing(words, reason):
    with pytest.raises(MediaError, match=reason):
        timed_words(words)


def test_cache_key_changes_with_asr_and_source_not_render_settings(media_case):
    _, _, source, _ = media_case
    key = cache_key(source, {"model": "small", "device": "cpu"})
    assert key != cache_key(source, {"model": "tiny", "device": "cpu"})
    assert key != cache_key(source.model_copy(update={"media_sha256": "f" * 64}), {"model": "small", "device": "cpu"})


def test_asr_releases_models_on_failure(media_case, monkeypatch):
    import transcribe_backends
    config, path, source, _ = media_case
    monkeypatch.setenv("WHISPER_MODEL", "small")
    monkeypatch.setattr("clip_pipeline.transcript.model_identity", lambda _: (path.parent, "abc"))
    monkeypatch.setattr("clip_pipeline.transcript.asr_settings", lambda *_: {"model_sha256": "abc"})
    def fail(_):
        raise RuntimeError("ASR failure")
    monkeypatch.setattr(transcribe_backends, "transcribe_media", fail)
    released = []
    monkeypatch.setattr(transcribe_backends, "release_models", lambda: released.append(True))
    with pytest.raises(RuntimeError, match="ASR failure"):
        transcribe(source, path, config)
    assert released == [True]


def test_manual_render_resumes_committed_stage_and_never_approves(media_case, tmp_path, monkeypatch):
    config, path, source, transcript = media_case
    config_file = tmp_path / "config.json"
    config_file.write_bytes(json_bytes(config))
    request = ManualRenderRequest(span=TimeSpan(start=0.0, end=5.0))
    manifest, directory = prepare_run(config_file, source.origin, render_request=request)
    calls, interrupt = [], [True]
    def worker(action, work, config_path, config, **inputs):
        calls.append(action)
        work.mkdir(parents=True)
        if action == "ingest":
            values = {"source": source, "source_media": b"original media", "ingest": {"reused": False}}
        elif action == "transcribe":
            if interrupt.pop() if interrupt else False:
                raise KeyboardInterrupt
            values = {"transcript": transcript, "transcription": {"reused": False}}
        else:
            values = {"final": {}, "final_media": b"rendered", "clean_media": b"clean", "render_report": {"approved": False}, "layout_ranges": []}
        result = {}
        for name, value in values.items():
            target = work / name
            target.write_bytes(value if isinstance(value, bytes) else json_bytes(value))
            result[name] = target
        return result
    monkeypatch.setattr("clip_pipeline.execution._worker", worker)
    with pytest.raises(KeyboardInterrupt):
        execute_manual(Path(config.workspace), manifest.run_id)
    saved = inspect_run(Path(config.workspace), manifest.run_id)
    assert saved.stage == "ingested" and saved.input_fingerprint == manifest.input_fingerprint
    result = execute_manual(Path(config.workspace), manifest.run_id)
    assert result.state == "checkpointed" and result.stage == "rendered" and result.outcome is None
    assert calls == ["ingest", "transcribe", "transcribe", "render"]
    assert execute_manual(Path(config.workspace), manifest.run_id) == result
    (directory / result.artifacts["final_media"].path).write_bytes(b"changed")
    with pytest.raises(ArtifactError, match="checksum"):
        execute_manual(Path(config.workspace), manifest.run_id)


def test_invalid_manual_span_stops_before_workspace(config_path, config_data, tmp_path):
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"test")
    with pytest.raises(ArtifactError):
        prepare_run(config_path, SourceInput(kind="local", locator=str(source_path)),
                    render_request=ManualRenderRequest(span=TimeSpan(start=0.0, end=1.0)))
    assert not Path(config_data["workspace"]).exists()
