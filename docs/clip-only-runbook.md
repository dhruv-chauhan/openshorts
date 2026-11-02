# Clip-only pipeline: Phases A–E

Phase A provides contracts, preparation, checksums, and a single-worker lock. Phase B adds the Foundry text adapter, capability preflight, and enforced media profile. Phase C adds bounded source ingestion, reusable word transcripts, and captioned vertical rendering of an explicitly supplied span. Phase C.1 adds separate story files, ordered sequence manifests, optional 1.1× playback, and per-story resume. Phase D adds market-policy selection from reused source/transcript artifacts. Phase D.1 adds reviewed market classification and exposure matching. Phase E adds final-audio auditing, explicit human review, and private Blob publication; Azure deployment follows in Phase F of the [implementation plan](architecture/clip-only-azure-implementation-plan.md). A successful manual render is an **unaudited checkpoint**, never approved content.

## Setup

Python 3.11+ on Linux or macOS is supported. The local lock uses POSIX `flock`; all workers on a host must use the same workspace. This is not a distributed lease.

From the repository root, install the small controller dependency set into your chosen environment:

```bash
python -m pip install -r requirements-clip-core.txt
```

No full `requirements.txt`, Docker, media libraries, Azure SDK, or credentials are needed for Phase A. The package never reads `.env` or secret environment-variable values during preparation.

For Phase B, install `requirements-clip-foundry.txt` instead. It includes the core requirements, the OpenAI v1 client, Azure Identity, and local JSON Schema validation; it does not install Google, ElevenLabs, or the media ML stack.

## Foundry setup and preflight

Use an existing Azure OpenAI/Foundry resource when available. Deploy a currently supported text model with strict structured outputs, using a standard pay-as-you-go SKU. The initial adapter evaluation uses GPT-5-mini with `max_completion_tokens`, omitted temperature, and `reasoning_effort: "minimal"`. Model availability, quota, and lifecycle are region/subscription specific; the deployment name is configurable independently for relevance, selection, and audit. All three may share one deployment initially.

Keep the real config under ignored `output/clip-only-azure/` or outside the repository. Copy a config and update its relative policy/market paths or use absolute paths. Set the inference URL to `https://YOUR-RESOURCE.openai.azure.com/openai/v1/` or the resource's `https://YOUR-RESOURCE.services.ai.azure.com/openai/v1/` endpoint, not the project or management URL. The live development probe passed on the `services.ai.azure.com` endpoint with Azure CLI identity and the default `https://ai.azure.com/.default` scope. Use the endpoint that passes preflight for the actual resource.

Authentication is explicit:

- `developer_identity`: uses only the signed-in Azure CLI identity (`az login`), with automatic token refresh. It does not search other developer credentials.
- `managed_identity`: uses only Azure Managed Identity; optionally set `managed_identity_client_id` for a user-assigned identity. Verify this again inside the eventual worker container.
- `api_key`: reads only the variable named by `api_key_env`. The value is never saved in configuration or usage output.

The identity needs inference data permissions on the resource. **Cognitive Services OpenAI User** covers OpenAI deployment calls. For the Foundry gateway, Microsoft documents a custom role with `Microsoft.CognitiveServices/accounts/MaaS/*`, or the broader Cognitive Services User role, for [keyless inference](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/configure-entra-id). The wildcard includes inference APIs beyond chat completions. Granular `MaaS/chat/completions/action` and `MaaS/v1/chat/completions/action` permissions exist, but did not resolve the development v1 probe's `PermissionDenied` response; do not assume those roles alone prove v1 access. Role configuration remains an operator action outside the preflight command. New role assignments can take time to propagate. The default token scope is `https://ai.azure.com/.default`; `https://cognitiveservices.azure.com/.default` is also an explicit supported setting. Neither authentication failures nor missing deployments fall back to API keys or another provider.

```bash
python -m pip install -r requirements-clip-foundry.txt
python -m clip_pipeline preflight --config output/clip-only-azure/config.json
```

This command makes **one small paid request per distinct deployment**, validates the structured response, and reports deployment roles and per-attempt usage as JSON. It verifies the configured token-limit parameter, optional temperature/reasoning settings, identity, and basic strict-schema support. It does not validate market API access, Blob access, media quality, or topic prompts that have not yet been implemented. It creates no run and approves no content. Exit 4 means the probe failed; the JSON error is safe to display. To retain a local report, redirect stdout and stderr to separate ignored files.

The adapter is available through `llm_backend.foundry_backend(config)` or `clip_pipeline.foundry.FoundryBackend(config)`. Use one context-managed instance per sequential run so its aggregate call/token limits cover all stages and retries. `generate(stage=..., system=..., payload=..., response_model=..., validate=...)` returns a validated Pydantic model; the optional callback enforces stage-specific semantic constraints, such as exact coverage of window IDs. Later stages must persist `usage_report()` with their artifacts and carry prior usage forward on resume; those stages are not wired yet.

