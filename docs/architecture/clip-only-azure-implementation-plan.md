# Clip-only Azure pipeline: implementation plan

Date: 2026-11-01  
Repository baseline reviewed: `459716b`  
Status: Phases A–D implemented and pushed; Phase C is `ac14215`, C.1 is `1c83090`, and D is `ff9f680`. Phase D's execution path and exit criteria are validated; the reference run completes with `needs_review` and withholds its story proposals. Phase D.1 is implemented and validated locally (uncommitted): reviewed market classification and exposure matching precede Phase E. The corrected-catalog baseline and enriched reference both return `needs_review`; no content approval is claimed. Editorial refinement, final-media audits, approval and Azure worker deployment remain outstanding. See the [clip-only runbook](../clip-only-runbook.md) for commands and limitations.

## 1. Outcome and scope

Build a manually triggered, single-video pipeline that runs on one Azure worker. It downloads a YouTube video or accepts a local source, produces or reuses a timestamped transcript, identifies complete relevant discussions using Microsoft Foundry, renders separate vertical story clips, audits each story and its sequence, and stores approved artifacts in private Azure Blob Storage. The consuming app presents the clips in order so viewers can stop after a complete idea or continue deeper. Iterate on the same sample until the user is satisfied with the content and presentation.

The editorial objective is to preserve a market argument and its storyline while removing unrelated material, repetition and unnecessary setup. Each story is one contiguous source span with a natural beginning and local completion. A sequence groups related stories in source order; it is metadata over separate media files, not a concatenated video. Source gaps are explicit in provenance. App navigation between files does not by itself fix an abrupt cut or missing context; boundary and completeness checks remain required.

The first milestone deliberately pulls topic rules, a final audit, and approved storage forward from the originally suggested second increment. Without them, a technically successful render would not establish that the output meets this use case.

The eventual workload is a configurable list of third-party channels and a daily accepted-output target. These are future planning inputs, not a quota that may lower content standards. Initial execution processes exactly one explicitly submitted source, one job at a time. No channel poller, scheduler, distributed queue, database, public API, dashboard work, or automatic publishing is needed for this milestone.

### Required baseline behavior

- Only Clip Generator capabilities: original source audio, local transcription, vertical reframing, captions, optional source-grounded hook text.
- Microsoft Foundry for all baseline LLM calls; no Google/Gemini network calls or credentials required.
- Two relevance paths: allowed underlying market/company OR equity-market macroeconomic relevance.
- A backend API supplies the allowed-market list. A deliberately selected local snapshot supports development before the endpoint exists.
- Phase D.1 adds separately versioned market profiles: preserve the spoken story subjects, allow multiple relevant categories and evidenced affected markets, and keep inferred exposure distinct from attributed source claims. Profiles cannot grant market eligibility.
- Zero qualifying clips is a normal successful outcome.
- One coherent market discussion per sequence; independent discussions become separate sequences. Preserve questions, claims, evidence, conditions, attribution and opposing views where needed for meaning.
- Separate, independently addressable story files plus a versioned ordered sequence manifest for the consuming app. No stitched compilation, generated bridging narration, cross-video assembly or app implementation in this milestone.
- Independent audit of each final rendered story and the ordered sequence; manual visual review during this narrow pilot.
- Checkpoints and immutable run artifacts make iteration cheap and explainable.
- One Azure VM running one containerized worker, with private Blob artifacts and managed identity.
- Configuration separates topic policy, inference deployments, media settings, storage, and operational limits.

### Explicitly deferred

Channel discovery, broad caption scraping as a prefilter, Service Bus, PostgreSQL, fleet autoscaling, cross-channel semantic deduplication, social publishing, translation/dubbing, AI Shorts, thumbnail generation, public galleries, billing, Remotion editing, and automated factual research.

Automatic layout selection, screencast detection, burned-in subtitle detection, visual hook grounding, and silent-video selection are not migrated in this baseline. Disable their Gemini paths explicitly. Local face tracking and the GENERAL blurred-background layout remain available. If the sample depends on readable charts or existing captions, use a reviewed conservative full-width layout/caption setting; bring forward the relevant Foundry vision stage only if that cannot produce acceptable results.

## 2. Verified repository findings and consequences

| Existing location | Finding | Implementation consequence |
|---|---|---|
| `main.py:get_viral_clips` | Two-pass transcript selection, global top-window shortlist, fallback to first windows, clip-floor recovery | Reuse pure window/boundary helpers; introduce a separate policy selector that never falls back to unqualified windows or fills a minimum count. |
| `clip_selection.py` | Default shortlist capped at ten; default count ceiling reaches twelve; minimum overrides clamp to at least one | New profile uses zero-to-maximum selection and batched eligible-window detail passes. Preserve legacy helper defaults for existing callers. |
| `gemini_worker.py` | Shared schemas, prompts, JSON parsing, and Google SDK dependencies are mixed | Extract provider-neutral compatibility contracts/parsing and introduce separate market-policy schemas. |
| `llm_backend.py` | OpenAI-compatible text requests, permissive response-format fallback, static bearer key, all costs reported as local/zero | Add an explicit Foundry provider with strict outputs, identity auth, typed errors, and honest usage accounting. |
| `main.py` CLI | `--transcript` exists; download can start selection concurrently through `EARLY_AUDIO`; checkpoints are removed after completion | New orchestrator owns stage ordering and durable reuse. Do not invoke the legacy all-in-one CLI to run the new pipeline. |
| `main.py` and `reframe_v2.py` | Heavy model initialization and import-time flags; renderer imports `main` back | Initialize the media subprocess after applying validated flags. Make Google imports lazy while avoiding a broad renderer rewrite. |
| `main.py` render loop | Cuts, reframing, hook/caption outputs, layout sidecars, and final delivery paths are coupled | Extract a reusable single-clip render operation with an explicit final artifact result. |
| `app.py` | Mixed product endpoints, in-process queue, S3 completion backup, nonempty-render assumption | Do not deploy or import `app.py` in the clip-only worker. Its status/storage behavior does not govern new jobs. |
| `layout_picker.py`, `screencast_layout.py`, `burned_subtitles.py`, `hook_grounding.py` | Direct Gemini vision calls | Disable these features through an enforced profile; tests must catch hidden calls even when stray Gemini credentials exist. |
| `transcribe_backends.py` | Local word-level ASR and explicit `release_models()` | Reuse transcription and release resources after source and audit ASR. |
| `Dockerfile`, `docker-compose.yml` | Full product image/services; default command starts FastAPI | Add a dedicated image/entry point and local single-worker Compose profile. |

Repository guidance currently requires the legacy clip floor. The user's requested strict-relevance pipeline intentionally has different semantics. Implement those semantics in the new profile and preserve the old floor for the existing product; do not weaken existing tests or change public product behavior as a side effect.

## 3. Architecture and execution contract

```text
Manual CLI submission
  -> validate config + acquire run lock + resolve market snapshot
  -> ingest exact source + record provenance and media hash
  -> load validated transcript OR transcribe once
  -> assess every transcript window -> map complete discussions and context dependencies
  -> classify discussions using pinned profiles -> validate direct and inferred market associations (D.1)
  -> propose ordered story sequences -> snap complete boundaries -> deterministic validation
  -> render each contiguous story separately to staging, with optional modest speed adjustment
  -> inspect media + transcribe final audio + audit each story and the sequence
  -> manual visual review for pilot
  -> revalidate market eligibility if changed
  -> upload approved story artifacts + ordered sequence manifests + final commit manifest
```

### New package boundary

Use a small `clip_pipeline/` package. The orchestrator depends on explicit operations for market snapshots, inference, media processing, and artifact storage. Keep typed Pydantic contracts and stage functions; avoid a generic workflow engine or plugin framework.

Run the media portion in a subprocess. This isolates import-time renderer flags, ASR/GPU memory, and per-run environment from the controller. Start with one job and one clip-render worker. The same job payload can later be delivered from a queue without changing the stage contracts.

### Proposed commands (not implemented yet)

```bash
python -m clip_pipeline run --config configs/clip-only.example.json --url '<youtube-url>'
python -m clip_pipeline run --config configs/clip-only.example.json --input '<local-video>' --transcript '<transcript-artifact>'
python -m clip_pipeline resume --run-id '<run-id>'
python -m clip_pipeline rerun --run-id '<run-id>' --from selection --config '<revised-config>'
python -m clip_pipeline review --run-id '<run-id>' --decisions '<review-json>'
python -m clip_pipeline promote --run-id '<run-id>'
```

`resume` continues an interrupted run with the same resolved configuration. `rerun` creates a new run linked to its parent, reusing only compatible artifacts. Never overwrite a prior run's decisions or approved files. Promotion is idempotent and refuses unreviewed, rejected, failed, or incompletely uploaded clips.

### State and errors

Separate stage progress from terminal outcome. Stages: `configured`, `ingested`, `transcribed`, `selected`, `rendered`, `audited`, `reviewed`, `stored`.

Terminal outcomes:

- `completed`: all candidate decisions resolved and approved outputs durably committed.
- `completed_no_matches`: valid evaluation found no eligible/acceptable clips; store a result manifest with reason counts, no approved media.
- `needs_review`: one or more clips require operator judgment; never silently promote them.
- `partial_failure`: some candidates could not be rendered/audited/stored; retain completed work, expose unresolved failures, and support retry.
- `failed`: ingestion, configuration, transcript, provider, or infrastructure failure prevented valid evaluation.

