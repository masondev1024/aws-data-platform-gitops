#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

: "${AWS_PROFILE:?AWS_PROFILE must name the approved SSO profile}"
: "${AWS_REGION:?AWS_REGION must be explicitly set to ap-northeast-2}"
: "${EXPECTED_ACCOUNT_ID:?EXPECTED_ACCOUNT_ID must be the approved 12-digit account ID}"
: "${SESSION_ID:?SESSION_ID must be the Terraform session_id}"
: "${APPROVAL_ID:?APPROVAL_ID must be the recorded approval_id}"
: "${KUBE_CONTEXT:?KUBE_CONTEXT must name the exact session EKS context}"

PROJECT="kyobo-platform-live-lab"
CLUSTER_NAME="kyobo-${SESSION_ID}"
NAMESPACE="${NAMESPACE:-platform-validation}"
SECRET_NAME="${K8S_SECRET_NAME:-raffle-secret}"
MIGRATION_SECRET_NAME="${K8S_MIGRATION_SECRET_NAME:-raffle-migration-secret}"
DB_ADMIN_USER="${DB_ADMIN_USER:-raffle_admin}"
DB_MASTER_PASSWORD_SECRET_ID="${DB_MASTER_PASSWORD_SECRET_ID:-kyobo-live-lab/${SESSION_ID}/mysql-master-password}"
OPERATION_ID="runtime-secret-$(date -u +%Y%m%d%H%M%S)-$$-$RANDOM"
SECRET_CREATE_ATTEMPTED=0

if [[ "$AWS_REGION" != "ap-northeast-2" || ! "$EXPECTED_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "BLOCKED: this live lab is approved only for the exact Seoul region and a 12-digit account." >&2
  exit 2
fi
if [[ ! "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ || ! "$APPROVAL_ID" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]]; then
  echo "BLOCKED: invalid session_id or approval_id format." >&2
  exit 2
fi
if [[ ! "$DB_ADMIN_USER" =~ ^[A-Za-z][A-Za-z0-9_]{2,15}$ ]]; then
  echo "BLOCKED: DB_ADMIN_USER must match the Terraform RDS master username contract." >&2
  exit 2
fi

identity="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity --query Account --output text)"
[[ "$identity" == "$EXPECTED_ACCOUNT_ID" ]] || { echo "BLOCKED: AWS account mismatch." >&2; exit 2; }

cluster_json="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" eks describe-cluster --name "$CLUSTER_NAME" --output json)"
read -r cluster_arn cluster_endpoint cluster_status cluster_project cluster_session cluster_approval < <(
  python3 - "$cluster_json" <<'PY'
import json
import sys

cluster = json.loads(sys.argv[1])["cluster"]
tags = cluster.get("tags", {})
print(cluster["arn"], cluster["endpoint"], cluster["status"], tags.get("Project", ""), tags.get("Session", ""), tags.get("Approval", ""))
PY
)
expected_cluster_arn="arn:aws:eks:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:cluster/${CLUSTER_NAME}"
if [[ "$cluster_arn" != "$expected_cluster_arn" || "$cluster_status" != "ACTIVE" || "$cluster_project" != "$PROJECT" || "$cluster_session" != "$SESSION_ID" || "$cluster_approval" != "$APPROVAL_ID" ]]; then
  echo "BLOCKED: EKS cluster identity/status/tags do not match the approved live-lab session." >&2
  exit 2
fi

context_server="$(kubectl --context "$KUBE_CONTEXT" config view --minify -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["clusters"][0]["cluster"]["server"])')"
if [[ "$context_server" != "$cluster_endpoint" ]]; then
  echo "BLOCKED: the explicit Kubernetes context does not point at the approved EKS endpoint." >&2
  exit 2
fi
kubectl --context "$KUBE_CONTEXT" get namespace "$NAMESPACE" --output name >/dev/null

existing_secret="$(kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" get secret "$SECRET_NAME" --ignore-not-found --output name)"
existing_migration_secret="$(kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" get secret "$MIGRATION_SECRET_NAME" --ignore-not-found --output name)"
if [[ -n "$existing_secret" || -n "$existing_migration_secret" ]]; then
  echo "BLOCKED: one or more session runtime secrets already exist; inspect them, do not overwrite them." >&2
  exit 2
fi

master_password="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" secretsmanager get-secret-value \
  --secret-id "$DB_MASTER_PASSWORD_SECRET_ID" --query SecretString --output text)"