`batch_payloads` preserves complete items and shared policy while counting the same request overhead used by the adapter. The context budget reserves configured output (including reasoning) and uses a conservative UTF-8 byte bound for byte-BPE text models. This intentionally under-fills the model context; it is not billed token usage. Oversized individual items fail rather than being silently truncated. Failed attempts without reported usage retain their reservation against the run budget.

SDK retries are disabled; the adapter owns `max_attempts`, bounded backoff, jitter, and Retry-After handling capped at 60 seconds. Invalid JSON/schema/semantic results get at most one repair request. Refusals, truncated output, authentication errors, and unsupported request settings remain explicit failures, never successful empty results. There is no downgrade to plain JSON or unrestricted text. Original Pydantic constraints are checked locally after the wire schema is adapted to the supported subset.

Usage reports include stage, deployment, returned model, request ID when supplied, duration, token counts, attempt number, and outcome. Missing token usage and unknown cost remain null. Configure `foundry.prices` keyed by deployment with `input_per_million` and `output_per_million` (USD) to get an estimate. Cached input uses the full configured input rate; the estimate is not a billing reconciliation. Raw prompts, response bodies, keys, and transcripts are excluded from reports. HTTP proxy environment variables are not inherited by the Foundry client.

## Media dependency boundary

Before importing legacy media modules in a fresh subprocess, call `activate_media_profile(config.media)` or supply `media_environment(config.media)` as its environment. Late activation is rejected. It disables dotenv, removes inherited Gemini/legacy LLM credentials and routing settings, and pins the optional Gemini layout/subtitle/hook/silent-selection paths off. Guards also reject legacy selection if a sentinel Gemini key is injected later. Local captions, face tracking, and GENERAL layout remain available; visual intelligence is deferred.

Legacy Gemini users retain their existing routing and response shapes. Neutral schemas and the permissive legacy parser now live in `llm_schemas.py` and `llm_json.py`, with compatibility re-exports in `gemini_worker.py`. Foundry uses its own strict parser. Phase C starts a fresh process for each ingest, transcription, and render stage. The profile also disables dubbing, inherited download proxies/cookies, audio normalization, and GPU compositing; it pins the local Whisper backend and PySceneDetect. ASR models are released in `finally` after transcription. Stage timeouts terminate the process group, including FFmpeg children.

## Build and render a manual span