No-audio/sparse-speech footage is a recorded unsupported-source/no-match outcome for this transcript-only profile, not a Gemini fallback. A technical ASR failure is a failure, not evidence of no relevant speech. Likewise, provider refusal, incomplete window coverage, and malformed JSON are not negative relevance decisions.

Use machine-readable reason codes and stage/candidate IDs. Return zero for resolved successful outcomes, including zero matches; use distinct documented nonzero CLI codes for review-required and operational failures. Future queue consumers must interpret outcomes, not retry every nonzero code blindly.

## 4. Configuration and market API

Store nonsecret settings in validated JSON, with secrets/identity supplied separately by environment or Azure identity. Reject unknown fields and incompatible settings. Resolve and save a redacted configuration snapshot at run start. Changing configuration cannot silently mutate a running job.

| Configuration area | Baseline decision |
|---|---|
| Input | One URL or local video; optional validated transcript; full-source timing initially |
| Provider | `foundry`; independent deployment fields for relevance, selection, and audit; initially may point to the same deployment |
| Foundry endpoint | Azure OpenAI v1 inference base URL; distinguish it from a Foundry project/management endpoint |
| Authentication | Managed identity on Azure; developer identity locally; explicit secret-backed key mode only if required |
| Market source | `api` or explicit development `snapshot`; no silent switch between them |
| Topic policy | Versioned policy file; separate from market membership |
| Enrichment (D.1) | Explicit `off` baseline or reviewed local snapshot; versioned taxonomy, profiles, sources and bounded direct relationships. Configure freshness/materiality rules and candidate/context budgets; never silently invent missing profiles. |
| Selection | Complete discussions and ordered stories; zero eligible output allowed. Provisional preferred total viewing time 60–180 seconds per sequence, not per story and not a mandatory floor/ceiling. Story length follows local completion; configurable hard duration/count/compute limits route incomplete proposals to review or failure, never silent truncation. |
| Story budgets | Separate maximum sequences, maximum stories per sequence, story/sequence duration limits and token budgets. Maximum five stories per sequence is an initial configurable review budget, not an instruction to drop necessary context. |
| Media | Fixed 9:16; target 1080x1920, H.264/AAC, square pixels; separate contiguous story files; original audio; captions on; hook overlay off initially |
| Pacing | Compare 1.0× with optional 1.1× playback in the pilot. Record the rate per story; maintain pitch and A/V sync. Default 1.0× until reviewed; do not accelerate dense numerical explanations or charts merely to meet a duration target. |
| Layout | Local TRACK/GENERAL behavior; explicit reviewed preserve-full-width option; no automatic vision selection |
| Concurrency | One job; one render at a time; CPU Whisper/x264 initially; GPU settings optional later |
| Audit | Final-audio ASR + independent semantic check + deterministic media checks; manual visual approval required |
| Storage | Local workspace/checkpoints plus private Blob output; separate staging and approved artifacts |
| Limits | Source duration/size, inference timeouts/tokens/call budget, scratch-disk threshold, retry counts, retention; no silent source truncation |

Defaults above are starting points for sample iteration, not throughput or quality guarantees. Start the pilot with one language supported well by the selected ASR and model, and record that language explicitly. Unsupported or ambiguous language goes to review/configuration handling; do not translate or dub.

### Backend API contract

The backend remains authoritative. Agree the exact route and authentication when available; no concrete production endpoint or credential belongs in this repository.

```json
{
  "schema_version": "1",
  "version": "opaque-revision-id",
  "markets": [
    {
      "market_id": "xyz:EXAMPLE",
      "underlying_name": "Example Corporation",
      "asset_type": "equity",
      "aliases": ["Example Corp"],
      "enabled": true
    }
  ]
}
```

`EXAMPLE` is a fictional schema illustration, not an allowed instrument. Support the actual market classes returned by the backend; explicitly review unknown classes instead of treating them as equities. Prefer backend-curated entity relationships, or a separately versioned reviewed mapping, over model-invented subsidiaries, executives, competitors, or index membership.

- Require unique stable IDs, nonempty underlying names, explicit enabled flags, supported schema version, and bounded response size.
- Record fetch time, server version, response hash, and ETag if supplied. Snapshot the exact payload used for a run.
- Fetch once per run, then pin the snapshot across relevance, selection, and audit.
- In API mode, bounded retries may use a cached snapshot only within an explicit freshness limit; reject stale/missing state. For the baseline, pause the run rather than partially evaluating against an unknown universe.
- Before promotion, conditionally refresh the API. If membership changed, re-evaluate affected ticker-dependent clips under the new snapshot and record both versions. Macro-only eligibility does not require an invented ticker association.
- An offline snapshot is an explicit development dependency, never a substitute presented as current production state. Require a real snapshot before accepting real company-specific clips; synthetic fixtures are for tests only.
- Treat all API payloads and transcript text as data. They cannot change system instructions, provider endpoints, tools, or credentials.

## 5. Foundry migration

### Provider implementation

Add an explicit Foundry branch to `llm_backend.py`, using the OpenAI Python client against a supported Azure OpenAI v1 deployment. Use `azure-identity` token providers for automatic token renewal. The currently documented scope is `https://ai.azure.com/.default`; validate endpoint, scope, RBAC, and actual selected deployment in the Azure smoke test.

Use deployment names, not assumptions about model names. GPT-4.1-mini is an initial evaluation candidate, subject to availability and lifecycle at implementation time. Independently configure the auditor so it can later use a stronger deployment without changing stage logic. Do not require two paid deployments just to begin.

Use typed structured responses with strict schemas and local semantic validation. Make schemas conform to the deployment's supported JSON Schema subset, including required/nullable fields and additional-properties handling. Preserve legacy compatible-provider fallback only for legacy callers; the Foundry policy path must not silently downgrade to unrestricted text.

Handle:

- 429/transient server/network failures with bounded exponential backoff, jitter, and `Retry-After` where available.
- Authentication, missing deployment, unsupported schema/model parameters, and content refusals as explicit nonretryable classifications unless the service documents a transient condition.
- Truncated output, missing/duplicate window IDs, invalid evidence, and parsing failures as incomplete evaluation, never empty success. At most one bounded repair/retry for repairable output; no unlimited loops.
- Per-stage input/output token usage, elapsed time, request IDs, deployment/model metadata, retry counts, and estimated cost only when configured prices exist. Unknown cost is null/unknown, not zero or `local=true`.
- Model-specific parameters: do not send `temperature=0.2` unconditionally to models that do not support it. Explicit deployment capability settings and a startup probe catch incompatibility.

### Untangle shared code

Move existing reusable text schemas to `llm_schemas.py` and JSON parsing to `llm_json.py`. Re-export compatibility names from `gemini_worker.py` to avoid breaking legacy callers/tests. Keep new topic/evidence/audit schemas in `clip_pipeline/contracts.py`.

Make Google SDK imports lazy in legacy Gemini-only execution paths of `main.py`/`gemini_worker.py`; handle annotations that otherwise require Google types at import time. Baseline worker imports must succeed without `google-genai` installed. Do not remove Google support from unrelated legacy features.

### Enforced no-Gemini media profile

Apply flags before importing media modules in the subprocess:

```text
EARLY_AUDIO=0
AUTO_LAYOUT=0
SCREENCAST_LAYOUT=0
SUBTITLE_BAND_CHECK=0
HOOK_GROUNDING=0
AUTO_HOOK=0             # initial default; configurable after copy audit is proven
AUTO_CAPTIONS=1
CLIP_WORKERS=1
FFMPEG_ENCODER=x264
WHISPER_DEVICE=cpu
```

Also explicitly configure optional split/speaker/punch-in features off initially. Reject unsupported profile combinations. The new orchestrator never calls `get_visual_clips` or legacy selection. Prevent `.env` loading or inherited request keys from reactivating Gemini paths; absence of a key alone is not the guard.

Test this in a clip-only image without the Google SDK and with sentinel Gemini credentials in the test environment. Instrument provider calls to prove all model inference routes to the configured Foundry deployment. Source/model downloads remain separate network dependencies.

## 6. Topic policy and selection

### Accepted content

Path A: views, opinions, analysis, news, updates, explanations, education, or advice substantially relating to an allowed market's underlying asset or company.

Path B: macroeconomic factors with a direct, evidenced equity-market connection, such as political decisions affecting corporate economics, earnings, borrowing costs, inflation/rates, investor sentiment/positioning, liquidity, or capital flows. A specific ticker is optional for this path.

An eligible clip need not mention TradeXYZ or Megastocks. A platform mention does not itself qualify an unrelated clip. Educational content may qualify through a clear explanation of a relevant market mechanism without a current news event.

### Editorial rules

