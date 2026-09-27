#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
: "${AWS_PROFILE:?AWS_PROFILE must identify the approved SSO profile}"
: "${AWS_REGION:?AWS_REGION must be ap-northeast-2}"
: "${EXPECTED_ACCOUNT_ID:?EXPECTED_ACCOUNT_ID is required}"
: "${SESSION_ID:?SESSION_ID is required}"
: "${APPROVAL_ID:?APPROVAL_ID is required}"
: "${KUBE_CONTEXT:?KUBE_CONTEXT must identify this session cluster}"
: "${LIVE_LAB_TFVARS:?LIVE_LAB_TFVARS must point to the protected session tfvars file}"
: "${SESSION_DEADLINE_EPOCH:?SESSION_DEADLINE_EPOCH is required}"

EVIDENCE_DIR="platform/live-lab/evidence"
PID_FILE="$EVIDENCE_DIR/deadline-watchdog.pid"
LOG_FILE="$EVIDENCE_DIR/deadline-watchdog.log"
STATUS_FILE="$EVIDENCE_DIR/deadline-watchdog-status.json"
MODE="${1:-start}"
[[ "$MODE" == start || "$MODE" == --wait ]] || { echo "Usage: deadline_watchdog.sh [start|--wait]" >&2; exit 2; }
[[ "$AWS_REGION" == ap-northeast-2 && "$EXPECTED_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || { echo "BLOCKED: account/region mismatch." >&2; exit 2; }
[[ "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ && "$APPROVAL_ID" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]] || { echo "BLOCKED: invalid session or approval ID." >&2; exit 2; }
[[ "$SESSION_DEADLINE_EPOCH" =~ ^[0-9]{10}$ ]] || { echo "BLOCKED: deadline must be a Unix epoch in seconds." >&2; exit 2; }
now="$(date +%s)"
remaining=$((SESSION_DEADLINE_EPOCH - now))
(( remaining >= 60 && remaining <= 7200 )) || { echo "BLOCKED: cleanup deadline must be 1 minute to 2 hours from watchdog start." >&2; exit 2; }
command -v caffeinate >/dev/null || { echo "BLOCKED: caffeinate is required to prevent idle sleep during the approved live window." >&2; exit 2; }
[[ -f "$LIVE_LAB_TFVARS" && ! -L "$LIVE_LAB_TFVARS" ]] || { echo "BLOCKED: session tfvars are missing or symlinked." >&2; exit 2; }
tfvars_path="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$LIVE_LAB_TFVARS")"
[[ "$tfvars_path" == "$repo_root/$EVIDENCE_DIR/"* ]] || { echo "BLOCKED: session tfvars must remain in the private evidence directory." >&2; exit 2; }
tfvars_mode="$(stat -f '%Lp' "$tfvars_path" 2>/dev/null || stat -c '%a' "$tfvars_path")"
[[ "$tfvars_mode" == 600 ]] || { echo "BLOCKED: session tfvars must have mode 0600." >&2; exit 2; }
python3 - "$tfvars_path" "$EXPECTED_ACCOUNT_ID" "$AWS_REGION" "$SESSION_ID" "$APPROVAL_ID" <<'PY'
import json
import sys

path, account, region, session, approval = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    values = json.load(handle)
expected = {
    "aws_account_id": account,
    "aws_region": region,
    "session_id": session,
    "approval_id": approval,
}
if any(values.get(key) != value for key, value in expected.items()):
    raise SystemExit("BLOCKED: protected tfvars identity does not match watchdog scope")
if not 0 < values.get("cost_budget_usd", 0) <= 5.5 or not 1 <= values.get("max_session_hours", 0) <= 3:
    raise SystemExit("BLOCKED: tfvars exceed the approved short-session cost/lifetime")
PY

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
identity="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity --query Account --output text)"
[[ "$identity" == "$EXPECTED_ACCOUNT_ID" ]] || { echo "BLOCKED: watchdog SSO profile resolved to another AWS account." >&2; exit 2; }

mkdir -p "$EVIDENCE_DIR"
chmod 700 "$EVIDENCE_DIR"
if [[ "$MODE" == start ]]; then
  [[ ! -e "$STATUS_FILE" ]] || { echo "BLOCKED: watchdog status already exists for this session; inspect it before retrying." >&2; exit 2; }
  [[ ! -e "$PID_FILE" ]] || { echo "BLOCKED: watchdog PID evidence already exists; inspect it before retrying." >&2; exit 2; }
  watchdog_pid="$$"
  printf '%s\n' "$watchdog_pid" > "$PID_FILE"
  chmod 600 "$PID_FILE"
  python3 platform/live-lab/scripts/live_lab_lifecycle.py write-status "$STATUS_FILE" active \
    "foreground local watchdog PID $watchdog_pid will start scoped teardown at Unix deadline $SESSION_DEADLINE_EPOCH; keep this Mac powered on and online" \
    --session "$SESSION_ID" --region "$AWS_REGION"
  echo "Started foreground local deadline watchdog PID=$watchdog_pid; scheduled cleanup begins at $SESSION_DEADLINE_EPOCH. Keep this managed terminal session alive."
fi

caffeinate -dimsu -t "$((remaining + 3600))" &
caffeinate_pid=$!
on_exit() {
  kill "$caffeinate_pid" 2>/dev/null || true
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
while (( $(date +%s) < SESSION_DEADLINE_EPOCH )); do
  sleep 30
done

export TEARDOWN_CONFIRM="destroy-${SESSION_ID}-${AWS_REGION}"
retry_deadline=$((SESSION_DEADLINE_EPOCH + 3600))
while (( $(date +%s) < retry_deadline )); do
  if bash platform/live-lab/scripts/teardown_live_lab.sh; then
    python3 platform/live-lab/scripts/live_lab_lifecycle.py write-status "$STATUS_FILE" completed \
      "deadline-triggered session-scoped teardown completed" --session "$SESSION_ID" --region "$AWS_REGION"
    echo "Deadline teardown completed for session $SESSION_ID."
    exit 0
  fi
  echo "Deadline teardown attempt failed; retrying in 5 minutes within the one-hour cleanup allowance." >&2
  sleep 300
done
python3 platform/live-lab/scripts/live_lab_lifecycle.py write-status "$STATUS_FILE" incomplete \
  "deadline cleanup retries exhausted; manual intervention is required and AWS charges may continue" \
  --session "$SESSION_ID" --region "$AWS_REGION"
echo "CRITICAL: deadline cleanup did not complete; inspect AWS residuals immediately." >&2
exit 2
