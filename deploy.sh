#!/usr/bin/env bash
# Deploy a Cloud Function from this repo.
#
#   ./deploy.sh <function>              deploy (or redeploy)
#   ./deploy.sh <function> --schedule   also create/update its Cloud Scheduler job
#
# Project, region and service account come from config.yaml. The function folder, lib/
# and config.yaml are staged into a temporary directory and deployed from there, so the
# git-ignored config.yaml is uploaded without being committed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FN="${1:-}"
[[ -f "${ROOT}/config.yaml" ]] || { echo "config.yaml missing: cp config.example.yaml config.yaml" >&2; exit 1; }

cfg() {  # cfg gcp.region → value from config.yaml
  python3 - "$1" "${ROOT}/config.yaml" <<'PY'
import sys, yaml
v = yaml.safe_load(open(sys.argv[2]))
for k in sys.argv[1].split("."):
    v = (v or {}).get(k)
print("" if v is None else v)
PY
}

PROJECT_ID="$(cfg gcp.project_id)"
REGION="$(cfg gcp.region)"
SA="$(cfg gcp.service_account)"
SCHEDULE_AUTH="none"   # none | oidc | run-secret
SCHEDULE_PATH=""

# ── per-function settings ──────────────────────────────────────────────────────────────
case "${FN}" in
  spend-import)
    # Public, because Roku must reach /oauth/callback; /run is gated by X-Run-Secret.
    NAME="roku-ads-tw-import"; ENTRY="spend_import"; LIBS=(runtime tw_data_in)
    ACCESS="--allow-unauthenticated"; SCHEDULE_AUTH="run-secret"; SCHEDULE_PATH="/run"
    FLAGS=(--memory=512Mi --timeout=900s --max-instances=1)
    NEXT='Connect once:  open https://'"${REGION}-${PROJECT_ID}"'.cloudfunctions.net/roku-ads-tw-import/oauth/start
Dry run:       curl -H "X-Run-Secret: $RUN_SECRET" "{URL}/run?days=7&dry_run=1"' ;;
  conversion-proxy)
    NAME="roku-ads-conversion-proxy"; ENTRY="track"; LIBS=(runtime pixel_proxy)
    ACCESS="--allow-unauthenticated"
    FLAGS=(--memory=256Mi --timeout=30s --max-instances=10)
    NEXT='Set CF_ENDPOINT in shopify-pixel/pixel.js to {URL}/track' ;;
  *) echo "usage: $0 spend-import|conversion-proxy [--schedule]" >&2; exit 1 ;;
esac
# ───────────────────────────────────────────────────────────────────────────────────────

BUILD="$(mktemp -d)"; trap 'rm -rf "${BUILD}"' EXIT
cp "${ROOT}/${FN}"/*.py "${ROOT}/${FN}/requirements.txt" "${ROOT}/config.yaml" "${BUILD}/"
for m in "${LIBS[@]}"; do cp "${ROOT}/lib/${m}.py" "${BUILD}/"; done

echo "── Deploying ${NAME} (${PROJECT_ID} / ${REGION}) ──"
gcloud functions deploy "${NAME}" --project="${PROJECT_ID}" --region="${REGION}" \
  --gen2 --runtime=python312 --entry-point="${ENTRY}" --source="${BUILD}" \
  --trigger-http "${ACCESS}" --service-account="${SA}" --min-instances=0 "${FLAGS[@]}"

URL=$(gcloud functions describe "${NAME}" --project="${PROJECT_ID}" --region="${REGION}" \
  --gen2 --format="value(serviceConfig.uri)")
echo "Deployed: ${URL}"

if [[ "${SCHEDULE_AUTH}" == "oidc" ]]; then
  # The scheduler's OIDC identity needs run.invoker on the underlying Cloud Run service,
  # otherwise every call fails with "request was not authenticated".
  gcloud run services add-iam-policy-binding "${NAME}" --project="${PROJECT_ID}" --region="${REGION}" \
    --member="serviceAccount:${SA}" --role="roles/run.invoker" >/dev/null
fi

if [[ "${2:-}" == "--schedule" ]]; then
  [[ "${SCHEDULE_AUTH}" != "none" ]] || { echo "${FN} is not a scheduled function" >&2; exit 1; }
  JOB="${NAME}-schedule"
  AUTH_FLAGS=(--oidc-service-account-email="${SA}" --oidc-token-audience="${URL}")
  if [[ "${SCHEDULE_AUTH}" == "run-secret" ]]; then
    RUN_SECRET="$(gcloud secrets versions access latest --project="${PROJECT_ID}" --secret="$(cfg gcp.secrets.run_secret)")"
    AUTH_FLAGS=(--headers="X-Run-Secret=${RUN_SECRET}")
  fi
  VERB=create
  gcloud scheduler jobs describe "${JOB}" --project="${PROJECT_ID}" --location="${REGION}" &>/dev/null && VERB=update
  [[ "${VERB}" == "update" && "${SCHEDULE_AUTH}" == "run-secret" ]] && AUTH_FLAGS=(--update-headers="X-Run-Secret=${RUN_SECRET}")
  gcloud scheduler jobs "${VERB}" http "${JOB}" --project="${PROJECT_ID}" --location="${REGION}" \
    --schedule="$(cfg schedule.cron)" --time-zone="$(cfg schedule.time_zone)" \
    --uri="${URL}${SCHEDULE_PATH}" --http-method=POST --attempt-deadline=30m "${AUTH_FLAGS[@]}"
  echo "Scheduled ${JOB}: $(cfg schedule.cron) $(cfg schedule.time_zone)"
fi

[[ -n "${NEXT:-}" ]] && echo -e "\n${NEXT//\{URL\}/${URL}}"
exit 0