1. Relevance must be substantive and supported by exact transcript spans. A passing mention is insufficient.
2. For macro, identify the mechanism connecting the subject to equities; do not invent a causal argument absent from the clip/context.
3. Exclude generic politics, personal finance, product reviews, celebrity news, or unrelated markets unless the relevant market connection is actually established.
4. Exclude advertisements, sponsor reads, housekeeping, and unrelated calls to action.
5. Preserve a complete idea, necessary antecedents, question/answer context, conditions, uncertainty, and time horizon. Do not sever a rebuttal in a way that reverses the speaker's meaning.
6. Treat opinions and advice as eligible content types. Preserve attribution and distinguish claims, forecasts, and reported facts. Do not filter merely because the auditor disagrees with the view.
7. Do not invent names, numerical claims, certainty, investment recommendations, urgency, or calls to trade in generated copy.
8. Preserve currencies, units, percentages, price levels, reporting periods, and historical context. Ambiguous material numbers require review.
9. Avoid duplicate/near-duplicate moments within the video; retain distinct arguments and opposing perspectives.
10. No minimum clip count. Eligibility and completeness outrank hook strength and predicted popularity.
11. Retain context even when it is less engaging. A necessary qualification cannot be deferred to a later story if the current story would mislead a viewer who stops watching. Never split a sentence, question/answer dependency or claim/condition solely to fit a duration target.
12. Keep stories in original source order initially. Rank complete sequences for relevance/usefulness; do not rearrange statements within a sequence for a stronger hook. Do not make an answer appear to respond to a different question.
13. Each story has a recognizable subject and a complete local idea. Later stories may add evidence or detail, but must not rely on unexplained pronouns or missing essential premises. Expand a contiguous span or omit/review it when context cannot be preserved; do not fabricate a bridge.
14. Record stance as market + attributed speaker + timeframe + `bullish`, `neutral`, `bearish`, `mixed`, or `unclear`, supported by evidence. Missing directional information is `unclear`, not `neutral`. Criticism of a business or management is not automatically a stock-price forecast; preserve disagreement rather than inventing a consensus.

Represent rules with stable IDs, severity (`reject` or `review`), rationale, and positive/negative examples. Keep thresholds configurable but do not treat self-reported model confidence as calibrated probability. Include freshness categories (`time_sensitive`, `evergreen`, `uncertain`) and source/event dates where established; unknown dates remain unknown.

### Selection algorithm

Phase D implements the direct-market/macro baseline below. Phase D.1 extends discussion
classification and association validation without changing source fidelity, story
completion, full coverage or sequence ordering. Its detailed contract is in Section 11.

1. Normalize transcript segments and word timestamps, assign stable IDs, and evaluate all overlapping windows in bounded batches. Windows are model input units, not final story boundaries.
2. Map complete discussions across windows: subject/markets, attributed speakers, questions, claims, evidence, qualifications, conclusions, timestamps and context dependencies. Reconcile adjacent-window duplicates before proposing stories.
3. Validate complete coverage, allowed market IDs and exact evidence references. Missing responses are operational errors, not negative relevance decisions. Review the full source before applying output budgets.
4. Group eligible discussions by coherent thesis into sequence proposals. Preserve necessary supporting context even where a sentence alone scores low for engagement/relevance. Remove tangents, promotions and repetition without losing conditions or counterarguments. Permit zero proposals.
5. Propose an ordered list of contiguous source spans per sequence. Each span must reach local completion; stories remain in source order. Retain source intervals omitted between stories and reasons for omission so the auditor can inspect them. No legacy first-window fallback or fixed ten-window shortlist.
6. Snap to observed word boundaries, then validate sentence/idea completion, finite source bounds, evidence containment, nonoverlap, source order and configured budgets. Respect grouped-word timing warnings. If context requires expanding a span, revalidate it; if a hard limit prevents completeness, split only at a genuine idea boundary or route to review.
7. Check the sequence as a whole and each story on its own for essential context, truthful stance attribution, useful progression and nonredundancy. Rank whole sequences; do not discard a necessary story to fill a count/duration budget. Label budget-limited coverage and never refill a minimum after rejection.
8. Emit the C.1 typed story plan with evidence-backed titles/market labels and optional hooks. Editorial roles may include context, claim, explanation, evidence, qualification or conclusion; do not force every discussion into every role. Supporting app labels cannot replace a missing spoken qualification. No platform descriptions, engagement CTAs or generated narration are needed.

## 7. Transcript and media reuse

For the first sample, download the source once and transcribe locally. Do not add a separate caption-scraping service yet. Accept a supplied transcript only with explicit provenance and a known timing basis; allow a reviewed import step to bind a raw transcript to the source.

Transcript artifacts include schema version, source content hash, video ID when applicable, duration, source offset (zero initially), language, ASR model/backend/version, segments, and words. Validate monotonic finite timestamps, text/word consistency, source bounds, and adequate coverage. Segment-only captions can support relevance, but obtain word timing before precise cuts/captions; never invent timing by evenly spreading words.

Use content hashes rather than filename/duration alone for cache identity. Keep separate keys:

- Transcript: source media hash + ASR configuration/version.
- Selection: transcript hash + market snapshot + policy/prompt/schema versions + deployment configuration; D.1 also binds the enrichment mode and exact taxonomy/profile/relationship snapshot hashes.
- Story render: source hash + contiguous span + playback rate + caption/hook text + render settings/code version. Sequence order is not baked into the media.
- Sequence: ordered story IDs/final hashes + labels + topic/stance/context metadata + policy/schema versions.
- Audit: exact story media hashes and sequence manifest hash + source context/omitted intervals + audit policy/prompt/model configuration; D.1 associations and their pinned knowledge/evidence references also form part of the audit input.

A policy change can reuse source/ASR but invalidates selection and subsequent approval. A caption or hook change invalidates final audit even if boundaries are unchanged. An audit-only change reuses rendered candidates. A resume validates checksums; incomplete files never count as checkpoints.

A changed playback rate invalidates that story's captions, render and audit. Changing order, membership or app-visible labels invalidates sequence audit/review/approval even if the story bytes are unchanged; compatible story media may be reused. C.1 adds compatible per-story resume/reuse, while broader cross-run render caching remains a later optimization. No change may reuse an old sequence approval.

Persist artifacts with atomic local writes and stage manifests. Retain source and transcript across successful sample iterations until explicitly cleaned; do not inherit the legacy successful-run checkpoint deletion. Proposed development defaults: checkpoint/source retention seven days, rejected render retention one day, approved retention explicitly configured with no automatic deletion initially. These are editable operational defaults, not user-mandated retention policy.

### Rendering contract

Extract `render_one_clip` from the current nested render loop into a reusable operation. Return actual final paths, media metadata, layout ranges, and failures. Keep canonical clean video and derived caption/hook output distinct; always audit/promote the actual final delivered bytes.

Preserve existing FFmpeg cuts, local tracking, caption styling/font handling, and fallback behavior. Normalize the final output to 1080x1920 with square pixels in this profile, including already-vertical passthrough sources. Upscaling does not create source detail; flag weak source quality for review. Preserve original audio and A/V sync. Keep captions editable through sidecar artifacts.

In C.1, call the single-span seam for each planned story and retain separate clean/final files and sidecars. Do not concatenate spans, add artificial transitions, or join separate statements inside a story. Emit a sequence manifest for app navigation. Initial ordering is chronological; any later reordering feature needs an explicit policy change and new sequence audit.

If a story uses playback rate `r`, its expected duration is `(source_end - source_start) / r`, within an explicit encoding tolerance, and its local timeline maps source time `t` to `(t - source_start) / r`. Apply this consistently to audio/video, ASS/SRT events, layout ranges and evidence/review timestamps. Burn captions after retiming; preserve the original source-clock transcript for reuse. Pitch-preserving tempo adjustment uses the original voice and requires no dubbing service. Test 1.0× and 1.1× against comprehension, numbers, pitch, sync and readable captions. Do not treat file-to-file navigation as evidence that starts/ends sound natural.

Charts, multiple speakers, persistent news tickers, or burned-in captions may need a preserve-full-width layout or disabled added captions. Record manual layout/caption decisions in config. Do not automatically crop away market information using a face-only assumption. Do not add visual-model migration until the single sample demonstrates a concrete need.

## 8. Audit and pilot review

### Deterministic checks

After rendering, use `ffprobe` and decode checks to verify dimensions, sample/display aspect ratio, codecs, nonempty video/audio streams, decodability, finite duration, and expected duration within an explicit frame/time tolerance. Check timestamp mapping, configured duration band, caption event bounds, and final artifact hashes. Silence/clipping indicators can flag review; the mere presence of an audio stream does not establish intelligibility.

For story sequences, also verify unique IDs, increasing nonoverlapping source spans, one contiguous span per story, all manifest references/hashes, explicit playback rates, retimed evidence/captions and total duration from actual output files. Missing or failed story files prevent publishing the planned sequence.

### Independent content audit

Transcribe the final rendered audio locally, then give the auditor:

- Final clip transcript, selected source transcript, and bounded surrounding source context.
- Generated title/hook and the exact caption text/events used to render.
- Pinned market snapshot and policy, source publication date, and any established event date.

Do not supply the selector's popularity score or persuasive explanation as audit evidence. A separate call is independent in workflow, not a statistical guarantee; the same underlying model/ASR can share errors.

Return typed per-rule results, supporting spans, attribution/number/context issues, freshness classification, and `pass`, `reject`, or `needs_review`. Compare final-audio text to the intended source span and flag material discrepancies. ASR uncertainty must not be silently treated as proof of a false statement. The audit evaluates relevance and source fidelity, not external factual truth.

Any critical rule failure rejects the clip. An unresolved material ambiguity routes to review. Transient audit failure remains operational failure and must not approve or reject on the merits.

Audit the ordered sequence separately using the relevant full discussion, omitted source intervals, retained stories, app-visible labels and proposed stance evidence. Check that omission/order does not change meaning, hide disagreement, or postpone a material qualification past the point where a viewer may stop. Each story must pass its own audit; all passing stories do not automatically imply a passing sequence. If a story fails, withhold the sequence. Removing or replacing it creates a revised sequence that must be re-audited and reviewed; never silently close the gap. Stance tags are descriptions of attributed source views, not externally verified investment conclusions.

