#!/usr/bin/env bash
# Deploy the responder as a Pub/Sub-driven Cloud Run service.
#
# Usage:
#   DRY_RUN=1 ./deploy/deploy-cloud-run.sh   # shake out, never delivers
#   DRY_RUN=0 ./deploy/deploy-cloud-run.sh   # live
#
# Fill these in, or export them before running.
set -euo pipefail

PROJECT="${PROJECT:-your-gcp-project}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-email-responder}"
RUNTIME_SA="${RUNTIME_SA:-email-responder@${PROJECT}.iam.gserviceaccount.com}"
PUSH_SA="${PUSH_SA:-pubsub-push@${PROJECT}.iam.gserviceaccount.com}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Whether the caller named DRY_RUN at all. The default below is the safe one, but a
# deploy that silently applies it to an already-live service takes the agent off the
# air without saying so. Use the colon form to match the `${DRY_RUN:-1}` default:
# `${DRY_RUN+x}` tests only whether the name is set, so `DRY_RUN=""` — a wrapper or
# CI job forwarding an upstream variable that is itself unset — would count as
# "named", skip the guard, and then collapse to the unsafe default anyway.
DRY_RUN_EXPLICIT=0
[[ -n "${DRY_RUN:-}" ]] && DRY_RUN_EXPLICIT=1
DRY_RUN="${DRY_RUN:-1}"

# One pass (inbox scan + per-message LLM calls) can run for many minutes inside the
# Pub/Sub push request. The request timeout has to exceed that or Cloud Run kills the
# pass mid-flight. Pub/Sub caps its own push ack deadline at 600s and redelivers
# after that, so leave more than one instance free to answer the redelivery -- at
# --max-instances=1 it comes back "no available instance" and the notification is
# dropped. Make redelivery idempotent (a lease, or persisted message ids).
TIMEOUT="${TIMEOUT:-30m}"
MAX_INSTANCES="${MAX_INSTANCES:-3}"

assert_no_silent_dry_run() {
  # A live service carries DRY_RUN=0. Re-deploying without naming DRY_RUN would push
  # the default 1 over it and silently stop every send, while the logs keep happily
  # reporting that each pass completed.
  [[ "$DRY_RUN_EXPLICIT" == "1" || "$DRY_RUN" == "0" ]] && return 0
  local current
  current="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" \
    --format='value(spec.template.spec.containers[0].env.filter("name:DRY_RUN").extract(value))' 2>/dev/null || true)"
  current="$(printf '%s' "$current" | tr -d "[]' ")"
  [[ "$current" == "0" ]] || return 0
  echo "✗ $SERVICE is live (DRY_RUN=0) and this deploy would set it back to 1." >&2
  echo "  Pass DRY_RUN=0 to keep it live, or DRY_RUN=1 to take it off the air on purpose." >&2
  exit 1
}

echo "▶ deploy SERVICE=$SERVICE dry_run=$DRY_RUN timeout=$TIMEOUT max_instances=$MAX_INSTANCES"
assert_no_silent_dry_run

gcloud run deploy "$SERVICE" \
  --project="$PROJECT" --region="$REGION" --source="$REPO" \
  --service-account="$RUNTIME_SA" \
  --timeout="$TIMEOUT" --concurrency=1 --max-instances="$MAX_INSTANCES" \
  --no-allow-unauthenticated \
  --set-env-vars="DRY_RUN=${DRY_RUN}" \
  --set-secrets="MATON_API_KEY=MATON_API_KEY:latest,SLACK_BOT_TOKEN=SLACK_BOT_TOKEN:latest,LINEAR_API_KEY=LINEAR_API_KEY:latest"

gcloud run services add-iam-policy-binding "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --member="serviceAccount:$PUSH_SA" --role=roles/run.invoker >/dev/null

echo "✓ deployed $SERVICE (dry_run=$DRY_RUN)"
