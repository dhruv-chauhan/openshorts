#!/usr/bin/env bash
# Invoke through Azure Run Command. No inbound port, daemon, or publishing action.
set -euo pipefail
registry=$1
image=$2
account=$3
job_id=$4
job_blob=$5
[[ "$image" =~ ^[a-z0-9.-]+/clip-worker@sha256:[a-f0-9]{64}$ ]]
[[ "$registry" =~ ^[a-z0-9]{5,50}$ && "$account" =~ ^[a-z0-9]{3,24}$ ]]
[[ "$job_id" =~ ^[A-Za-z0-9_-]{1,80}$ && "$job_blob" == bootstrap/* && "$job_blob" != *..* ]]
cloud-init status --wait >/dev/null
mountpoint -q /srv/clip
exec 9>/srv/clip/.host-job.lock
flock -n 9
az login --identity --allow-no-subscriptions --output none
az acr login --name "$registry" --output none
docker pull "$image" >/dev/null
mkdir -p /srv/clip/requests /srv/clip/probes
az storage blob download --auth-mode login --account-name "$account" --container-name clip-runs --name bootstrap/config.json --file /srv/clip/config.json --overwrite --no-progress --output none
az storage blob download --auth-mode login --account-name "$account" --container-name clip-runs --name "$job_blob" --file "/srv/clip/requests/$job_id.json" --overwrite --no-progress --output none
probe_key=$( { printf '%s' "$image"; cat /srv/clip/config.json; } | sha256sum | cut -d' ' -f1)
if ! python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); sys.exit(0 if p.get("foundry",{}).get("ok") and p.get("blob_upload_download_verified") else 1)' "/srv/clip/probes/$probe_key.json" 2>/dev/null; then
  docker run --rm --cpus 4 --memory 12g --pids-limit 512 --security-opt no-new-privileges:true -v /srv/clip:/data --entrypoint python "$image" -m clip_pipeline.worker_probe --config /data/config.json --output "/data/probes/$probe_key.json"
fi
set +e
docker run --rm --name "clip-$job_id" --cpus 4 --memory 12g --pids-limit 512 --security-opt no-new-privileges:true -v /srv/clip:/data -e HF_HOME=/data/models/huggingface -e TORCH_HOME=/data/models/torch --entrypoint python "$image" -m clip_pipeline.worker_job --job "/data/requests/$job_id.json" --root /data/jobs
code=$?
set -e
for file in result.json stdout.log stderr.log; do
  if [[ -f "/srv/clip/jobs/$job_id/$file" ]]; then
    az storage blob upload --auth-mode login --account-name "$account" --container-name clip-runs --name "jobs/$job_id/$file" --file "/srv/clip/jobs/$job_id/$file" --overwrite --no-progress --output none
  fi
done
az storage blob upload --auth-mode login --account-name "$account" --container-name clip-runs --name "probes/$probe_key.json" --file "/srv/clip/probes/$probe_key.json" --overwrite --no-progress --output none
exit "$code"