if [[ ! "$master_password" =~ ^[A-Fa-f0-9]{40}$ ]]; then
  echo "BLOCKED: the session RDS master secret does not have the expected generated format." >&2
  exit 2
fi
app_password="$(openssl rand -hex 20)"
flask_secret="$(openssl rand -hex 32)"

cleanup_owned_k8s_secret() {
  (( SECRET_CREATE_ATTEMPTED == 1 )) || return 0
  local name operation_tag
  for name in "$SECRET_NAME" "$MIGRATION_SECRET_NAME"; do
    operation_tag="$(kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" get secret "$name" \
      -o jsonpath='{.metadata.labels.live-lab-operation}' 2>/dev/null || true)"
    if [[ "$operation_tag" == "$OPERATION_ID" ]]; then
      kubectl --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" delete secret "$name" --wait=false >/dev/null 2>&1 || true
    fi
  done
}
trap cleanup_owned_k8s_secret ERR
trap 'cleanup_owned_k8s_secret; exit 130' INT
trap 'cleanup_owned_k8s_secret; exit 143' TERM

SECRET_CREATE_ATTEMPTED=1
export LIVE_LAB_NAMESPACE="$NAMESPACE" LIVE_LAB_APP_SECRET="$SECRET_NAME"
export LIVE_LAB_MIGRATION_SECRET="$MIGRATION_SECRET_NAME" LIVE_LAB_SESSION="$SESSION_ID"
export LIVE_LAB_APPROVAL="$APPROVAL_ID" LIVE_LAB_OPERATION="$OPERATION_ID"
export LIVE_LAB_DB_ADMIN_USER="$DB_ADMIN_USER" LIVE_LAB_DB_ADMIN_PASSWORD="$master_password"
export LIVE_LAB_DB_APP_USER="raffle_app" LIVE_LAB_DB_APP_PASSWORD="$app_password"
export LIVE_LAB_FLASK_SECRET="$flask_secret"
python3 - <<'PY' | kubectl --context "$KUBE_CONTEXT" create --filename - >/dev/null
import base64
import json
import os


def secret(name, values):
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": os.environ["LIVE_LAB_NAMESPACE"],
            "labels": {
                "app.kubernetes.io/managed-by": "local-live-lab",
                "live-lab-session": os.environ["LIVE_LAB_SESSION"],
                "live-lab-approval": os.environ["LIVE_LAB_APPROVAL"],
                "live-lab-operation": os.environ["LIVE_LAB_OPERATION"],
            },
        },
        "type": "Opaque",
        "data": {
            key: base64.b64encode(value.encode()).decode()
            for key, value in values.items()
        },
    }


app_values = {
    "DB_APP_USER": os.environ["LIVE_LAB_DB_APP_USER"],
    "DB_APP_PASSWORD": os.environ["LIVE_LAB_DB_APP_PASSWORD"],
    "SECRET_KEY": os.environ["LIVE_LAB_FLASK_SECRET"],
}
migration_values = {
    "DB_ADMIN_USER": os.environ["LIVE_LAB_DB_ADMIN_USER"],
    "DB_ADMIN_PASSWORD": os.environ["LIVE_LAB_DB_ADMIN_PASSWORD"],
    **app_values,
}
print(json.dumps(secret(os.environ["LIVE_LAB_APP_SECRET"], app_values)))
print("---")
print(json.dumps(secret(os.environ["LIVE_LAB_MIGRATION_SECRET"], migration_values)))
PY

SECRET_CREATE_ATTEMPTED=0
trap - ERR INT TERM
unset master_password app_password flask_secret LIVE_LAB_DB_ADMIN_PASSWORD LIVE_LAB_DB_APP_PASSWORD LIVE_LAB_FLASK_SECRET
echo "Created session-scoped application and migration Secret metadata in $CLUSTER_NAME/$NAMESPACE (values not printed)."
