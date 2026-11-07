# One clip worker (Phase F)

This pilot runs explicit jobs on one Linux amd64 CPU VM. It does not schedule channel
polling or automatically review/publish content. Keep deployment parameters, configuration,
source media, provider responses, receipts and job results in ignored `output/` paths.

## Provision and build

`main.bicep` creates a 4-vCPU/16-GiB VM, persistent 128-GiB data disk, Basic ACR,
private Blob containers and scoped managed-identity assignments. It reuses an existing
Foundry account and inference role. Check VM SKU availability and quota in the selected
subscription/region before deployment. The VM has explicit outbound connectivity and
all inbound traffic denied; administer it through Azure Run Command.

```bash
az deployment group what-if --resource-group '<group>' \
  --template-file infra/clip-worker/main.bicep --parameters @'<private-parameters.json>'
az deployment group create --resource-group '<group>' \
  --template-file infra/clip-worker/main.bicep --parameters @'<private-parameters.json>'
docker buildx build --platform linux/amd64 --file Dockerfile.clip \
  --target runtime --load --tag '<registry>/clip-worker:<version>' .
az acr login --name '<registry-name>'
docker push '<registry>/clip-worker:<version>'
```

Resolve the pushed digest and operate with `registry/clip-worker@sha256:...`.
Base images are digest-pinned; Python requirements pin the direct dependencies and
`/opt/worker-dependencies.txt` records the resolved environment. OS packages and
transitive dependencies are captured by the resulting image digest, not a fully
hermetic rebuild. `vmImageVersion` can pin the Ubuntu image after resolving availability.

Cloud-init mounts an existing ext4 data volume without formatting it. It formats only
a blank attached data disk. The data disk is retained on VM deletion. Backups provide
recovery when the disk itself is unavailable; they are not a substitute for retaining
receipts and the effective configuration.

Roles: ACR `AcrPull` on the registry, Blob Data Contributor on each of the two
containers, and both the supplied gateway inference role and Cognitive Services OpenAI User on
the existing Foundry account only. Both inference assignments were explicitly approved
for this worker. This is the validated role/audience combination, not proof that
each assignment is individually necessary on every Foundry endpoint.
No account keys or Foundry API keys are needed. Blob privacy uses container properties,
which work with data-contributor permissions; reading stored access policies would
require a different permission. Storage endpoints are network-public with Entra
access control; anonymous Blob access and shared keys are disabled. Private endpoints
are outside this pilot.

## Run one job

Put the effective config at `clip-runs/bootstrap/config.json`. Use workspace
`/data/workspace`, managed identity, packaged policy paths, and persistent model caches.
The pilot worker passed Foundry inference with
`foundry.identity_scope=https://cognitiveservices.azure.com/.default`; its
`https://ai.azure.com/.default` token returned PermissionDenied despite the approved
roles. The developer identity passed the latter audience. Verify the actual worker
endpoint/audience combination; never silently fall back to API keys.
Upload one JSON job to `clip-runs/bootstrap/<job>.json`:

```json
{
  "job_id": "source-001",
  "args": ["source", "--config", "/data/config.json", "--url", "https://www.youtube.com/watch?v=VIDEO_ID"],
  "backup": true,
  "include_source": true
}
```

An operator needs Blob data access to the run container to upload inputs/read results.
Use Entra credentials, not SAS strings in job JSON. The operator is trusted: CLI path
arguments refer to the mounted worker filesystem. This is not a public job-submission API.

```bash
python infra/clip-worker/operate.py --group '<group>' --vm '<vm>' \
  --registry '<registry-name>' --image '<registry>/clip-worker@sha256:<digest>' \
  --account '<storage-account>' --job-id source-001 \
  --job-blob bootstrap/source-001.json --output '<private-command-result.json>'
```

The host serializes jobs with `flock`, verifies the mounted data disk, pulls using its
identity, and probes Foundry, mock market connectivity, private Blob access and immutable
upload/retry from inside the actual container. Probes are cached by image/config hash.
The container has a 4-CPU/12-GiB limit. Run Command's transport success does not establish
job success: `operate.py` parses the explicit shell exit marker.

Results and private stdout/stderr are uploaded to `clip-runs/jobs/<job-id>/`. Inspect
`result.json`, including `backup_error` and `monitor_error`, even when the CLI exited
successfully. CLI exit `3` means content needs review, not an infrastructure crash;
read the recorded outcome and report before retrying. Repeating an identical completed job ID returns its recorded result;
a different request requires a new ID. To retry a failed stage, use a new job whose
arguments are `resume` with the original run ID and appropriate execution mode.
Never change a completed job's input under its existing ID.

Source, select, render-stories, audit and compatible resume operations are allowed.
Human review and promotion are deliberately not worker-job commands. A transcript
checkpoint can be reused by a fresh selection run; rendering consumes the selected
plan and source transcript. Keep prior run IDs and reports when changing policy/models.

The reference download hit YouTube’s bot challenge on this Azure IP. Direct cloud
YouTube acquisition is therefore not established for production; resolve the permitted
source-acquisition path before channel automation. For this pilot, import an already acquired source using
`seed-source --config ... --source-artifact ... --media ...` in the same container,
then retry `source --url ...` as a new job. Import verifies exact media bytes, duration,
limits and YouTube provenance. It preserves permission status and publication metadata;
it does not declare ownership or convert the input to an anonymous local source.

## Measurements and recovery

`monitor.bicep` creates a Log Analytics workspace, direct-ingestion DCR and a scoped
Monitoring Metrics Publisher assignment. Optional job `monitor` contains its `endpoint`
and `rule_id`. Only job ID, command, exit status, elapsed time, CPU time, maximum child
RSS and free disk space go to Monitor. Text/transcripts stay in private run artifacts.
Private results also report source/output duration and cumulative reported model tokens.
RSS is the child-process peak, not aggregate VM memory; token totals include all recorded
attempts and must not be summed across resumes of the same run as independent usage.

`backup` verifies remote bytes and writes its checkpoint marker last. Backups contain
private configuration and evidence, never an approved discovery manifest. Source media
is included only when requested. Restore requires the original canonical workspace,
refuses overwrites and checks all hashes before making the run visible:

```bash
python -m clip_pipeline backup --workspace /data/workspace --run-id '<run>' \
  --receipt /data/checkpoint-receipt.json --include-source
python -m clip_pipeline restore --config /data/config.json \
  --receipt /data/checkpoint-receipt.json
```

For a backup omitting the original media, add `--source-media '<exact-source-file>'`.
To simulate disk loss safely, first stop processing and move the run directory aside;
do not delete the only copy. Restore it and compare the manifest hash, then use the
appropriate `resume` mode. Do not modify pinned absolute paths to relocate a checkpoint.

Deallocate the VM when idle (`az vm deallocate --resource-group '<group>' --name '<vm>'`).
ACR, retained disks, storage, monitoring and the static IP continue to incur charges.
Keep those resources for the next sample iteration or explicitly remove them later.


Microsoft references: [container-property privacy and RBAC](https://learn.microsoft.com/en-us/rest/api/storageservices/get-container-properties),
[keyless Foundry authentication](https://learn.microsoft.com/azure/ai-foundry/foundry-models/how-to/configure-entra-id),
[managed-identity ACR pulls](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-authentication-managed-identity),
and [Monitor ingestion](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/logs-ingestion-api-overview).