### Manual visual review is part of baseline acceptance

Generate a local static HTML report with relative video previews, source timestamps, selected text/context, rule outcomes, caption/hook copy, and before/after run comparison. Use escaped text, no external scripts, and no embedded production tokens. Generate review JSON with explicit clip IDs and final hashes.

For every clip approved during the pilot, inspect the full clip for framing, chart readability, duplicate/broken captions, face obstruction, audio quality, A/V sync, natural start/end, and preserved context. Record reviewer decision/time and artifact hash. Automatic semantic pass alone is insufficient while `require_manual_review=true`.

The review report must support playing each story alone and stepping through the ordered sequence, with source timestamps, omitted intervals and playback rate visible. Inspect caption overlap with broadcast banners and comprehension after speed adjustment. Bind sequence approval to the exact ordered manifest and its story hashes. This is a local review interface and app handoff contract; implementing the consumer app's story player/navigation remains outside this repository's baseline.

A reviewer may resolve genuine ambiguity with evidence or reject a clip. Hard failures require a corrected render/policy decision and another audit; a generic review command must not bypass them. Later disabling manual review is a deliberate configuration/product decision after evaluation, not an automatic consequence of one good sample.

## 9. Storage and one Azure worker

### Initial Azure footprint

- One Linux VM with Docker and managed identity; manually run the CLI inside a pinned container image. No public application listener.
- Azure Container Registry for the clip-only image; use identity-based pull permissions appropriate to the registry's RBAC/ABAC mode.
- An Azure OpenAI/Foundry deployment with text structured-output support, existing if available.
- Private Blob containers for run/checkpoint artifacts and approved clips. Use identity-based data access, not an S3 compatibility assumption.
- Key Vault only for unavoidable secrets such as backend API authentication if it cannot use identity.
- Structured application logs available through Azure Monitor; detailed transcripts stay in controlled artifacts rather than routine logs.

Parameterize region, VM SKU, scratch-disk size, resource names, existing Foundry endpoint, deployment names, and identity. Begin with CPU Whisper/x264 for functional correctness; size the disk for source, model cache, intermediates, candidates, and reruns. Benchmark before selecting final CPU/GPU sizing. One sample cannot justify production throughput claims.

Use a persistent data disk for model/source/checkpoint cache. Ensure the container identity path can reach Azure's identity service and validate it inside the running container, not just on the host. Plan management access through the chosen Azure administrative path, with no broadly exposed SSH/application ports. Required outbound access includes YouTube/source acquisition, Foundry, Blob, the market API, and reviewed model downloads during provisioning.

### Artifact layout

```text
runs/<run-id>/manifest.json
runs/<run-id>/resolved-config.json
runs/<run-id>/market-snapshot.json
runs/<run-id>/transcript.json
runs/<run-id>/selection.json
runs/<run-id>/story-plan.json
runs/<run-id>/sequences/<sequence-id>.json
runs/<run-id>/audits/<clip-id>.json
runs/<run-id>/audits/sequences/<sequence-id>.json
runs/<run-id>/review.json
runs/<run-id>/report.html
staging/<run-id>/<clip-id>/...
approved/<source-id>/<run-id>/<clip-id>/clip.mp4
approved/<source-id>/<run-id>/<clip-id>/provenance.json
approved/<source-id>/<run-id>/sequences/<sequence-id>.json
approved/<source-id>/<run-id>/commit.json
```

Local workspaces additionally retain source media and caches. Upload checkpoint artifacts after stages; source upload is an explicit optional retention setting. On VM loss, recover available checkpoints and re-acquire any source not backed up, verifying its hash before reuse.

Upload approved media and provenance first, verify lengths/checksums, then write the commit manifest last. Blob multi-object upload is not a transaction: consumers must discover approved items only through commit manifests, never by listing raw media prefixes. A retry uses deterministic keys and checksums; interrupted uploads cannot appear as completed output. Local atomic manifests plus this commit convention are enough for one worker; distributed coordination is deferred.

The approved sequence manifest lists the exact ordered approved story IDs, artifact keys/hashes, actual durations, playback rates, source spans, titles, market labels and attribution/stance metadata. It contains private artifact identifiers, not embedded credentials or expiring signed URLs. The consuming backend resolves authorized delivery URLs separately. Final commit manifests reference sequence manifests by hash; neither an incomplete sequence nor an individually rejected story can be exposed through an approved sequence. Unaffected complete sequences may be committed only with explicit per-sequence results; unresolved technical failures still produce `partial_failure` for the run.

Record source URL/channel/video identifiers, publication date, source timestamps, title/speaker attribution when known, market matches, snapshot/policy/model versions, media hashes, audit and review decisions. Track third-party source permission/licensing status separately; adding a channel or passing a topic audit does not assert ownership. Never blanket-populate the legacy rights acknowledgment.

## 10. File-level implementation map

Paths are proposed additions unless marked existing. Keep application changes limited to the new baseline and the reusable seams it needs.

| File | Action and responsibility |
|---|---|
| `clip_pipeline/__init__.py`, `clip_pipeline/__main__.py` | Add package and CLI commands; no API server imports. |
| `clip_pipeline/config.py`, `clip_pipeline/contracts.py` | Typed configuration, source/transcript/candidate/audit/run contracts, validation, redacted snapshots. |
| `clip_pipeline/runner.py` | Stage ordering, run lock, resume/rerun, outcomes, cancellation handling, bounded retries. |
| `clip_pipeline/artifacts.py` | Local atomic manifests, checksums, cache invalidation, artifact identities. |
| `clip_pipeline/markets.py` | Backend fetch/auth boundary, snapshot validation/cache, final eligibility refresh. |
| `clip_pipeline/selection.py` | Topic gates, batched detail extraction, zero-count semantics, evidence checks, candidate ranking. |
| `clip_pipeline/stories.py` (C.1) | Typed manual story-plan validation, chronological sequence manifests, context/identity references and mapping between source and per-story output timelines; no concatenation. |
| `clip_pipeline/audit.py` | Final-audio/source comparison, independent Foundry audit, deterministic media checks, approval rules. |
| `clip_pipeline/execution.py`, `clip_pipeline/media_worker.py`, `clip_pipeline/media_common.py` | Sequential controller, isolated media subprocesses, bounded execution, atomic checkpoints and deterministic media checks. |
| `clip_pipeline/ingest.py`, `clip_pipeline/transcript.py`, `clip_pipeline/render.py` | Bounded source acquisition, source-bound ASR/import/cache, and reusable single-clip render operation; final-audio audit follows in E. |
| `clip_pipeline/storage.py` | Private Blob uploads, checkpoint persistence, idempotent final commit manifests. |
| `clip_pipeline/report.py` | Escaped static review report, comparison view, review decision validation. |
| `tests/test_clip_pipeline_stories.py`, `tests/media_smoke.py` (C.1) | Ordered separate outputs, rate/timing mappings, incomplete sequence recovery, approval invalidation and real-media sequence tests. |
| `configs/clip-only.example.json` | Nonsecret worker/provider/media/storage settings with placeholders. |
| `configs/market-policy.example.json` | Versioned topic/editorial policy, thresholds and review rules; no real backend list duplicated here. |
| `clip_pipeline/prompts/relevance.txt`, `selection.txt`, `audit.txt` | Versioned Foundry prompts. Include as runtime assets in the image; separate trusted policy from transcript data. |
| `llm_schemas.py`, `llm_json.py` | Extract provider-neutral legacy schemas/parser so Foundry does not import Google. |
| **Existing `llm_backend.py`** | Add explicit Foundry configuration/client, strict structured output, identity/token refresh, typed errors, usage. Preserve legacy interface. |
| **Existing `gemini_worker.py`** | Re-export moved compatibility symbols; lazy Google-only imports. Preserve legacy Gemini behavior. |
| **Existing `main.py`** | Extract callable single-clip render helper, lazily import Gemini-only code, preserve final paths/layout metadata; leave legacy CLI and floor behavior intact. |
| **Existing `clip_selection.py`** | Reuse pure helpers; add explicit new-policy variants only when needed. Preserve existing default count/floor behavior. |
| **Existing `transcribe_backends.py`** | Reuse unchanged unless explicit ASR settings/resource lifecycle cannot be passed safely; any change must preserve current defaults. |
| **Existing `reframe_v2.py`, `ffmpeg_utils.py`, `subtitles.py`** | Reuse first; narrowly change only if necessary for explicit output normalization or render result contract. No aesthetic rewrite. |
| `requirements-clip-core.txt`, `requirements-clip-foundry.txt`, `requirements-clip-media.txt`, `Dockerfile.clip` | Separate controller/provider/media dependencies and CPU runtime with bundled fonts. Nonroot Azure deployment, persistent-volume ownership and `docker-compose.clip.yml` remain in F. Exclude Google, billing, dubbing, social, dashboard, and Remotion dependencies. |
| **Existing `.dockerignore`, `.gitignore`** | Only adjust if needed to exclude new local secrets/reports/snapshots and include intentional runtime assets. |
| `infra/azure/clip-worker.bicep`, `infra/azure/clip-worker.example.bicepparam` | Parameterized VM/data disk/identity/storage/registry/logging resources and role bindings; existing Foundry input. No deployment in planning work. |
| `ops/clip-worker-bootstrap.sh` | Idempotent Docker/workspace setup and identity-based image pull; no secrets baked into images or logs. |
| `docs/clip-only-runbook.md` | Local/Azure setup, model preflight, sample run, review, rerun, recovery, storage cleanup, known visual limitations. |
| **Existing `docs/architecture/clip-selection.md`, `layouts-and-reframing.md`** | Describe the optional policy profile and its disabled visual features; keep legacy product documentation accurate. |
| **Existing `README.md`** | Add a concise optional clip-only profile link after implementation works; no broad rewrite. |
| **Existing `.github/workflows/ci.yml`** | Add lightweight clip-policy/provider/storage tests and clip-image build checks; separately mark heavyweight media/Azure smoke tests. |

