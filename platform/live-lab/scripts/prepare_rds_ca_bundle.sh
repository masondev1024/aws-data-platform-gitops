#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
: "${AWS_PROFILE:?AWS_PROFILE must name the approved SSO profile}"
: "${AWS_REGION:?AWS_REGION must be explicitly set to ap-northeast-2}"
: "${EXPECTED_ACCOUNT_ID:?EXPECTED_ACCOUNT_ID must be the approved 12-digit account ID}"
: "${SESSION_ID:?SESSION_ID must be the Terraform session_id}"
: "${APPROVAL_ID:?APPROVAL_ID must be the recorded approval_id}"
: "${KUBE_CONTEXT:?KUBE_CONTEXT must point to the exact session EKS cluster}"
NAMESPACE="${NAMESPACE:-platform-validation}"
CONFIGMAP_NAME="rds-ca-bundle"
SOURCE_URL="https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
PROJECT="kyobo-platform-live-lab"
CLUSTER_NAME="kyobo-${SESSION_ID}"

command -v curl >/dev/null || { echo "BLOCKED: curl is required" >&2; exit 2; }
command -v kubectl >/dev/null || { echo "BLOCKED: kubectl is required" >&2; exit 2; }
[[ "$AWS_REGION" == "ap-northeast-2" && "$EXPECTED_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || { echo "BLOCKED: account/region do not match the approved scope." >&2; exit 2; }
[[ "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ && "$APPROVAL_ID" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]] || { echo "BLOCKED: invalid session or approval ID." >&2; exit 2; }

identity="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity --query Account --output text)"
[[ "$identity" == "$EXPECTED_ACCOUNT_ID" ]] || { echo "BLOCKED: AWS account mismatch." >&2; exit 2; }
cluster_json="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" eks describe-cluster --name "$CLUSTER_NAME" --output json)"
read -r cluster_arn cluster_endpoint cluster_status project_tag session_tag approval_tag < <(python3 -c 'import json,sys; c=json.loads(sys.argv[1])["cluster"]; t=c.get("tags", {}); print(c.get("arn", ""), c.get("endpoint", ""), c.get("status", ""), t.get("Project", ""), t.get("Session", ""), t.get("Approval", ""))' "$cluster_json")
[[ "$cluster_arn" == "arn:aws:eks:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:cluster/${CLUSTER_NAME}" && "$cluster_status" == "ACTIVE" && "$project_tag" == "$PROJECT" && "$session_tag" == "$SESSION_ID" && "$approval_tag" == "$APPROVAL_ID" ]] || { echo "BLOCKED: EKS identity/status/ownership tags do not match this session." >&2; exit 2; }
context_endpoint="$(kubectl --context "$KUBE_CONTEXT" config view --minify -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["clusters"][0]["cluster"]["server"])')"
[[ "$context_endpoint" == "$cluster_endpoint" ]] || { echo "BLOCKED: KUBE_CONTEXT endpoint does not match the approved EKS cluster." >&2; exit 2; }
kube() { kubectl --context "$KUBE_CONTEXT" "$@"; }

temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/rds-ca-bundle.XXXXXX")"
chmod 700 "$temp_dir"
trap 'rm -rf "$temp_dir"' EXIT

bundle="$temp_dir/global-bundle.pem"
curl --fail --silent --show-error --location \
  --proto '=https' --tlsv1.2 \
  "$SOURCE_URL" \
  --output "$bundle"
chmod 600 "$bundle"

if ! grep -q -- '-----BEGIN CERTIFICATE-----' "$bundle" || ! grep -q -- '-----END CERTIFICATE-----' "$bundle"; then
  echo "BLOCKED: downloaded RDS CA bundle does not contain PEM certificates" >&2
  exit 2
fi

if kube get configmap "$CONFIGMAP_NAME" --namespace "$NAMESPACE" >/dev/null 2>&1; then
  existing="$temp_dir/existing.pem"
  if ! kube get configmap "$CONFIGMAP_NAME" --namespace "$NAMESPACE" \
    --output 'jsonpath={.data.global-bundle\.pem}' > "$existing"; then
    echo "BLOCKED: existing RDS CA ConfigMap cannot be read" >&2
    exit 2
  fi
  if ! cmp -s "$bundle" "$existing"; then
    echo "BLOCKED: $CONFIGMAP_NAME exists with different bytes; inspect this session-owned object before replacing it" >&2
    exit 2
  fi
  echo "Verified existing $NAMESPACE/$CONFIGMAP_NAME against the current AWS RDS global bundle."
  exit 0
fi

kube create configmap "$CONFIGMAP_NAME" \
  --namespace "$NAMESPACE" \
  --from-file="global-bundle.pem=$bundle" \
  --dry-run=client \
  --output yaml | kube create --filename -

echo "Created bootstrap trust bundle $NAMESPACE/$CONFIGMAP_NAME from $SOURCE_URL."
