#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

: "${AWS_PROFILE:?AWS_PROFILE must name the approved SSO profile}"
: "${AWS_REGION:?AWS_REGION must be explicitly set to ap-northeast-2}"
: "${EXPECTED_ACCOUNT_ID:?EXPECTED_ACCOUNT_ID must be the approved 12-digit account ID}"
: "${SESSION_ID:?SESSION_ID must be the Terraform session_id}"
: "${APPROVAL_ID:?APPROVAL_ID must be the recorded approval_id}"

PROJECT="kyobo-platform-live-lab"
SECRET_NAME="${DB_MASTER_PASSWORD_SECRET_ID:-kyobo-live-lab/${SESSION_ID}/mysql-master-password}"
OPERATION_ID="secret-$(date -u +%Y%m%d%H%M%S)-$$-$RANDOM"
CREATE_ATTEMPTED=0

if [[ "$AWS_REGION" != "ap-northeast-2" || ! "$EXPECTED_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "BLOCKED: this live lab is approved only for the exact Seoul region and a 12-digit account." >&2
  exit 2
fi
if [[ ! "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ || ! "$APPROVAL_ID" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]]; then
  echo "BLOCKED: invalid session_id or approval_id format." >&2
  exit 2
fi

identity="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity --query Account --output text)"
if [[ "$identity" != "$EXPECTED_ACCOUNT_ID" ]]; then
  echo "BLOCKED: active SSO identity account does not match EXPECTED_ACCOUNT_ID." >&2
  exit 2
fi

set +e
describe_error="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" secretsmanager describe-secret --secret-id "$SECRET_NAME" 2>&1 >/dev/null)"
describe_status=$?
set -e
if (( describe_status == 0 )); then
  echo "BLOCKED: the exact session Secrets Manager name already exists; inspect it, do not overwrite it." >&2
  exit 2
fi
if [[ "$describe_error" != *"ResourceNotFoundException"* ]]; then
  echo "BLOCKED: could not prove the session secret is absent; inspect AWS access/connectivity before proceeding." >&2
  exit 2
fi

master_password="$(openssl rand -hex 20)"

cleanup_new_cloud_secret() {
  (( CREATE_ATTEMPTED == 1 )) || return 0
  local tagged_operation
  tagged_operation="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" secretsmanager describe-secret \
    --secret-id "$SECRET_NAME" --query "Tags[?Key=='Operation'].Value | [0]" --output text 2>/dev/null || true)"
  if [[ "$tagged_operation" == "$OPERATION_ID" ]]; then
    aws --profile "$AWS_PROFILE" --region "$AWS_REGION" secretsmanager delete-secret \
      --secret-id "$SECRET_NAME" --force-delete-without-recovery >/dev/null 2>&1 || true
  fi
}
trap cleanup_new_cloud_secret ERR
trap 'cleanup_new_cloud_secret; exit 130' INT
trap 'cleanup_new_cloud_secret; exit 143' TERM

# Set before the API call so an ambiguous client timeout can be recovered by
# checking the unique operation tag, without deleting a concurrently-created secret.
CREATE_ATTEMPTED=1
secret_arn="$(printf '%s' "$master_password" | aws --profile "$AWS_PROFILE" --region "$AWS_REGION" secretsmanager create-secret \
  --name "$SECRET_NAME" \
  --description "Ephemeral live-lab RDS master password for session $SESSION_ID" \
  --secret-string file:///dev/stdin \
  --tags "Key=Project,Value=$PROJECT" "Key=Session,Value=$SESSION_ID" "Key=Approval,Value=$APPROVAL_ID" \
         "Key=ManagedBy,Value=local-live-lab" "Key=Operation,Value=$OPERATION_ID" \
  --query ARN --output text)"

if [[ ! "$secret_arn" =~ ^arn:aws:secretsmanager:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:secret: ]]; then
  echo "BLOCKED: Secrets Manager returned an ARN outside the approved account and region." >&2
  exit 2
fi

CREATE_ATTEMPTED=0
trap - ERR INT TERM
unset master_password
echo "Created session-scoped RDS master secret metadata: name=$SECRET_NAME arn=$secret_arn"
echo "Kubernetes runtime credentials are created separately after EKS and its namespace are ready."