Do not modify `app.py`, `cloud/`, `dashboard/`, dubbing, social publishing, or Remotion for the initial deployed worker. They remain in the repository for legacy use but are not run. If extraction exposes an unavoidable legacy seam, document it and keep the patch narrowly compatible rather than moving the whole application.

## 11. Ordered implementation tasks and exit criteria

### A. Contracts and isolated execution

Implemented: isolated `clip_pipeline` package, typed contracts/configuration, preparation-only CLI, local input snapshots/manifests, single-workspace lock, and synthetic fixtures. `run` and `resume` require `--prepare-only`; they stop at `configured` with no terminal outcome. Live source/provider/media execution and selective stage reruns remain in later phases.

1. Add configuration/contracts, CLI, artifact workspace, state/outcome handling, and one-job lock.
2. Add placeholder-backed config and synthetic policy/market fixtures.
3. Define exact source/transcript/candidate/final-artifact contracts before refactoring media code.

Exit: a fixture run validates configuration and writes a reproducible manifest without importing heavy ML modules or `app.py`. Invalid settings fail before downloading or spending inference tokens.

### B. Foundry adapter and dependency isolation

Implemented and committed: explicit `FoundryBackend` behind `llm_backend.foundry_backend`,
refreshing identity/API-key authentication, strict schemas and local validation,
typed failures, bounded retries/repair, usage accounting, conservative token
batching, and `clip_pipeline preflight`. Neutral legacy schemas/parser are
extracted with compatibility re-exports; a fresh media profile prevents dotenv
and inherited credentials from enabling Gemini paths. Pinned provider requirements
and a separate CI environment without Google SDK cover the import boundary.
Heavyweight local detectors are stubbed in that light import test; the carried-over
real media image check is implemented in C. Azure worker validation remains in F. The live Azure smoke result
is recorded in the operator's ignored local preflight report. Validation passed:
mocked provider tests, the environment without Google SDK, and a real v1
structured-output request to GPT-5-mini using Azure CLI developer identity through
the resource's `services.ai.azure.com/openai/v1/` endpoint. The account-scoped
Foundry inference permission was explicitly approved and applied. The one shared
deployment was verified for relevance, selection, and audit configuration; stage
prompts and media execution remain in later phases. VM managed-identity access
still requires verification inside the eventual worker container.

1. Extract neutral schemas/parser and implement Foundry client/auth/errors/usage.
2. Make media import graph work without Google SDK; enforce profile flags.
3. Add token-bounded request batching and capability preflight.

Exit: mocked provider tests pass; a real Azure smoke request returns schema-validated data using the selected deployment/identity; malformed/refused/truncated responses remain explicit errors. No hidden Gemini calls.

### C. Source, transcript reuse, and rendering seam

Implemented and committed in `ac14215`: `render --start/--end` and `resume --render-only`, guarded direct YouTube/local ingestion, source hashing and provenance, validated word-transcript import, content/model/settings-keyed ASR reuse, isolated stage subprocesses, and `render_one_clip`. The seam composes the existing cutter, renderer and caption helpers without changing the legacy render loop. Clean/final paths and caption/layout sidecars are distinct; output is normalized and fully decoded to check 1080×1920 square-pixel H.264/AAC delivery. A completed manual render remains an unaudited `checkpointed` run, never an approved result. Sparse/no-audio sources fail explicitly in this manual path; selection outcomes follow in D.

`Dockerfile.clip`, the media requirements and `tests/media_smoke.py` close the pending real-image test without Google SDK: actual model imports, speech ASR, caption rendering, unchanged-source reuse, same-name changed-source invalidation, full-width and portrait input, and checkpoint resume passed locally with networking disabled. No heavyweight modules were mocked in this test. Lightweight controller/cache/error tests also pass. Automated content audit, final-audio ASR, acceptance of sample aesthetics, remote cache refresh, retention cleanup and cross-run render caching remain later work. See the [runbook](../clip-only-runbook.md) for commands and boundaries.

1. Reuse URL validation/SSRF protections and downloader; local-file fallback remains available.
2. Implement source hash/provenance and validated transcript import/cache.
3. Extract single-clip render operation and exact final-path contract.
4. Run known source boundaries through fixed 9:16 output before involving selection.

Real-sample testing exposed instantaneous Whisper tokens. The adapter groups these with adjacent timed words using observed outer bounds, preserves all text, and records a timing warning. It does not invent sub-word timestamps; future selection/audit must retain the warning when checking boundaries.

A real YouTube sample was acquired and transcribed in full, then manually rendered. A second span reused both source and transcript with networking disabled. Final media checks and frame inspection passed; the inherited caption position overlaps the source's broadcast lower third, so visual acceptance is explicitly pending quality iteration. These local CPU tests use the `tiny` ASR model for integration validation, not a transcript-accuracy benchmark or production model selection. Real URLs, transcripts, reports and clips stay in ignored local output.

Exit: a manually specified span produces a playable captioned vertical clip. Repeating with unchanged source/ASR reuses the transcript. Changing the source invalidates it. No dubbing, Google, social, S3, or full app startup occurs.

### C.1. Story-sequence extension before Phase D

Implemented locally; replaces the previously discussed concatenated-video "assembly extension" with **logical sequencing of separate story clips**. Phase C's original exit criteria remain complete. The consumer app is outside this repository change.

1. Add versioned `StoryPlan`, `StorySpec`, `StoryArtifact` and `SequenceManifest` contracts. A plan pins source/transcript hashes and has zero or more coherent sequences. A story has a stable ID, one source span, editorial role, title, source-grounded subject/market and speaker/timeframe references, optional evidence-backed stance, playback rate and context/evidence references. A rendered sequence adds ordered artifact references/hashes and actual durations. Unknown speaker/timeframe/stance stays explicitly unknown; manual plans need not invent topic decisions that D has not performed.
2. Separate preferred sequence viewing time, hard story/sequence limits, story/sequence count budgets and playback settings from the legacy clip-duration defaults. Use the proposed 60–180-second total sequence target as a soft preference, not a mandate to stretch sparse content or omit necessary context. Begin with source ordering, one video, zero minimum output, 1.0× default and optional reviewed 1.1×; retain the existing single-span CLI for compatibility.
3. Add an explicit manual command such as `render-stories --config ... --plan ...` with validation before media/model work. Reject duplicate IDs, reversed/overlapping spans, nonchronological order, mismatched source hashes and unsupported rates. Check actual media bounds after ingest. Full topic eligibility and semantic completeness remain D/E gates, not properties inferred from a manual plan.
4. Reuse C's source and transcript. Render one story at a time to separate files with optional pitch-preserving speed adjustment and correctly retimed captions/layout/evidence. No cross-span joins or synthetic speech. Publish each local story checkpoint atomically, then the local sequence manifest only after all required files validate; manual sequence outputs remain unaudited and unapproved.
5. Extend the runner, CLI status and worker protocol for explicit per-story progress and resume. Interrupt during a later story and reuse previously verified completed stories on resume. Preserve the intended order and failed-story status; no missing story may be silently dropped. Rate/span/config changes invalidate affected output; sequence changes invalidate sequence approval.
6. Provide a minimal local preview for independently playing stories and moving through their order, plus JSON for eventual app integration. Include source spans, rates, hashes and unaudited status. Define app consumption fields without adding public API endpoints, feed logic or the consumer UI.
7. Test a manually reviewed sequence from the selected video at 1.0× and optional 1.1×. Add synthetic boundary/context fixtures and a real image test without Google SDK covering separate outputs, captions/sync, exact geometry, cached transcript reuse, sequence order, interruption and rate-change invalidation. Carry the known lower-third caption overlap into visual review; do not claim acceptance solely from decoding checks.

Files: extend `contracts.py`, `config.py`, `__main__.py`, `execution.py`, `media_worker.py`, `render.py` and artifact/resume handling; add `stories.py` and focused tests. Add the minimal preview through `report.py` or a small reusable precursor, and update the example configuration, runbook and media-image test. Preserve A–C compatibility or explicitly version/migrate artifact schemas when needed.

Implementation details: `render-stories --config ... --plan ...` and `resume --workspace ... --run-id ... --stories-only` operate on pinned manual plans. Optional `--stop-after-stories N` pauses after N newly completed stories. The separate `stories` configuration defaults to five sequences, five stories per sequence, 300 seconds per story and 900 seconds per sequence; 60–180 seconds is a soft sequence preference. Supported playback rates are exactly 1.0 and 1.1. Source/transcript hashes, word boundaries, evidence, artifact checksums and ordering are validated. Story outputs include clean/final media, captions, layout ranges and source/output timeline mappings; completed sequences expose typed JSON and an escaped local HTML preview. Retryable failures and interruptions preserve completed story checkpoints. Changed plans/settings require a new run; cross-run render caching is deferred. Empty manual plans remain unaudited rather than claiming no relevant matches.

