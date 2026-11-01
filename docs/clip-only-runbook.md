# Clip-only pipeline: Phases A–B

Phase A provides configuration and artifact contracts, local preparation, state validation, checksums, and a single-worker lock. Phase B adds the Foundry text adapter, a live capability preflight, and the enforced media environment profile. `run` and `resume` still only prepare inputs: downloading, transcription, topic selection, rendering, auditing, approval, and Blob upload arrive in later phases of the [implementation plan](architecture/clip-only-azure-implementation-plan.md).

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

Legacy Gemini users retain their existing routing and response shapes. Neutral schemas and the permissive legacy parser now live in `llm_schemas.py` and `llm_json.py`, with compatibility re-exports in `gemini_worker.py`. Foundry uses its own strict parser. The media entry point and subprocess execution are connected in Phase C; this phase does not claim a rendered-video smoke test or a complete media image.

## Validate configuration

```bash
python -m clip_pipeline validate --config configs/clip-only.example.json
```

The example uses placeholder service endpoints. Validation checks configuration structure and the local policy, but does not assert that remote services exist or are accessible. Output explicitly contains `remote_services_checked: false`.

Relative workspace, policy, and market snapshot paths resolve against the **config file's directory**, not the shell's current directory. Relative `--input`, `--transcript`, and `--fixture` paths resolve against the shell's current directory.

Unknown fields are errors. Fixed vertical geometry, a zero clip minimum, one worker, and manual review are enforced. Secrets may only be referenced by environment-variable name; do not put key values or credential-bearing URLs into JSON. In `api_key` auth mode, supply `api_key_env`; identity modes reject that field. Production endpoint details and user media/transcripts should stay outside committed examples.

The API and development snapshot modes are explicit and mutually exclusive. There is no automatic fallback. API mode is **not fetched** during preparation. Snapshot mode validates the provided local list. `blob: null` supports local preparation; a real storage target will be required before live promotion is implemented.

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

These commands verify every saved artifact's size/hash and the run fingerprint. Resume takes the workspace lock and uses pinned snapshots; it does not reread changed original configuration, policy, market, or fixture files. It performs no downstream execution and does not rewrite the run. There is no `rerun`, `review`, or `promote` command yet; submit a new preparation to capture changed inputs.

## Files and reproducibility

Each prepared run contains:

- `manifest.json`: run identity, state, outcome, checksummed artifact references and input fingerprint.
- `resolved-config.json`: expanded defaults and resolved local paths, with secret names only.
- `policy.json` and `submission.json`: pinned policy and normalized source request.
- `market-snapshot.json`: only in explicit snapshot mode.
- `fixture.json`: only for a synthetic fixture run.
- `supplied-transcript.json`: only for an explicitly provided transcript artifact.

Identical resolved inputs in the same workspace produce the same fingerprint but distinct run IDs and timestamps. Fingerprints are not a promise of identical future LLM output. Source media cache keys, source-to-transcript binding, and selective stage reruns are not implemented yet.

Input artifacts cannot be overwritten through the store. Files are written via temporary file, flush/fsync and rename; the manifest is committed last. An interrupted preparation may leave an uncommitted run directory, which is not a resumable run. Do not treat directory existence as completion. The OS releases the lock when the worker exits or is killed; never remove an active `.worker.lock` file to bypass contention.

The contracts also define lifecycle stage/outcome rules, source/transcript/candidate/final-media boundaries, and hash-bound audit results for later phases. Contract construction is not a substitute for the actual media checks, semantic policy checks, and approval gates.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Validation, preparation, inspection, or live preflight succeeded. |
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
  tests/test_clip_pipeline_foundry.py tests/test_clip_pipeline_isolation.py -q
```

The isolation test starts a fresh interpreter that blocks media/cloud/app imports and network connection/DNS operations. Other tests cover invalid configuration, source/evidence/timestamp contracts, pinned resume inputs, corruption, atomic write failure, lock contention and process death, and distinguishing preparation from completed output.

Phase B tests exercise the real OpenAI SDK through a mocked HTTP transport: strict schemas, repairs, refusals, truncation, HTTP/network errors, bounded retries, budgets, batching, usage, auth configuration, and preflight deduplication. The separate CI job installs no Google SDK and tests fresh-process media imports with local heavyweight detectors stubbed to avoid weight downloads. This proves the provider import/guard boundary, not actual ML rendering. Live Azure preflight is an explicit operator command, never an automatic CI test.
