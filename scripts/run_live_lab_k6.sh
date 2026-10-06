#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

: "${BASE_URL:?BASE_URL is required; use this session ALB HTTPS endpoint}"
MODE="${MODE:-readiness}"
apply_vus="${APPLY_VUS:-10}"
SESSION_ID="${SESSION_ID:-$(terraform -chdir=platform/live-lab/terraform output -raw session_id 2>/dev/null || true)}"
RUN_ID="${RUN_ID:-k6-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
SUMMARY_PATH="platform/live-lab/evidence/k6-${RUN_ID}.json"
LEDGER_PATH="platform/live-lab/evidence/request-ledger-${SESSION_ID}.json"
K6_BIN="${K6_BIN:-k6}"

if [[ ! "$BASE_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]]; then
  echo "BLOCKED: BASE_URL must be a single HTTPS hostname without path, query, or fragment." >&2
  exit 2
fi
if [[ ! "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ ]]; then
  echo "BLOCKED: a valid Terraform session_id is required." >&2
  exit 2
fi
allow_self_signed="${ALLOW_SELF_SIGNED_TLS:-false}"
if [[ "$allow_self_signed" != "true" && "$allow_self_signed" != "false" ]]; then
  echo "BLOCKED: ALLOW_SELF_SIGNED_TLS must be true or false; request budget was not reserved." >&2
  exit 2
fi
if ! command -v "$K6_BIN" >/dev/null; then
  echo "BLOCKED: k6 is required; request budget was not reserved." >&2
  exit 2
fi
if [[ "$MODE" == "canary-apply" || "$MODE" == "apply" ]]; then
  : "${TEST_PASSWORD:?TEST_PASSWORD must be set in the local process environment; do not pass it as a command-line argument}"
fi

if [[ "$MODE" == "soak" ]]; then
  planned=102000
elif [[ "$MODE" == "synchronized-refresh" ]]; then
  planned=10000
elif [[ "$MODE" == "canary-apply" ]]; then
  planned=15600
elif [[ "$MODE" == "apply" ]]; then
  [[ "$apply_vus" =~ ^[0-9]+$ ]] || {
    echo "BLOCKED: APPLY_VUS must be an integer from 1 to 200; request budget was not reserved." >&2
    exit 2
  }
  (( apply_vus >= 1 && apply_vus <= 200 )) || {
    echo "BLOCKED: APPLY_VUS must be an integer from 1 to 200; request budget was not reserved." >&2
    exit 2
  }
  planned=$((apply_vus * 4))
elif [[ "$MODE" == "readiness" || "$MODE" == "health" ]]; then
  planned=150
else
  echo "BLOCKED: unsupported live-lab k6 mode: $MODE" >&2
  exit 2
fi

mkdir -p platform/live-lab/evidence
chmod 700 platform/live-lab/evidence
planned_from_code="$(python3 scripts/loadtest_ledger.py plan --mode "$MODE" --apply-vus "$apply_vus")"
if [[ "$planned_from_code" != "$planned" ]]; then
  echo "BLOCKED: wrapper plan ($planned) and ledger plan ($planned_from_code) disagree." >&2
  exit 2
fi
python3 scripts/loadtest_ledger.py reserve \
  --ledger "$LEDGER_PATH" \
  --session-id "$SESSION_ID" \
  --run-id "$RUN_ID" \
  --mode "$MODE" \
  --planned "$planned"

set +e
env -i \
  "PATH=$PATH" \
  "BASE_URL=$BASE_URL" \
  "MODE=$MODE" \
  "RUN_ID=$RUN_ID" \
  "SUMMARY_FILE=$SUMMARY_PATH" \
  "MAX_PLANNED_REQUESTS=200000" \
  "READ_RATE=20" \
  "READ_DURATION=60m" \
  "BURST_RATE=100" \
  "BURST_DURATION=5m" \
  "APPLY_RATE=5" \
  "APPLY_DURATION=13m" \
  "APPLY_VUS=$apply_vus" \
  "REFRESH_VUS=10000" \
  "ALLOW_SELF_SIGNED_TLS=$allow_self_signed" \
  "TEST_PASSWORD=${TEST_PASSWORD:-}" \
  "TEST_USERNAME_PREFIX=${TEST_USERNAME_PREFIX:-k6}" \
  "ITEM_ID=${ITEM_ID:-1}" \
  "$K6_BIN" run loadtest/raffle.js
k6_exit_code=$?
set -e

if [[ ! -s "$SUMMARY_PATH" ]]; then
  echo "BLOCKED: k6 did not write a request summary. The reservation remains active; inspect before retrying." >&2
  exit 2
fi
python3 scripts/loadtest_ledger.py complete \
  --ledger "$LEDGER_PATH" \
  --session-id "$SESSION_ID" \
  --run-id "$RUN_ID" \
  --summary "$SUMMARY_PATH" \
  --exit-code "$k6_exit_code"

exit "$k6_exit_code"