Use the dedicated Linux amd64 image for the CPU baseline. It contains FFmpeg, fonts, Whisper, the real local detectors, and yt-dlp with Deno/EJS; it contains neither Google GenAI nor ElevenLabs. Deno/EJS are required by current [yt-dlp YouTube extraction](https://github.com/yt-dlp/yt-dlp/wiki/EJS). The main application's image/dependency set is not required.

```bash
docker build --platform linux/amd64 -f Dockerfile.clip --target runtime -t clip-worker .
mkdir -p output/clip-pilot
docker run --rm --platform linux/amd64 \
  -v "$PWD/output/clip-pilot:/data" \
  -v "$PWD/output/clip-models:/opt/models/huggingface" \
  clip-worker render --config /data/config.json \
  --input /data/source.mp4 --start 30 --end 60
```

Create `/data/config.json` in the mounted directory first, using `configs/clip-only.example.json` as a template. Use container paths for workspace and inputs; the bundled policy is `/app/configs/market-policy.example.json`. Mount a development market snapshot or configure the future API; manual rendering does not fetch the API or call Foundry. Set disk/source limits to the available scratch capacity. The default free-space floor is 20 GiB. The Hugging Face cache persists model weights; the first run downloads the configured model, so it needs network access. YOLO weights are included in the image. The runtime image is a local foundation for Phase F, not an Azure deployment.

Replace `--input` with `--url 'https://www.youtube.com/watch?v=abcdefghijk'` for a real single-video URL. This placeholder must be replaced. Direct yt-dlp downloads use the shared client policy and connect-time SSRF checks, refuse live/unknown-duration/oversized sources, and enforce a cumulative video-plus-audio download limit. They never truncate the source to fit a limit or switch to a paid proxy. Download restrictions/bot checks can still require an authorized local file. No playlist, browser-cookie import, or proxy provisioning is implemented here.

`--start` and `--end` are seconds on the full source timeline and must fit the configured duration band and actual source. `--transcript /data/transcript-artifact.json` optionally supplies a typed `TranscriptArtifact`, bound to the exact source SHA-256, duration, zero offset, language and real word timestamps. Raw ASR/caption JSON needs explicit conversion to that contract. Segment-only captions are rejected for rendering; word timestamps are never fabricated.

Whisper sometimes emits instantaneous tokens (`start == end`). The ASR adapter preserves their text by grouping them with an adjacent timed word using only the members' observed outer bounds. This reduces boundary precision and records `zero_duration_words_grouped` in the transcript and render report. All-instantaneous segments, overlaps, reversed timestamps, and malformed imports still fail. Downstream selection/audit must retain this warning and review cuts within grouped words.

Output JSON includes `final_media` and the manifest path. The final clip is H.264/AAC, 1080×1920 with square pixels; FFprobe and a full decode verify geometry, duration and streams. Canonical `clean.mp4` and derived `captioned.mp4` stay separate. ASS/SRT and layout ranges remain editable sidecars. The report flags source upscaling and cuts inside words; manual boundaries are preserved, not automatically snapped. Existing caption style and local layout behavior are retained. Hooks are refused until audited copy is available. `media.captions: false` delivers the clean artifact explicitly.

The successful manifest has `execution_kind: manual_render`, `stage: rendered`, `state: checkpointed`, and `outcome: null`. It performs no topic decision, final-audio audit, approval, or upload. No-audio/no-usable-speech inputs fail explicitly in this manual path; they do not invoke Gemini or claim no relevant matches.

```bash
python -m clip_pipeline resume --workspace /path/to/workspace \
  --run-id '<returned-run-id>' --render-only
```

Run resume in the same environment with the same mounted paths. It validates hashes and continues from the last committed stage after interruption. A completed manual checkpoint returns its existing output. Failed terminal runs are immutable: submit a new render, which can reuse valid source/transcript caches. Use a new run for changed boundaries/layout/captions/configuration; render caching across separate runs is not implemented yet.

Source blobs are keyed by content hash; YouTube locator entries permit reusing the downloaded bytes. Local files are recopied/hashed so changed bytes at the same filename invalidate reuse. Transcript keys include source bytes, model-weight hashes, ASR settings and package versions, or the exact supplied-transcript hash. Changes to render settings or policy reuse compatible ASR. Checksums reject corrupted caches. YouTube cache entries intentionally retain the first acquired bytes: use a fresh workspace to reacquire an edited remote video. Source/model/transcript caches and unfinished stage files are retained until operator cleanup; there is no automatic retention job yet.

## Manual story sequences (Phase C.1)

`render-stories` renders an ordered manual plan as **separate video files**. It makes no Foundry request, topic decision, content audit, approval or upload. The existing `render --start/--end` command retains its Phase C behavior and limits.

Start from `configs/story-plan.example.json`. Its URL, hashes, titles and boundaries are placeholders, not a runnable content sample. A real plan pins:

- `source`: the same YouTube locator or existing local input used for source acquisition.
- `source_sha256`: the exact ingested media hash from `source.json`.
- `transcript_sha256`: the hash of the canonical `TranscriptArtifact` JSON. The pipeline's generated `transcript.json` already uses this representation; for an imported JSON file use `digest(read_json_model(path, TranscriptArtifact))`, not a hash of arbitrary formatting.
- `sequences`: ordered groups with unique `sequence_id` and title; each contains one or more stories.
- Each story's unique `story_id`, title, contiguous source `span`, editorial role and `playback_rate` (1.0 default; 1.1 also supported). Optional subject, market IDs, speaker, timeframe, stance, exact evidence and source context remain unaudited manual annotations. Unknown values should stay null/`unclear`.

Use a Phase C run's source/transcript artifacts to author the first plan. Preserve the plan's source locator but use container paths for local files and configuration. A supplied transcript is optional; otherwise the configured ASR/cache must reproduce the pinned transcript. Changing ASR/model results requires reviewing and pinning the new transcript in a new plan.

```bash
python -m clip_pipeline render-stories \
  --config /path/to/config.json --plan /path/to/story-plan.json \
  --prepare-only

python -m clip_pipeline render-stories \
  --config /path/to/config.json --plan /path/to/story-plan.json \
  --stop-after-stories 1

python -m clip_pipeline resume --workspace /path/to/workspace \
  --run-id '<returned-run-id>' --stories-only
```

The same commands work in `clip-worker` after mounting the inputs and persistent workspace. `--transcript` optionally supplies a reviewed contract file. `--prepare-only` creates an immutable run without media work; use `resume --stories-only` to execute that same prepared run. Without `--prepare-only` or `--stop-after-stories`, rendering continues through the whole plan. `--stop-after-stories N` pauses after N *newly rendered* stories, exits successfully, and writes no sequence index until all planned stories are complete. It is also supported on story resume.

Plan/config validation happens before creating a run. Duplicate IDs, nonchronological/overlapping spans (including overlap across sequences), unsupported playback rates and hard budget violations are rejected. Actual source bounds/hash are checked after acquisition; transcript hash, evidence text/bounds and cuts inside timed words are checked before rendering. A manually supplied plan cannot prove sentence completeness, truthful attribution or market eligibility; these remain D/E checks and human review obligations.

The `stories` configuration is independent of legacy `selection` duration settings. Defaults are five sequences, five stories per sequence, 300 seconds per story and 900 seconds per sequence as hard limits on expected delivered duration. The preferred total sequence range is 60–180 seconds, recorded as a review flag rather than enforced by trimming, padding or dropping stories. There is no duration floor for a story beyond a positive span; choose complete ideas, not a minimum number of seconds. Limits never truncate a story or remove context to make it fit.

Every story uses its own output timeline: `(source_time - source_start) / playback_rate`. The renderer applies video timestamp and pitch-preserving audio tempo changes, retimes caption words and layout ranges, then burns captions. `timeline.json` records source/output word and evidence timestamps plus source-only context. Original source transcripts remain unchanged. Final media is independently decoded and checked against expected speed-adjusted duration. The image test also checks actual caption bounds and audio/video stream timing; these checks are not semantic audio audits.

Story-run state differs deliberately from the older single-span command:

- `story_progress` records each story's `pending`, `running`, `rendered`, `failed` or `interrupted` state, attempt count and committed artifact key.
- Completed story files, metadata and progress are committed atomically. Killing/interruption during a later story leaves earlier commits reusable. SIGTERM/SIGINT terminate the active media process group; SIGKILL/container loss may leave uncommitted files, which are not checkpoints.
- An operational error returns exit 4 with run state `retryable`. Explicit `resume --stories-only` retries unfinished stories. Interruptions return 130 and retain an `interrupted` run; an intentional `--stop-after-stories` pause returns 0. A corrupt committed file blocks resume instead of silently rerendering or approving it.
- Resume uses only pinned config/plan/transcript inputs. Original files changed outside the run are ignored. Editing pinned files breaks checksums. Submit a **new run** for changed rates, spans, labels, membership or settings; broader cross-run render reuse is not implemented.
- The final state is `checkpointed` at `rendered`, with `outcome: null` and `approved: false`. An empty manual plan produces an empty unaudited index, never `completed_no_matches`.

Outputs include per-story `story.json`, clean/final media, ASS/SRT, layout and timeline sidecars; separate `sequence-*.json` files; an ordered `sequences-*.json` index; and a static `preview-*.html`. CLI/status JSON returns `sequence_index`, `preview` and story progress. There is intentionally no single `final_media` for a story run.

All artifact paths in the JSON are relative to the **run directory**, not the sequence file's parent in a hypothetical relocated layout. Sequence manifests contain the exact ordered story specifications, artifact references/hashes, source/transcript/plan hashes and actual summed duration. They contain no delivery credentials or approval. The consuming backend can later resolve private artifact identifiers into authorized URLs after Phase E promotion.

Open the local preview to play stories independently and follow Previous/Next links. It exposes source spans, rates, provenance hashes and omitted intervals between stories; it escapes supplied text, loads no external scripts and never auto-approves. This is a pilot review page, not the consumer app's story feed. Pay particular attention to whether a viewer stopping after an earlier story would miss a qualification, and whether faster playback obscures numerical explanations.

## Market-policy story selection (Phase D)

Start with Phase C's typed `source.json` and `transcript.json`. Selection is a text-only checkpoint; it neither downloads media again nor starts a render. The current reference sample stays unchanged. For a different source, first acquire and transcribe it using the Phase C media workflow. End-to-end automatic approval remains outside this stage.

```bash
python -m clip_pipeline select --config /path/to/config.json \
  --source-artifact /path/to/source.json --transcript /path/to/transcript.json

python -m clip_pipeline resume --workspace /path/to/workspace \
  --run-id '<selection-run-id>' --selection-only
```

`select --prepare-only` pins inputs without API/inference calls. The source and transcript must match exactly and contain valid word timing. A source artifact is provenance supplied by the operator; the renderer rechecks actual downloaded/local media bytes before rendering. The selector never fabricates word times.

For the current pilot, copy `configs/clip-only.mock-tradexyz.example.json` and configure Foundry. Its explicit `mock_api` mode uses the [development TradeXYZ fixture](../configs/development/README.md), which includes 120 unique markets: 118 standard-index entries plus the Live pre-IPO ANT and OURA entries, retrieved on 2026-11-01. Converted pre-IPO entries remain in the standard set without duplication. It exercises the HTTP response/parser contract through an in-process transport, without opening a server or accepting arbitrary localhost URLs. This is development data, not the production Megastocks allowlist. `snapshot` remains an explicit offline alternative. Neither mode is a fallback from the production API.

In `api` mode the provider makes bounded HTTPS requests with redirects and inherited proxies disabled, connect-time SSRF checks, a 2 MiB response cap, schema validation and at most three attempts for transport/429/5xx failures. It pins fetch time, payload hash, server version and ETag. Conditional 304 responses refresh freshness; transient failure may reuse only a valid cached snapshot within `max_cache_age_seconds`. Missing credentials, authentication failures, redirects, invalid schemas and stale caches fail explicitly. Unknown asset classes require correcting/reviewing the universe; they are never silently treated as equities. Approval-time refresh belongs to Phase E.

The selector:

1. Evaluates every overlapping transcript window in bounded batches, including negatives and ambiguous content. Default input windows are 45 seconds, with one segment of overlap and at most four windows per request. These do not define story boundaries.
2. Uses the complete window map to expand surrounding context across batch boundaries and merge overlapping discussion regions. Regions exceeding the model context budget go to review; they are not truncated.
3. Proposes complete chronological stories with exact source evidence, subject, market connection, attributed stance, freshness, and explicit reasons for gaps between stories. The model selects existing first/last segment IDs and evidence/context segment IDs; code resolves the exact observed first/last word times and source quotes. It does not ask the model to recreate timestamps or quotations for story proposals. Automatic selection retains 1.0× speed. Every matched market must have its underlying name or an explicit alias spoken in that window/story (legal suffixes may be omitted); lexical presence is necessary but does not by itself establish substantive relevance.
4. Checks every policy rule for every story and sequence against original context. Required qualifications must stay in the same story when a viewer stopping there would otherwise be misled. A failed story withholds its entire sequence.
5. Ranks all surviving complete sequences before global deduplication and count limits. Distinct arguments and opposing views should survive; necessary stories are never dropped to fit a budget. The emitted sequence order follows the source. Budget exclusions and review reasons remain in the report.

The shipped policy includes standalone completion, chronology, entity disambiguation, stance attribution, ASR/numeric uncertainty and resistance to instructions embedded in source content. Unknown direction is `unclear`, not `neutral`; criticism alone is not a stock-price forecast. A macro-only discussion needs a spoken equity-market mechanism but no invented ticker association. ASR punctuation supplies an additional deterministic review signal: a cut starting or ending inside an ASR sentence is withheld for review even if the model marks it complete. Punctuation can be wrong and is not proof of semantic completion. Semantic checks are model judgments and remain subject to the independent final-media audit in E.

The larger mock universe needs more input budget than the original tiny fixture. The development example uses a conservative 262,144-token accounting ceiling (UTF-8 bytes are counted as an upper bound, not actual billed tokens) and 12,000 reserved output tokens per request, including reasoning. The development example uses GPT-5-mini-compatible `reasoning_effort: "medium"`; the earlier minimal-reasoning preflight only established API capability, not editorial selection quality. Verify these settings against your deployment's context and throughput limits; neither input nor output is silently truncated. Per-run call/token limits include failed attempts. Reservations are committed **before** each network call, so interruption cannot reset the budget. A response is cached only after schema/evidence/coverage validation; explicit resume reuses validated calls and pinned inputs. A changed prompt, policy, market universe, configuration or transcript requires a new run. No final approval is reused.

CLI output links `story_plan`, `selection_report` and a text-first `preview`. The report retains complete window decisions, all proposals, per-rule selection checks, global ranking, exclusions and uncertainty. The `foundry` plan binds source/transcript, policy, markets, prompts, settings and report hashes. The original C.1 manual-plan serialization stays compatible.

- `checkpointed` at `selected`: a nonempty unaudited plan is ready for editorial inspection and optional `render-stories --plan ...` using the same source/transcript/settings.
- `finished` / `completed_no_matches`: all source windows were evaluated and no complete eligible sequence remains. No render pool or filler clips are started.
- `finished` / `needs_review` (exit 3): ambiguity or a context/budget limit prevents confidently completing selection. Proposed text remains visible in the report/preview; the emitted render plan is empty. This is never reported as no matches.
- `retryable`: API/model/operational failure. Explicit `resume --selection-only` continues validated checkpoints under the original budget. Failed responses, refusals, missing IDs and malformed evidence cannot become empty success.

This CLI does not run final-audio ASR, approve clips, upload Blob artifacts, or deploy the Azure worker. Stage-specific metadata remains in the selection report even when the compact C.1 render plan carries only the renderable story fields.

Opt-in live prompt evaluation (paid Foundry calls, synthetic text only):

```bash
python tests/selection_live_smoke.py --config /path/to/config.json \
  --output output/clip-pilot/editorial-smoke.json
```

## Validate configuration

```bash
python -m clip_pipeline validate --config configs/clip-only.example.json
```

The example uses placeholder service endpoints. Validation checks configuration structure and the local policy, but does not assert that remote services exist or are accessible. Output explicitly contains `remote_services_checked: false`.

Relative workspace, policy, and market snapshot paths resolve against the **config file's directory**, not the shell's current directory. Relative `--input`, `--transcript`, and `--fixture` paths resolve against the shell's current directory.

Unknown fields are errors. Fixed vertical geometry, a zero clip minimum, one worker, and manual review are enforced. Secrets may only be referenced by environment-variable name; do not put key values or credential-bearing URLs into JSON. In `api_key` auth mode, supply `api_key_env`; identity modes reject that field. Production endpoint details and user media/transcripts should stay outside committed examples.

The API and development snapshot modes are explicit and mutually exclusive. There is no automatic fallback. API mode is **not fetched** during preparation; Phase D fetches and pins it when selection executes. Snapshot mode validates the provided local list. `blob: null` supports local preparation; a real storage target will be required before live promotion is implemented.

## Exercise the synthetic fixture

```bash
python -m clip_pipeline run \
  --config tests/fixtures/clip_pipeline/config.json \
  --fixture tests/fixtures/clip_pipeline/job.json \
  --prepare-only
```

This uses fictional markets, synthetic text and placeholder media hashes. No playable source or clip exists. The candidate is only a schema/evidence example, not an executed model result. The fixture is deliberately marked `synthetic: true`, and cannot be silently passed as a normal source.

The command returns a new run ID and manifest path beneath `output/clip-pipeline-fixture/runs/`. It stops at:

```json
{
  "execution_kind": "fixture",
  "stage": "configured",
  "state": "prepared",
  "outcome": null
}
```

A success exit code here means preparation succeeded, **not** that clipping or approval succeeded. A run without `--prepare-only` exits with code 6 and writes no run.

You may also prepare one real URL or local file, without processing it:

```bash
python -m clip_pipeline run \
  --config configs/clip-only.example.json \
  --url 'https://www.youtube.com/watch?v=abcdefghijk' \
  --prepare-only

python -m clip_pipeline run \
  --config configs/clip-only.example.json \
  --input /path/to/source.mp4 \
  --transcript /path/to/transcript-artifact.json \
  --prepare-only
```

The video ID above is a placeholder. URLs must identify one YouTube video; tracking parameters are stripped. A local file must exist, but its media bytes are not read in Phase A. An optional supplied transcript must conform to `TranscriptArtifact` in `clip_pipeline/contracts.py`; raw Whisper output needs a later import adapter. Preparation validates transcript structure, not its correspondence to uninspected source media. No source ownership or permission is inferred.

## Inspect and resume preparation

```bash
python -m clip_pipeline status \
  --workspace output/clip-pipeline-fixture --run-id '<returned-run-id>'

python -m clip_pipeline resume \
  --workspace output/clip-pipeline-fixture --run-id '<returned-run-id>' \
  --prepare-only
```

These commands verify every saved artifact's size/hash and the run fingerprint. Resume takes the workspace lock and uses pinned snapshots; it does not reread changed original configuration, policy, market, or fixture files. `--prepare-only` performs no downstream execution and does not rewrite the run; `--render-only` explicitly resumes a manual render. There is no `rerun`, `review`, or `promote` command yet; submit a new run to capture changed inputs.

## Files and reproducibility

Each prepared run contains:

- `manifest.json`: run identity, state, outcome, checksummed artifact references and input fingerprint.
- `resolved-config.json`: expanded defaults and resolved local paths, with secret names only.
- `policy.json` and `submission.json`: pinned policy and normalized source request.
- `market-snapshot.json`: only in explicit snapshot mode.
- `fixture.json`: only for a synthetic fixture run.
- `supplied-transcript.json`: only for an explicitly provided transcript artifact.

Identical resolved inputs in the same workspace produce the same fingerprint but distinct run IDs and timestamps. Fingerprints are not a promise of identical future LLM output. Manual runs additionally pin `render-request.json`; `input_artifact_keys` separates fingerprint inputs from later stage outputs. Each subprocess writes in a unique `stages/` directory, with source/transcript/render artifacts, result metadata and a diagnostic log. Only checksummed files committed into the manifest constitute a checkpoint.

Input artifacts cannot be overwritten through the store. Files are written via temporary file, flush/fsync and rename; the manifest is committed last. An interrupted preparation may leave an uncommitted run directory, which is not a resumable run. Do not treat directory existence as completion. The OS releases the lock when the worker exits or is killed; never remove an active `.worker.lock` file to bypass contention.

The contracts also define lifecycle stage/outcome rules, source/transcript/candidate/final-media boundaries, and hash-bound audit results for later phases. Contract construction is not a substitute for the actual media checks, semantic policy checks, and approval gates.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Validation, preparation, inspection, live preflight, or manual rendering succeeded; not content approval. |
| 2 | Invalid command/configuration/input or corrupt checkpoint. |
| 3 | Reserved for a future `needs_review` outcome. |
| 4 | Local I/O, provider preflight, or other operational failure. |
| 5 | Another process owns the workspace lock. |
| 6 | Requested execution is not implemented in this phase. |
| 130 | Interrupted by the operator. |

Successful command output is JSON on stdout. Runtime validation errors use JSON on stderr and omit input values/validator contexts to avoid echoing accidentally pasted secrets. Argument-parser usage errors use the standard argparse format.

## Tests

```bash
python -m pytest tests/test_clip_pipeline_config.py \
  tests/test_clip_pipeline_artifacts.py tests/test_clip_pipeline_cli.py \
  tests/test_clip_pipeline_foundry.py tests/test_clip_pipeline_isolation.py \
  tests/test_clip_pipeline_media.py tests/test_clip_pipeline_stories.py -q
```

The isolation test starts a fresh interpreter that blocks media/cloud/app imports and network connection/DNS operations. Other tests cover invalid configuration, source/evidence/timestamp contracts, pinned resume inputs, corruption, atomic write failure, lock contention and process death, and distinguishing preparation from completed output.

Phase B tests exercise the real OpenAI SDK through a mocked HTTP transport: strict schemas, repairs, refusals, truncation, HTTP/network errors, bounded retries, budgets, batching, usage, auth configuration, and preflight deduplication. The separate CI job installs no Google SDK and tests fresh-process media imports with local heavyweight detectors stubbed to avoid weight downloads. This proves the provider import/guard boundary, not actual ML rendering. Live Azure preflight is an explicit operator command, never an automatic CI test.

Phase C's separate image test uses actual Whisper and detector weights, generated speech, FFmpeg and fonts, with no network at runtime and no mocked media modules:

```bash
docker build --platform linux/amd64 -f Dockerfile.clip --target media-test -t clip-media-test .
mkdir -p output/clip-media-smoke
docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 6g \
  -v "$PWD/output/clip-media-smoke:/data" clip-media-test /data/smoke
```

Use a new `/data/smoke-*` directory for each invocation so the initial cache-miss assertion remains meaningful. `smoke-report.json` records real-model execution, absence of Google SDK, transcript reuse, same-filename source invalidation, verified resume, imported word timestamps, and GENERAL/full-width/portrait output checks. This passed locally, including visual inspection of a captioned frame. Generated speech/test-pattern footage does not establish face-tracking quality or content suitability on representative videos. `.github/workflows/clip-media.yml` runs this test for relevant PRs or manual dispatch; it does not make paid model calls.

The first real YouTube sample also passed acquisition, full-source transcription, manual rendering and offline source/transcript reuse. Frame inspection confirmed speaker tracking, with inherited captions overlapping the broadcaster's lower third. That remains a visual-review/tuning item; technical success does not imply publishable output. The integration test and initial sample used `tiny` Whisper for speed; the configured baseline default remains `small`. No ASR accuracy or Azure throughput claim follows from these tests. Sample artifacts and diagnostics remain under ignored local output.

Phase C.1 extends the same real image test with `tests/story_media_smoke.py`: two separate story files at 1.0×/1.1×, an operator checkpoint, SIGTERM during a real media worker, resume without changing the earlier file, caption/stream timing checks, and a changed-rate plan that starts with pending output. The result is saved under `stories/story-smoke-report.json` within the smoke directory.

## Phase D.1: reviewed market enrichment

Copy `configs/clip-only.enriched.example.json` and configure Foundry/identity as for
Phase D. Its `enrichment.mode: "snapshot"` selects the six-entity pilot knowledge file.
Use `enrichment: {"mode": "off"}` (also the default) for a direct/macro baseline.
`configs/enrichment.example.json` illustrates the knowledge contract using fictional
entities; see `configs/development/README.md` for pilot coverage, citations and curation.

The existing `select` and `resume --selection-only` commands operate unchanged. A new
selection pins knowledge before inference, and resume reads only that snapshot. Changing
profiles, categories, relationships or their dates requires a new selection run. Source
media and the timestamped transcript can be reused. The baseline now supplies explicit
spoken-ID hints to the window model; it does not relax the direct-name validator.

Selection classifies a complete discussion, generates candidates from sourced category
memberships and at most one relationship edge, then assesses every candidate. Association
metadata has its own schema version and distinguishes direct matches from editorial
inferences. `matched_market_ids` still means named direct subjects. Multiple associations
can carry different/unknown exposure directions; these are never speaker-attributed
bullish/bearish forecasts. Unsupported/uncertain optional links remain report diagnostics.
Only supported links are eligible for app display, subject to Phase E's final-media audit and human approval.

Configuration limits:

- `max_candidates_per_story` (default 30): exceeding it produces review, not a partial
  successful assessment. Context overflow likewise withholds rendering.
- `membership_max_age_days` (365) and `relationship_max_age_days` (90): freshness is
  measured at the pinned run creation date. Applicability dates are checked independently.
- All profile links are filtered by the enabled market universe. Missing profiles do
  not disable a direct/macro path. No graph traversal beyond one edge is performed.

Unknown or unsupported full calendar event dates remain unknown. The selector accepts
explicit ISO or full English month/day/year dates; it does not invent the day from a
quarter, year, or video publication date. Time-sensitive inferred links with unknown
applicability require review. An independently relevant direct/macro story can survive
optional link uncertainty, but material source-number/date/context uncertainty still
withholds the sequence. This conservative date rule is a pilot limitation, not a claim
that all legitimate financial discussions state a complete date.

The local HTML preview includes source evidence IDs, association decisions, knowledge
record IDs, citations, coverage gaps and reasons. It clearly labels inferences as editorial
metadata. Generated source titles/captions must not include inferred claims. If rendering
an enriched plan, provide its exact knowledge snapshot in the render configuration; the
renderer pins it and preserves metadata in each story artifact and sequence manifest.
It performs no new media processing for enrichment and still makes no approval claim.

To evaluate synthetic classification/association cases against a real Foundry deployment
(opt-in paid inference; no real media):

```bash
python tests/enrichment_live_smoke.py --config '<configured-json>' --output '<local-report.json>'
```

For the reference comparison, use the same source/transcript and corrected 120-market
catalog in both modes. Retain both run IDs, prompt/policy/model settings, knowledge hashes,
coverage/review reasons and token usage. The older 118-market result cannot measure the
benefit of enrichment because it omitted ANT. Association precision and recall require
manual labels; a single video's output count is not evidence of general accuracy.


## Phase E: final-media audit, review and private storage

Install `requirements-clip-storage.txt` for the controller, or the dedicated media
requirements/image for local ASR. This adds the pinned Azure Blob SDK without Google,
ElevenLabs or the legacy cloud application. Keep real configs, transcripts and reviewer
identities in ignored local output. The baseline still requires a human reviewer.

Audit a completed `render-stories` checkpoint using the policy and markets in your
configured JSON. The audit imports verified render artifacts into its own run; it does
not mutate the render or reuse a source transcript as final-audio evidence.

```bash
python -m clip_pipeline audit --config '<configured-json>' \
  --render-workspace '<render-workspace>' --render-run-id '<render-run-id>'
```

Optional configuration:

```json
"audit": {
  "final_audio_asr": true,
  "require_manual_review": true,
  "whisper_model": "small",
  "context_padding_seconds": 45.0
}
```

`audit --prepare-only` pins inputs without media/model execution. `audit --media-only`
performs media checks and final-audio ASR, then stops before Foundry. Resume that run
with `resume --workspace … --run-id … --audit-only`; adding `--media-only` reuses
completed inspections without model calls. Completed model calls and the token budget
are durable across retries. Operational errors remain retryable, never “no matches.”
Changed audit prompts or input settings require a new audit, with compatible ASR cache
reuse. A copied audit run has its own artifact root; the original render is not resumed.

A completed semantic audit exits **3** and returns the local preview, audit bundle and
pending review template. The preview plays each story separately in order and includes
source context, final transcript, timed captions, policy results, evidence paths and
media hashes. It flags omitted source intervals. Inspect the entire video/audio for
framing, charts, banners, captions, synchronization and complete boundaries. The text
model does not inspect images; visual acceptance belongs to the human reviewer.

Copy the template and fill in the actual human reviewer, UTC review timestamp, each
item's decision and `watched_entire_item`. For every review issue on an approved item,
provide its `issue_id` and a concrete evidence-backed resolution (at least 20 characters).
Leave pending items pending; never invent a review. Every planned story must be approved
before its sequence can be approved. A reject-level audit issue cannot be bypassed.
If the resolution changes media, metadata, ordering or membership, create the revised
plan/render and audit it again. A note claiming a correction does not change the output.

```bash
python -m clip_pipeline review --workspace '<audit-workspace>' \
  --run-id '<audit-run-id>' --decisions '<completed-review.json>'
```

To publish, configure the destination **before preparing the audit**:

```json
"blob": {
  "account_url": "https://YOURACCOUNT.blob.core.windows.net",
  "runs_container": "clip-runs",
  "approved_container": "clip-approved"
}
```

Blob uses the Azure CLI identity when Foundry `auth_mode` is `developer_identity`;
otherwise it uses managed identity, including the configured client ID if present.
Foundry API keys never authenticate to Blob. Provision the scoped Blob data permissions
and verify them on the Azure worker in Phase F. Containers are created private when
absent; existing public containers are refused. No SAS URL is embedded in manifests.

```bash
python -m clip_pipeline promote --workspace '<audit-workspace>' --run-id '<audit-run-id>'
```

Promotion refreshes API markets with zero stale-cache allowance before uploading and
again before the commit. Changed eligibility/identity of a used market, changed
knowledge, or an expired supported inference requires new audit/review. Explicit mock
or snapshot mode remains usable for development and is marked in the commit; it must
not be confused with the authoritative backend API.

Objects use deterministic keys under
`approved/<source-sha>/<audit-run-id>/<review-sha>/`. Each sequence manifest contains
ordered stories, private media/caption keys and hashes, timing, display metadata,
source permission status, policy/knowledge/model provenance and audit results. Separate
run-container evidence includes final-audio inspections. All remote bytes are checked
before writing `commit.json` last. Consumers must discover **only commit manifests**,
never raw clip prefixes. The consuming backend resolves private delivery URLs.

Repeat `promote` after interruption; immutable existing objects must match both size
and SHA-256. A failed upload has no discovery commit. Once publication has started,
review changes require a new audit run because the remote commit may already exist
when a local acknowledgement fails. Local workspace files must remain available for
retry; remote reconstruction after VM loss is a Phase F deployment task.

The initial E validation used the existing C.1 reference clips, independent `small`
Whisper ASR and live Foundry audits. It did not approve withheld D.1 proposals. The
reference is still unapproved: captions and metadata need further attention, and the
auditor can overflag unknown speaker/stance or quarter context. This is an audit/review
baseline, not a claim of editorial precision across channels. SDK storage validation
used local Azurite; Azure RBAC has not yet been tested for storage.

The offline media CI smoke now also runs `tests/audit_media_smoke.py` on generated
speech: final-media checks, fresh final-audio ASR, checkpoint reuse, no Foundry calls,
and no Google SDK. Its `tiny` model override is only for the synthetic test; the
reference validation and default audit configuration use `small`.
