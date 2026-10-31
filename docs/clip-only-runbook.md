# Clip-only pipeline: Phase A

Phase A provides configuration and artifact contracts, local preparation, state validation, checksums, and a single-worker lock. It does **not** download media, transcribe, call Foundry/the market API, render, audit, approve, or upload anything. Azure resource provisioning and the live provider/media stages come later in the [implementation plan](architecture/clip-only-azure-implementation-plan.md).

## Setup

Python 3.11+ on Linux or macOS is supported. The local lock uses POSIX `flock`; all workers on a host must use the same workspace. This is not a distributed lease.

From the repository root, install the small controller dependency set into your chosen environment:

```bash
python -m pip install -r requirements-clip-core.txt
```

No full `requirements.txt`, Docker, media libraries, Azure SDK, or credentials are needed for Phase A. The package never reads `.env` or secret environment-variable values during preparation.

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
| 0 | Validation, preparation or inspection succeeded; future resolved successful outcomes also use 0. |
| 2 | Invalid command/configuration/input or corrupt checkpoint. |
| 3 | Reserved for a future `needs_review` outcome. |
| 4 | Local I/O/operational failure; future failed/partial-failure outcomes. |
| 5 | Another process owns the workspace lock. |
| 6 | Requested execution is not implemented in this phase. |
| 130 | Interrupted by the operator. |

Successful command output is JSON on stdout. Runtime validation errors use JSON on stderr and omit input values/validator contexts to avoid echoing accidentally pasted secrets. Argument-parser usage errors use the standard argparse format.

## Tests

```bash
python -m pytest tests/test_clip_pipeline_config.py \
  tests/test_clip_pipeline_artifacts.py tests/test_clip_pipeline_cli.py -q
```

The isolation test starts a fresh interpreter that blocks media/cloud/app imports and network connection/DNS operations. Other tests cover invalid configuration, source/evidence/timestamp contracts, pinned resume inputs, corruption, atomic write failure, lock contention and process death, and distinguishing preparation from completed output.