Validation: 1,584 backend tests passed, 57 skipped; the isolated environment without Google SDK passed 124 tests with one legacy compatibility test skipped. The offline media-image test rendered separate files at both rates, checked geometry/caption bounds/audio-video timing, interrupted a real later-story worker with SIGTERM, resumed without changing the completed story, and verified rate changes create fresh pending work. The story seam bounds normalization to the requested speed-adjusted duration to remove duplicate tail frames produced by legacy scene concatenation. Legacy single-span behavior is unchanged. These checks establish media/control behavior, not semantic content approval or acceptance of transcript quality.

Selected-video validation: the same requested sample produced two independently playable stories, 37.338 seconds at 1.1× and 82.416 seconds at 1.0×, in an ordered sequence totaling 119.754 seconds. Both source and transcript were cache hits. Pausing after the first story and resuming preserved its bytes, modification time and attempt count. Both files passed media/timing/caption-bound checks. Inspected frames with `preserve_full_width` keep the original broadcast banner and place added captions below it. The reused tiny-model transcript still contains ASR errors and grouped-word timing warnings; user review of pacing, completeness and caption quality remains necessary. Ignored local `output/clip-media-smoke/phase-c1-validation.json` records exact artifact locations and test evidence; source media, transcript and real plan are not committed.

Exit: a manually authored plan produces multiple independently playable captioned 9:16 files plus an ordered, hash-bound, unaudited sequence manifest; source/ASR are reused; speed and caption mappings are correct; interrupted later stories resume without discarding verified earlier outputs. The sequence can be inspected one story at a time. **No concatenated video is produced.** This establishes the contract that Foundry selection will populate in D; content approval remains E.

### D. Market policy and topic-aware story selection

Implemented and pushed in `ff9f680` through a separate `select` CLI over Phase C source/transcript artifacts. `resume --selection-only` retains immutable inputs, validated model responses and usage reservations committed before requests. No render starts during selection. A selected plan binds source/transcript, policy, market snapshot, prompt, settings and report hashes; it can later be inspected and supplied to C.1's renderer.

The user requested a development mock containing every market in the TradeXYZ specification index. The explicit `mock_api` transport uses the same bounded HTTP/schema/ETag path as production and pins 120 unique entries retrieved on 2026-11-01: 118 standard-index instruments plus Live pre-IPO ANT and OURA. All eight Converted pre-IPO entries already exist in the standard set and are not duplicated. It is not the production Megastocks allowlist. The API provider supports bounded transient retries and fresh-cache reuse, rejects authentication/schema/redirect failures, and records provenance. No promotion-time refresh is claimed before E.

The selector evaluates every overlapping window, reconstructs surrounding discussions across batch boundaries, validates exact evidence/allowed IDs, and checks all policy rules for every proposed story and sequence. Automatic proposals retain 1.0× speed; actual C.1 hard story/sequence limits apply independently of legacy viral-clip durations. Complete surviving sequences are globally ranked/deduplicated before applying the output count budget. Every gap, withheld proposal, uncertainty and budget exclusion stays in the report. Context overflow or unresolved ambiguity emits `needs_review`, not a negative match or a truncated story. Purely negative complete coverage emits `completed_no_matches` without rendering. Source-grounded semantic completeness remains a model judgment requiring E's independent final-media audit.

New files: `markets.py`, `selection_contracts.py`, `selection_prompts.py`, `selection.py`, `selection_runner.py`; extended contracts/config/CLI/provider budget persistence and preview; `configs/clip-only.mock-tradexyz.example.json`, development market/provenance fixtures, versioned story policy, focused provider/selection tests, and an opt-in `tests/selection_live_smoke.py`.

Validation: 1,621 backend tests passed, 57 skipped; the isolated environment without Google SDK passed 161 with one legacy compatibility skip. Seven live synthetic editorial cases passed, including clear nonfinancial alias matches, adversarial instructions, unresolved identity, macro-only relevance and an unlisted supporting company with an explicit economic link to an allowed company. The real C.1 checkpoint still loads unchanged in the no-Google image.

With the original 118-entry standard-only snapshot (before adding ANT/OURA), the same reference video completed all ten window assessments, proposal generation and pre-render policy checks in five successful model calls with no retries. It proposed one sequence containing three stories and returned `needs_review`: the first cut starts inside an ASR sentence, and the sequence contains an ambiguous price-move claim and unspecified event/quarter years. No renderable sequence or approval was emitted. A completed resume reverified the checkpoint without making model calls. The source/transcript hashes match the C.1 reference artifacts. The ignored local `output/clip-media-smoke/phase-d-validation.json` records exact reports/preview paths and validation evidence.

Live validation led to three concrete safeguards: matched market IDs require a spoken name/alias in the relevant window/story; unknown attribution resolves to `unclear`; and cuts inside ASR sentences require review independently of the model's completion judgment. Proposal generation now selects existing segment IDs; code derives observed word boundaries and exact source quotes instead of asking the model to recreate timestamps/evidence. Unlisted supporting entities can remain in a source-explicit explanation of an enabled company's economics without receiving invented market IDs. The development model uses medium reasoning; minimal reasoning was insufficient for these contextual decisions. The approved deployment capacity increase was applied successfully. Boundary quality, source dating/number attribution and tiny-model ASR accuracy remain editorial issues to resolve before accepting this sample or progressing to final-media approval.


1. Implement API/snapshot provider and policy prompts/schemas.
2. Classify all windows, reconstruct complete discussions across batches, and generate C.1 story plans with source-grounded subject/stance/context metadata. Validate evidence/IDs, contiguous spans, complete boundaries and chronological progression.
3. Implement zero-match outcomes, bounded sequence/story budgets and within-video deduplication without deleting necessary context. Preserve qualifications even when they are less engaging; do not force directional sentiment or short durations.

Exit: synthetic positive, negative, macro-only, ambiguous-name, mixed/unclear stance and adversarial transcript cases behave correctly. Context crosses input-window boundaries without loss; question/answer and claim/qualification cases produce complete chronological stories. A valid no-match run emits no sequences and terminates without an empty render pool or manufactured clips. Legacy floor tests still pass.

### D.1. Reviewed market classification and exposure matching

**Status and objective:** implemented locally after D and before E; not yet committed. Identify
complete stories on their own terms, then associate them with one or more relevant
business categories and allowed markets. Avoid forcing a multi-company argument onto
one named ticker. Preserve D's direct-market and equity-macro paths as a measurable
baseline. No graph database, automated research agent or new Azure resource is needed.

#### Implementation and validation record

D.1 adds `enrichment_contracts.py` and `enrichment.py`, explicit off/snapshot
configuration, versioned optional story metadata, pinned knowledge in selection and
render runs, and hash-compatible loading of older plans. The selector classifies
complete proposed discussions, evaluates bounded candidates from memberships and one
relationship edge, and independently checks supported associations with the existing
pre-render policy pass. Direct spoken IDs retain their original semantics. Optional
uncertain/unsupported links remain diagnostics; unresolved required exposure or context
coverage withholds rendering. An unsupported exposure cannot hide an independent
source-policy uncertainty. Shared-context overflow reports unevaluated coverage rather
than truncating knowledge or returning no matches.

The six-entity pilot has overlapping model/cloud/accelerator categories and two sourced
customer links. Manufacturing is a defined category with no curated members yet; scale
is explicitly unknown. The reviewed public pilot and the separate fictional example
are documented in `configs/development/README.md`. Profile gaps remain visible; there
is no claim of comprehensive knowledge for all 120 allowed markets. No external research
or relationship discovery happens during inference.

The real baseline initially rejected direct IDs assigned to windows without a spoken
name. Both comparison modes now use prompt v4 and deterministic spoken-ID hints; the
validator remains strict. Classification also receives spoken-entity hints. Dates must
be explicit full calendar dates in source evidence; publication dates and partial dates
cannot silently establish temporal applicability. This conservative pilot rule and
window-level continuation ambiguity can increase review volume. They remain visible
limitations, not evidence that the source lacks relevant content.

Validation: **1,654 backend tests passed, 57 skipped; 194 isolated tests passed with one
legacy compatibility skip.** Thirty-three enrichment tests cover references/cycles,
multiple associations, source evidence, disabled/unknown IDs, one-hop traversal,
freshness/conflicts/dates, no-match versus uncertainty, bounded coverage, escaped
previews, pinned resume, metadata preservation and knowledge changes. The real C.1
checkpoint still loads unchanged in the existing no-Google media image with networking
disabled; the prior D checkpoint still validates. Seven opt-in live synthetic cases
passed, with all three expected supported market links retained and no unexpected
supported links. Those small labeled counts are not production precision estimates.

The reference comparison reused identical source/transcript, the corrected 120-market
snapshot, source policy and Foundry settings. The baseline proposed two stories in five
calls (62,139 reported tokens). The enriched run also proposed two stories, with changed
grouping, in ten calls (108,294 reported tokens), including one rejected classification
response followed by a successful bounded repair. It identified AI-model, cloud and
accelerator categories and supported the directly discussed markets. Nine association
candidates required review and two were unsupported; no inferred market association
was promoted to supported. Both runs returned `needs_review` and withheld all rendering,
including a cut beginning inside an ASR sentence and unresolved numerical/date/context
issues. Temporal overlap with the earlier manual reference spans was unchanged; this
is not a semantic-completeness or full-video recall result. The extra model work is a
measured pilot cost, not a throughput benchmark.

