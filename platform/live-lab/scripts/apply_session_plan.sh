#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
: "${AWS_PROFILE:?AWS_PROFILE must identify the approved SSO profile}"
: "${LIVE_LAB_TFVARS:?LIVE_LAB_TFVARS must point to the protected session tfvars file}"

evidence_dir="$repo_root/platform/live-lab/evidence"
terraform_dir="$repo_root/platform/live-lab/terraform"
[[ -f "$LIVE_LAB_TFVARS" && ! -L "$LIVE_LAB_TFVARS" ]] || {
  echo "BLOCKED: session tfvars are missing or symlinked." >&2
  exit 2
}
tfvars_path="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$LIVE_LAB_TFVARS")"
[[ "$tfvars_path" == "$evidence_dir/"* ]] || {
  echo "BLOCKED: session tfvars must remain in the private evidence directory." >&2
  exit 2
}
tfvars_mode="$(stat -f '%Lp' "$tfvars_path" 2>/dev/null || stat -c '%a' "$tfvars_path")"
[[ "$tfvars_mode" == 600 ]] || {
  echo "BLOCKED: tfvars mode must be 0600." >&2
  exit 2
}

read -r account_id region session_id approval_id budget hours < <(python3 - "$tfvars_path" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    values = json.load(handle)
required = (
    "aws_account_id", "aws_region", "session_id", "approval_id",
    "cost_budget_usd", "max_session_hours",
)
if any(key not in values for key in required):
    raise SystemExit("session tfvars are missing required approval inputs")
print(
    values["aws_account_id"], values["aws_region"], values["session_id"],
    values["approval_id"], values["cost_budget_usd"], values["max_session_hours"],
)
PY
)
[[ "$account_id" =~ ^[0-9]{12}$ && "$region" == "ap-northeast-2" ]] || {
  echo "BLOCKED: session tfvars are outside the approved account or Seoul region." >&2
  exit 2
}
[[ "$session_id" =~ ^[a-z0-9][a-z0-9-]{5,40}$ && "$approval_id" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]] || {
  echo "BLOCKED: invalid session or approval ID in protected tfvars." >&2
  exit 2
}
if [[ -n "${AWS_REGION:-}" && "$AWS_REGION" != "$region" ]]; then
  echo "BLOCKED: AWS_REGION differs from the approved session region." >&2
  exit 2
fi
export AWS_REGION="$region"

requests="${SESSION_REQUESTS:-200000}"
[[ "$requests" =~ ^[0-9]+$ ]] || { echo "BLOCKED: SESSION_REQUESTS must be an integer." >&2; exit 2; }
python3 platform/live-lab/scripts/estimate_session_cost.py \
  --hours "$hours" --budget "$budget" --reserve 1.00 --requests "$requests" >/dev/null

identity="$(aws --profile "$AWS_PROFILE" --region "$region" sts get-caller-identity --query Account --output text)"
[[ "$identity" == "$account_id" ]] || {
  echo "BLOCKED: active SSO account does not match the protected session tfvars." >&2
  exit 2
}

run_id="${LIVE_LAB_PLAN_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
[[ "$run_id" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,64}$ ]] || {
  echo "BLOCKED: invalid LIVE_LAB_PLAN_RUN_ID." >&2
  exit 2
}
mkdir -p "$evidence_dir"
chmod 700 "$evidence_dir"
plan_file="$evidence_dir/${session_id}-validated-${run_id}.tfplan"
plan_json="$evidence_dir/${session_id}-validated-${run_id}.plan.json"
[[ ! -e "$plan_file" && ! -e "$plan_json" ]] || {
  echo "BLOCKED: plan evidence already exists; refusing to overwrite it." >&2
  exit 2
}
workspace="$(terraform -chdir="$terraform_dir" workspace show)"
[[ "$workspace" == default ]] || {
  echo "BLOCKED: live-lab Terraform must use the default workspace so teardown can find the applied state." >&2
  exit 2
}

terraform -chdir="$terraform_dir" plan \
  -input=false \
  -var-file="$tfvars_path" \
  -var=apply_approval_phrase=APPROVED_FOR_EPHEMERAL_APPLY \
  -out="$plan_file"
chmod 600 "$plan_file"
plan_digest="$(shasum -a 256 "$plan_file" | awk '{print $1}')"

terraform -chdir="$terraform_dir" show -json "$plan_file" > "$plan_json"
chmod 600 "$plan_json"
python3 platform/live-lab/scripts/validate_session_plan.py "$plan_json" \
  --account "$account_id" \
  --region "$region" \
  --session "$session_id" \
  --approval "$approval_id" \
  --budget "$budget" \
  --hours "$hours" \
  --reserve 1.00 \
  --requests "$requests"

[[ -n "$plan_digest" && "$(shasum -a 256 "$plan_file" | awk '{print $1}')" == "$plan_digest" ]] || {
  echo "BLOCKED: saved Terraform plan changed after validation." >&2
  exit 2
}
echo "Applying the exact validated plan for session $session_id (sha256=$plan_digest)."
terraform -chdir="$terraform_dir" apply -input=false -auto-approve "$plan_file"
