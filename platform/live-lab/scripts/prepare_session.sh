#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
: "${AWS_PROFILE:?AWS_PROFILE must identify the approved SSO profile}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"
[[ "$AWS_REGION" == ap-northeast-2 ]] || { echo "BLOCKED: only the approved Seoul region is allowed." >&2; exit 2; }

SESSION_HOURS="${SESSION_HOURS:-3}"
SESSION_BUDGET_USD="${SESSION_BUDGET_USD:-5.50}"
SESSION_RESERVE_USD="${SESSION_RESERVE_USD:-1.00}"
SESSION_REQUESTS="${SESSION_REQUESTS:-40000}"
# Fail before even the identity query; no cloud side effects are needed to reject cost.
python3 platform/live-lab/scripts/estimate_session_cost.py \
  --hours "$SESSION_HOURS" --budget "$SESSION_BUDGET_USD" \
  --reserve "$SESSION_RESERVE_USD" --requests "$SESSION_REQUESTS" >/dev/null
export LIVE_LAB_HOURS="$SESSION_HOURS" LIVE_LAB_BUDGET="$SESSION_BUDGET_USD"

identity="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity --output json)"
account_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])' <<< "$identity")"
caller_arn="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"])' <<< "$identity")"
if [[ -n "${EXPECTED_ACCOUNT_ID:-}" && "$EXPECTED_ACCOUNT_ID" != "$account_id" ]]; then
  echo "BLOCKED: active SSO account differs from EXPECTED_ACCOUNT_ID." >&2
  exit 2
fi
EXPECTED_ACCOUNT_ID="$account_id"
SESSION_ID="${SESSION_ID:-live-$(date -u +%y%m%d)-$(openssl rand -hex 3)}"
APPROVAL_ID="${APPROVAL_ID:-SS0-$(date -u +%Y%m%d)-codex-live-lab}"
OPERATOR_NAME="${OPERATOR_NAME:-local-codex-session}"
RECOVERY_CONTACT="${RECOVERY_CONTACT:-local-operator}"
[[ "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ ]] || { echo "BLOCKED: invalid SESSION_ID." >&2; exit 2; }
[[ "$APPROVAL_ID" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]] || { echo "BLOCKED: invalid APPROVAL_ID." >&2; exit 2; }

if [[ -z "${OPERATOR_CIDR:-}" ]]; then
  public_ip="$(curl --fail --silent --show-error --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')"
  OPERATOR_CIDR="${public_ip}/32"
fi
python3 - "$OPERATOR_CIDR" <<'PY'
import ipaddress
import sys
network = ipaddress.ip_network(sys.argv[1], strict=True)
if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global:
    raise SystemExit("OPERATOR_CIDR must be one globally routable public IPv4 /32")
PY

az_json="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ec2 describe-availability-zones \
  --filters Name=state,Values=available --query 'AvailabilityZones[].ZoneName' --output json)"
azs="$(python3 -c 'import json,sys; values=json.load(sys.stdin); len(values)>=2 or sys.exit("fewer than two available AZs"); print(json.dumps(values[:2]))' <<< "$az_json")"
session_file="platform/live-lab/evidence/${SESSION_ID}.auto.tfvars.json"
mkdir -p platform/live-lab/evidence
chmod 700 platform/live-lab/evidence
[[ ! -e "$session_file" ]] || { echo "BLOCKED: session tfvars evidence already exists; do not overwrite it." >&2; exit 2; }
export LIVE_LAB_ACCOUNT="$EXPECTED_ACCOUNT_ID" LIVE_LAB_REGION="$AWS_REGION" LIVE_LAB_SESSION="$SESSION_ID"
export LIVE_LAB_APPROVAL="$APPROVAL_ID" LIVE_LAB_OPERATOR="$OPERATOR_NAME" LIVE_LAB_RECOVERY="$RECOVERY_CONTACT"
export LIVE_LAB_CIDR="$OPERATOR_CIDR" LIVE_LAB_AZS="$azs"
export LIVE_LAB_SECRET_ID="kyobo-live-lab/${SESSION_ID}/mysql-master-password"
python3 - "$session_file" <<'PY'
import json
import os
import sys

values = {
    "aws_region": os.environ["LIVE_LAB_REGION"],
    "aws_account_id": os.environ["LIVE_LAB_ACCOUNT"],
    "operator_cidr": os.environ["LIVE_LAB_CIDR"],
    "operator_name": os.environ["LIVE_LAB_OPERATOR"],
    "approval_id": os.environ["LIVE_LAB_APPROVAL"],
    "session_id": os.environ["LIVE_LAB_SESSION"],
    "recovery_contact": os.environ["LIVE_LAB_RECOVERY"],
    "apply_approval_phrase": "APPROVED_FOR_EPHEMERAL_APPLY",
    "availability_zones": json.loads(os.environ["LIVE_LAB_AZS"]),
    "db_master_password_secret_id": os.environ["LIVE_LAB_SECRET_ID"],
    "max_session_hours": float(os.environ["LIVE_LAB_HOURS"]),
    "cost_budget_usd": float(os.environ["LIVE_LAB_BUDGET"]),
}
with open(sys.argv[1], "x", encoding="utf-8") as handle:
    json.dump(values, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.chmod(sys.argv[1], 0o600)
PY

echo "Prepared protected, non-secret Terraform session inputs at $session_file"
echo "AWS account: $EXPECTED_ACCOUNT_ID; region: $AWS_REGION; operator ARN: $caller_arn"
echo "Session: $SESSION_ID; approval record: $APPROVAL_ID; operator CIDR: $OPERATOR_CIDR"
echo "This command made read-only AWS identity/AZ queries; it did not create AWS resources."