The ignored local `output/clip-media-smoke/phase-d1-validation.json` records both runs,
source/policy/model/snapshot hashes, coverage, review reasons, usage and preview paths.
`phase-d1-editorial-smoke.json` records the synthetic cases. Completed selection resumes
were checked without additional inference. Separate-story rendering carries the exact
knowledge snapshot and association metadata forward, but new reference media was not
rendered because the selection gate withheld it. Phase E still owns final-audio/media
assessment, manual approval and storage promotion.

#### Scope and first iteration

- Start with a reviewed, versioned JSON knowledge snapshot beside the allowlist. Keep
  authoritative membership and editorial enrichment independently replaceable; the
  backend API continues to decide which instruments are enabled.
- Cover a small AI-focused subset first: model developers, semiconductor designers,
  manufacturing/memory where relevant, cloud operators and infrastructure exposures.
  These are overlapping categories, not a claim that every member benefits from the
  same event. Add geography and selected macro sensitivities where supported.
- Include a few sourced, direct supplier/customer/competitor/ownership relationships
  only where they improve an actual test case. Limit traversal to one curated edge.
  Larger dependency coverage is a follow-on iteration, not a prerequisite for D.1.
- Keep separate source spans, chronological story manifests and 1.0× automatic
  playback. Do not add concatenation, external facts to spoken captions, channel
  monitoring, new rendering features, publishing or final-media approval here.

#### Contracts and knowledge curation

Define typed, versioned contracts; settle exact field names during implementation.

| Record | Required meaning and constraints |
|---|---|
| Taxonomy | Stable category IDs, definitions, inclusion/exclusion examples and optional parent IDs; distinguish sector, business role, end market, geography and macro sensitivity. Validate references and reject parent cycles. |
| Entity/profile | Stable entity identity and explicit aliases; separate links to zero or more market IDs; multiple sourced category memberships and exposure attributes. Nontradeable supporting entities are allowed in knowledge but never become enabled instruments. |
| Scale/exposure | Optional, defined qualitative bands or sourced measurements with units, scope, period and as-of date. Unknown remains null. Do not infer economic scale from a ticker price or pre-IPO status, or invent revenue weights and materiality scores. |
| Relationship | Stable ID, directed endpoints, a typed relation and scoped explanation; source references, verification date and applicable dates where known. Do not infer reciprocal or transitive economic effects from the edge alone. |
| Evidence source | Stable ID, citation URL/document reference, supporting excerpt or factual summary, retrieval/verification date and temporal applicability. Content is untrusted data; inference does not fetch arbitrary model-supplied URLs. |
| Snapshot | Schema/revision, taxonomy/profiles/relationships/source records and content hashes. Pin the exact snapshot in each run; report explicit coverage gaps and stale or conflicting records. |

Use issuer disclosures and other authoritative sources for initial manual curation.
Model-assisted drafts may help preparation but require review before entering the
accepted snapshot. Public examples use synthetic entities; no private backend details
or credentials enter tracked fixtures. Changing a market from pre-IPO to standard
does not silently change its underlying entity or duplicate its profile. Different
instruments on one issuer remain distinct market IDs.

Freshness must be explicit and configurable by record type. Stable industry labels
and changing commercial relationships need different review intervals. Preserve
both when a fact was verified and when it applied; never use a later relationship to
explain an earlier event without evidence of temporal applicability. Unknown event
dates or stale facts cannot silently support a time-sensitive inferred association.

#### Story and association output

Separate three concepts in the selection report and downstream story metadata:

1. **Source subjects / mentioned markets:** entities actually discussed, resolved from
   spoken names/aliases and context, with exact transcript evidence. Keep D's lexical
   identity check for direct market matches; passing mentions still do not qualify.
2. **Affected categories:** one or more categories tied to the discussed event or
   argument, with a concrete economic mechanism and supporting transcript spans.
   Category membership generates candidates; it does not prove significant impact.
3. **Affected allowed markets:** direct or inferred associations with typed basis,
   mechanism, materiality rationale, transcript evidence, knowledge-record IDs and
   decision (`supported`, `needs_review`, `unsupported`). Final IDs must intersect the
   current enabled universe. Do not place an inferred ID into a field claiming that
   its name was spoken.

Keep source-attributed price stance separate from inferred exposure. Each market can
have different or uncertain exposure; absence of a directional statement remains
`unclear`. Do not copy one speaker's bullish/bearish label onto an entire category.
Inferred associations are explicitly labeled editorial metadata and cannot be
presented as quotes, forecasts made by the speaker, or facts heard in the clip.

Version the new story/selection metadata and define backward-compatible loading for
D and C.1 artifacts. Existing `matched_market_ids` retains its direct-match meaning;
introduce separate typed associations rather than silently changing that field.
The renderer must preserve the metadata through the sequence manifest, without
changing source audio or adding inferred claims to captions/titles. Metadata may not
repair an incomplete spoken idea or justify cutting away a necessary qualification.

#### Selection integration and safeguards

1. Pin config, policy, allowlist and enrichment before inference. An explicit `off`
   mode reproduces D's baseline; invalid configured snapshots fail clearly rather
   than falling back silently. Missing profiles leave direct and macro matching
   available and expose a coverage gap; they do not create speculative matches.
2. Assess every source window for subjects, events, economic mechanisms and context
   dependencies before making final ticker-specific exclusions. A potentially relevant
   discussion must reach association evaluation even when no allowed ticker is named.
   Reconstruct complete discussions using D's bounded context handling.
3. Resolve subject identities, assign multiple categories, and build candidate market
   associations from explicit memberships/exposure rules and at most one curated edge.
   Do not enumerate every category member as an affected market by default. Bound
   payloads and fan-out; unresolved coverage caused by a hard budget is review-required,
   not proof of no matches. Evaluate the candidate set before output count limits.
4. Require a source-established event/argument plus a sourced, applicable exposure
   mechanism and a qualitative materiality rationale for each inferred association.
   A relationship alone, weak sector similarity or self-reported model confidence is
   insufficient. Validate IDs, evidence, allowed membership and knowledge references
   deterministically; check economic reasoning in the pre-render policy pass.
5. An inferred allowed-market association can establish relevance only when all of
   those checks pass. A category tag alone cannot. Preserve macro-only eligibility
   without manufactured ticker IDs. Preserve complete story boundaries regardless of
   which market/category eventually qualifies it.
6. Record and exclude unsupported optional associations without invalidating an
   independently eligible direct/macro story. If the story's only eligibility depends
   on an unresolved association, return `needs_review`. Material source ambiguity or
   an unresolved required sequence decision continues to withhold the render plan;
   do not silently discard a necessary story to obtain a passing sequence.
7. Rank and deduplicate complete sequences only after relevance and association checks.
   Explain accepted, rejected and unresolved associations in the local preview, with
   source evidence and knowledge citations clearly distinguished. Extra matched markets
   must not mechanically inflate usefulness or displace a better complete argument.

#### Reuse, audit handoff and implementation files

Include enrichment mode, snapshot hashes, association contracts and prompt versions
in selection/cache bindings. A changed taxonomy, relationship, profile, policy or
allowlist requires a new selection decision; an interrupted resume uses its original
pinned inputs. Reuse the source and transcript. Preserve existing manual-plan hashes
and old-run inspection through explicit version handling. Compatible media reuse is
allowed only under existing render rules; broader cross-run caching stays deferred.

Phase E must independently inspect every displayed/eligibility-bearing association
against the final story audio, original context and the same pinned knowledge. It
must also check that editorial inferences are not attributed to the speaker. A change
to association metadata invalidates sequence approval even if media bytes are unchanged.
At promotion, refresh membership and check configured knowledge freshness; relevant
changes require reevaluation and new approval, never silent mutation of an old result.

Planned file changes:

| Files | Responsibility |
|---|---|
| New `clip_pipeline/enrichment.py` and `enrichment_contracts.py` | Validated snapshot loading, reference/temporal checks, profile lookup and bounded candidate generation; separate from authoritative `markets.py`. |
| `config.py`, `contracts.py`, `selection_contracts.py` | Explicit enrichment mode/path/limits, versioned subjects/categories/associations, provenance bindings and compatibility handling. |
| `selection_prompts.py`, `selection.py` | Discussion-first classification, direct versus inferred checks, materiality reasoning, category/market validation and unchanged global sequence ranking. |
| `selection_runner.py`, `runner.py` | Immutable knowledge artifacts, hashing, durable resume and completed-checkpoint validation. |
| `report.py`, `stories.py`, `execution.py` as needed | Inspectable association explanations and preservation through separate-story sequence metadata; no new media processing. |
| New enrichment example config/snapshot under `configs/`; policy and development README | Synthetic contract examples, reviewed public pilot profiles, curation procedure, explicit coverage and policy version update. |
| New `tests/test_clip_pipeline_enrichment.py`; selection/provider compatibility tests and editorial fixtures | Deterministic validation, adversarial and temporal cases, multiple associations, bounded coverage, resume/cache invalidation and old artifact compatibility. |
| Runbook and this plan | Commands, baseline/enriched comparison, curation responsibilities, remaining limitations and E handoff. |

#### Evaluation and exit criteria

First rerun the unchanged reference sample with the corrected **120-market** catalog
and enrichment off. The earlier 118-market run omitted ANT; it cannot serve as the
sole comparison for measuring enrichment gains. Reuse the same source and transcript
and hold policy/model/settings fixed for the paired comparison where compatible;
record all differences and model-call/token usage. Do not claim missing ANT was fixed
by classification, or require a particular number of stories/ticker assignments.

Then evaluate enrichment on the same reference plus a small labeled synthetic suite:
direct Anthropic identity/ANT resolution, a supported indirect association without a
spoken ticker, multiple business roles with different exposure, category-only weak
association, nonfinancial alias collision, stale or future-dated relationships,
disabled/unknown instruments, conflicting knowledge, unsupported multi-hop links,
macro-only content, unknown stance, missing profiles and prompt injection in both
transcript and knowledge data. Positive indirect cases must retain enough context to
explain their relevance; negative cases must not manufacture affected markets.

Exit requires:

- All deterministic contract, evidence, enabled-membership, temporal, fan-out and
  compatibility tests pass; changed enrichment invalidates selection while source/ASR
  stays reusable. Resume does not mix snapshot versions or repeat verified calls.
- Reviewed examples demonstrate both multiple valid associations and rejection of
  plausible but unsupported links. Every emitted association is inspectable through
  transcript evidence plus applicable knowledge references; no invented IDs or
  speaker-attributed inferences occur in the acceptance set.
- Paired results report eligible-discussion coverage, association precision against
  manual labels, unsupported-link count, review reasons/rate, boundary/context quality
  and inference usage. Report small-sample counts, not population accuracy claims.
- The reference preserves complete story progression and exposes ANT as an available
  direct match where identity/evidence support it. Existing ASR, boundary, number or
  date problems remain explicit review items; enrichment is not evidence of content
  approval. No media enters approved storage before E.

Planning estimate: 3–5 engineering days for contracts, a small curated category set,
matching, provenance and focused evaluation. Broader curated dependencies may add
3–5 engineering days plus source research; defer them until measured errors justify
the work. Research/refresh effort depends on profile breadth. These estimates are not
a delivery guarantee; complete profile coverage of all 120 markets is not a D.1 exit requirement.

### E. Final audit, review, and storage

1. Add media checks and final-audio ASR per story, independent story/sequence semantic audits, omitted-context checks and typed decision results. Audit D.1's direct/inferred associations, knowledge applicability and attribution against the final story and pinned source/knowledge evidence.
2. Produce independent-story and sequential previews with hash-bound story and sequence reviewer decisions.
3. Add Blob storage and approved ordered sequence manifests, final commit manifests and approval-time market refresh. A removed/changed story triggers sequence re-audit before promotion.

Exit: no unreviewed/rejected/failed story or sequence enters the approved index. Changing final bytes, order, membership or displayed metadata invalidates the corresponding approval. Upload interruption/retry cannot publish an incomplete sequence. Audit failure cannot become `completed_no_matches`.

### F. One Azure worker and sample iteration

1. Build the dedicated pinned CPU image, provision the minimal Azure footprint, and verify identity access from inside the container.
2. Process the selected source on Azure; record actual resource, token, elapsed-time, and disk measurements.
3. Review every candidate and refine policy, selection boundaries, captions, and layout one concern at a time.
4. Use stage reruns to avoid repeated downloads/transcription and preserve before/after reports.

Exit: the user accepts the actual sample clips; all technical gates pass; repeat/restart behavior is demonstrated; known limitations and effective configuration are recorded. Do not claim general channel-level precision or throughput from this single-video result.

## 12. Verification plan

### Lightweight tests (no GPU or external network)

Add focused tests such as:

- `tests/test_clip_pipeline_config.py`: invalid combinations, fixed aspect, nonsecret snapshots, explicit snapshot mode.
- `tests/test_foundry_backend.py`: identity/key modes, schema contracts, token renewal seam, retry classification, truncation/refusal, model-specific parameters, unknown cost.
- `tests/test_market_snapshots.py`: duplicates, aliases/schema validation, ETag, freshness, API failure, changed membership before promotion.
- `tests/test_topic_selection.py`: substantive versus incidental mentions, evidence verification, macro without ticker, generic politics, opinions/advice, full window coverage, zero output, no first-window fallback, snapping and dedupe.
- `tests/test_clip_pipeline_artifacts.py`: cache keys, same-name different-source invalidation, interrupted writes, policy/prompt/model changes, resume versus rerun.
- `tests/test_clip_audit.py`: altered numbers, lost qualifiers/context, ASR disagreement, stale-event presentation, hard-fail/nonbypassable review.
- `tests/test_clip_pipeline_stories.py`: contiguous separate outputs, chronology, duplicate/missing stories, source-to-output timing at 1.0×/1.1×, natural boundary fixtures, per-story resume and rate/config invalidation.
- Selection/audit sequence cases: an engaging claim with a quiet caveat, essential context in an earlier input window, a viewer stopping before a later qualification, opposing speakers, company criticism without a price view, unrelated discussion separation, and removing a rejected middle story. Assert explicit review/rejection where meaning cannot be preserved.
- `tests/test_clip_pipeline_storage.py`: only approved hashes committed, upload failure, retry/idempotency, no partial manifest publication.
- `tests/test_clip_only_imports.py`: baseline path imports without Google/full app; disabled features stay disabled with sentinel keys present.

Use synthetic transcripts and short generated/licensed media fixtures, not third-party transcripts or real service responses committed into a public repository.

### Media/container integration

Run in the actual clip-only image: source ingest, source/final ASR, font/caption rendering, exact geometry, audio/decoding checks, output-path correctness, and model resource release. Exercise source with speech, no audio, no matches, and a failed clip. Verify report previews and full pilot clips visually. Do not let minimal-CI `importorskip` results substitute for this check.

Existing regression coverage includes `test_llm_backend.py`, `test_clip_selection.py`, `test_clip_floor.py`, `test_transcript_checkpoint.py`, `test_clip_render_order.py`, `test_clip_ready_marker.py`, `test_phantom_clips.py`, and relevant caption/layout tests when shared code changes. Run the repository's required full checks before any later push; a GPU render equivalence run is required if GPU rendering is changed, which is not planned for the CPU baseline.

### Azure smoke/recovery

Verify Foundry inference, market API connectivity, ACR pull, Blob access, and source acquisition independently before the full sample. Kill the worker after transcription and during artifact upload, then confirm compatible checkpoint reuse and no premature approval. Confirm repeat promotion does not create duplicate approved records.

### Sample evaluation record

For the one video, manually mark relevant intervals, prohibited/irrelevant intervals, key numerical statements, context requirements, and acceptable framing. Compare candidate precision, missed labeled intervals, context loss, material caption errors, duplicate moments, and reviewer acceptance. Small-sample results guide iteration only. Add a broader holdout set before scheduled multi-channel operation.

## 13. Inputs needed later, not blockers for this plan

| Input | Needed by | Until then |
|---|---|---|
| One sample YouTube URL or authorized local media | First real ingest/render evaluation | Develop against synthetic/appropriate fixtures. |
| Backend market API route, auth, schema agreement | Live market integration | Use an explicitly supplied dated snapshot; never fabricate the real allowlist. |
| Azure subscription/resource group/region, administrative access, approved resource choices | Deployment | Parameterized infrastructure and local image validation. |
| Foundry endpoint, deployment name(s), access/quota | Real provider smoke test | Mock transport/schema/error tests. |
| Sample language and preferred caption/layout appearance | Sample quality iteration | Proposed defaults above; record overrides when the sample is selected. |
| Source acquisition/reuse permission context | Real third-party media processing | Keep status explicit; no blanket ownership assertion. |

No further product clarification is required to start implementing the isolated package and contracts. Credentials, the sample, and live endpoint details are execution prerequisites rather than reasons to delay the plan.

## 14. Later expansion seams

- A channel watcher emits the same source job payload now supplied manually.
- Service Bus replaces manual dispatch; worker leases/idempotency extend the existing run/commit contract. Do not reuse the legacy API's in-memory queue across replicas.
- PostgreSQL indexes channels, jobs, market snapshots, review decisions, and accepted clip metadata when needed.
- A captions-first prefilter can avoid full downloads/ASR for irrelevant videos, while preserving exact word-timing validation for accepted sources.
- Foundry vision implementations fill the currently disabled layout/subtitle/hook stages, with representative visual evaluations before enabling them.
- Cross-source semantic/event deduplication and source diversity policies operate before final publication/storage selection.
- GPU/parallel worker choices follow measured source-hours, candidate counts, render time, ASR time, VRAM, scratch usage, and accepted-output yield. Output targets never relax rule enforcement.

## 15. External implementation references

These references were checked while preparing the plan; confirm deployment-specific availability and SDK versions during implementation.

- [Microsoft Foundry / Azure OpenAI v1 API](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle): v1 inference endpoints, deployment configuration, identity-based access.
- [Structured outputs](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs): supported typed response contracts and Python identity examples.
- [Foundry GPT-4.1-mini catalog](https://ai.azure.com/catalog/models/gpt-4.1-mini): initial evaluation candidate, not a fixed long-term model commitment.
- [Authorize Blob access with Microsoft Entra ID](https://learn.microsoft.com/en-us/azure/storage/blobs/authorize-access-azure-active-directory): managed identity and data-plane RBAC.
- [Managed identity authentication for ACR](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-authentication-managed-identity): image pulls from an Azure VM.
- [YouTube captions download](https://developers.google.com/youtube/data/reference/rest/v3/captions/download): official caption retrieval requires video-edit permission; it is not a general third-party transcript API.

This document is the only deliverable of the planning task. Implementation, deployment, provider calls on real media, and infrastructure provisioning are subsequent work.
