#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

: "${AWS_PROFILE:?AWS_PROFILE must identify the approved SSO profile}"
: "${AWS_REGION:?AWS_REGION must be explicitly set to ap-northeast-2}"
: "${EXPECTED_ACCOUNT_ID:?EXPECTED_ACCOUNT_ID must be the approved 12-digit account}"
: "${SESSION_ID:?SESSION_ID must be the Terraform session_id}"
: "${APPROVAL_ID:?APPROVAL_ID must be the approved session approval_id}"
: "${KUBE_CONTEXT:?KUBE_CONTEXT must name the exact session EKS context}"

PROJECT="kyobo-platform-live-lab"
CLUSTER_NAME="kyobo-${SESSION_ID}"
[[ "$AWS_REGION" == "ap-northeast-2" && "$EXPECTED_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || { echo "BLOCKED: account/region do not match the approved scope." >&2; exit 2; }
[[ "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ && "$APPROVAL_ID" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]] || { echo "BLOCKED: invalid session or approval ID." >&2; exit 2; }

identity="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity --query Account --output text)"
[[ "$identity" == "$EXPECTED_ACCOUNT_ID" ]] || { echo "BLOCKED: AWS identity account mismatch." >&2; exit 2; }
cluster_json="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" eks describe-cluster --name "$CLUSTER_NAME" --output json)"
read -r cluster_arn cluster_endpoint cluster_status project_tag session_tag approval_tag < <(python3 -c 'import json,sys; cluster=json.loads(sys.argv[1])["cluster"]; tags=cluster.get("tags", {}); print(cluster.get("arn", ""), cluster.get("endpoint", ""), cluster.get("status", ""), tags.get("Project", ""), tags.get("Session", ""), tags.get("Approval", ""))' "$cluster_json")
[[ "$cluster_arn" == "arn:aws:eks:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:cluster/${CLUSTER_NAME}" && "$cluster_status" == "ACTIVE" ]] || { echo "BLOCKED: exact session EKS cluster is not ACTIVE." >&2; exit 2; }
[[ "$project_tag" == "$PROJECT" && "$session_tag" == "$SESSION_ID" && "$approval_tag" == "$APPROVAL_ID" ]] || { echo "BLOCKED: EKS Project/Session/Approval tags mismatch." >&2; exit 2; }
context_endpoint="$(kubectl --context "$KUBE_CONTEXT" config view --minify -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["clusters"][0]["cluster"]["server"])')"
[[ "$context_endpoint" == "$cluster_endpoint" ]] || { echo "BLOCKED: KUBE_CONTEXT endpoint does not match the approved cluster." >&2; exit 2; }

terraform_dir="platform/live-lab/terraform"
vpc_id="$(terraform -chdir="$terraform_dir" output -raw vpc_id)"
lbc_role_arn="$(terraform -chdir="$terraform_dir" output -raw aws_load_balancer_controller_role_arn)"
[[ "$vpc_id" =~ ^vpc-[0-9a-f]+$ && "$lbc_role_arn" == "arn:aws:iam::${EXPECTED_ACCOUNT_ID}:role/kyobo-${SESSION_ID}-aws-load-balancer-controller" ]] || { echo "BLOCKED: Terraform outputs are not scoped to this session." >&2; exit 2; }

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/live-lab-bootstrap.XXXXXX")"
cleanup() { rm -rf "$tmp_dir"; }
trap cleanup EXIT
export SESSION_ID APPROVAL_ID
helm_repo_config="$tmp_dir/repositories.yaml"
helm_repo_cache="$tmp_dir/repository-cache"
install_file="$tmp_dir/argo-rollouts-install.yaml"
metrics_file="$tmp_dir/metrics-server-components.yaml"

curl --fail --silent --show-error --location \
  https://github.com/argoproj/argo-rollouts/releases/download/v1.10.0/install.yaml -o "$install_file"
printf '%s  %s\n' ca8a1785391026023627c8a12db5422e227d7a7a6ec7c01f99f7d5b1726ed53c "$install_file" | shasum -a 256 --check --status || { echo "BLOCKED: Argo Rollouts v1.10.0 manifest checksum mismatch." >&2; exit 2; }
curl --fail --silent --show-error --location \
  https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.0/components.yaml -o "$metrics_file"
printf '%s  %s\n' 1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b "$metrics_file" | shasum -a 256 --check --status || { echo "BLOCKED: metrics-server v0.9.0 manifest checksum mismatch." >&2; exit 2; }

kubectl --context "$KUBE_CONTEXT" create namespace argo-rollouts --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f - >/dev/null
kubectl --context "$KUBE_CONTEXT" create namespace monitoring --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f - >/dev/null
kubectl --context "$KUBE_CONTEXT" apply --server-side --field-manager=live-lab-bootstrap --namespace argo-rollouts -f "$install_file" >/dev/null
kubectl --context "$KUBE_CONTEXT" apply -f "$metrics_file" >/dev/null
kubectl --context "$KUBE_CONTEXT" -n kube-system rollout status deployment/metrics-server --timeout=180s
kubectl --context "$KUBE_CONTEXT" -n argo-rollouts rollout status deployment/argo-rollouts --timeout=180s

export LIVE_LAB_LBC_ROLE_ARN="$lbc_role_arn"
kubectl --context "$KUBE_CONTEXT" create serviceaccount aws-load-balancer-controller -n kube-system --dry-run=client -o json |
  python3 -c 'import json,os,sys; obj=json.load(sys.stdin); obj["metadata"].setdefault("annotations", {})["eks.amazonaws.com/role-arn"]=os.environ["LIVE_LAB_LBC_ROLE_ARN"]; print(json.dumps(obj))' |
  kubectl --context "$KUBE_CONTEXT" apply -f - >/dev/null
mkdir -p platform/live-lab/evidence
chmod 700 platform/live-lab/evidence
grafana_secret_name="$(kubectl --context "$KUBE_CONTEXT" -n monitoring get secret live-lab-grafana-admin --ignore-not-found -o name)"
if [[ -n "$grafana_secret_name" ]]; then
  grafana_secret_json="$(kubectl --context "$KUBE_CONTEXT" -n monitoring get secret live-lab-grafana-admin -o json)"
  python3 - "$grafana_secret_json" platform/live-lab/evidence/grafana-admin-password.txt <<'PY'
import base64
import json
import os
import sys

secret = json.loads(sys.argv[1])
labels = secret.get("metadata", {}).get("labels", {})
if labels.get("live-lab-session") != os.environ["SESSION_ID"] or labels.get("live-lab-approval") != os.environ["APPROVAL_ID"]:
    raise SystemExit("BLOCKED: existing Grafana Secret belongs to another session")
password = base64.b64decode(secret.get("data", {}).get("admin-password", "")).decode()
if len(password) < 32:
    raise SystemExit("BLOCKED: existing Grafana Secret password is invalid")
path = sys.argv[2]
if os.path.exists(path):
    with open(path, encoding="utf-8") as handle:
        if handle.read().strip() != password:
            raise SystemExit("BLOCKED: local Grafana password evidence does not match the session Secret")
else:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(password + "\n")
PY
else
  export LIVE_LAB_GRAFANA_PASS="$(openssl rand -hex 24)"
  python3 - <<'PY' | kubectl --context "$KUBE_CONTEXT" create -f - >/dev/null
import base64
import json
import os

def b64(value):
    return base64.b64encode(value.encode()).decode()

print(json.dumps({
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {
        "name": "live-lab-grafana-admin",
        "namespace": "monitoring",
        "labels": {
            "app.kubernetes.io/managed-by": "local-live-lab",
            "live-lab-project": "kyobo-platform-live-lab",
            "live-lab-session": os.environ["SESSION_ID"],
            "live-lab-approval": os.environ["APPROVAL_ID"],
        },
    },
    "type": "Opaque",
    "data": {"admin-user": b64("admin"), "admin-password": b64(os.environ["LIVE_LAB_GRAFANA_PASS"])},
}))
PY
  printf '%s\n' "$LIVE_LAB_GRAFANA_PASS" > platform/live-lab/evidence/grafana-admin-password.txt
  chmod 600 platform/live-lab/evidence/grafana-admin-password.txt
  unset LIVE_LAB_GRAFANA_PASS
fi

helm --repository-config "$helm_repo_config" --repository-cache "$helm_repo_cache" repo add eks https://aws.github.io/eks-charts >/dev/null
helm --repository-config "$helm_repo_config" --repository-cache "$helm_repo_cache" repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm --repository-config "$helm_repo_config" --repository-cache "$helm_repo_cache" repo update >/dev/null
helm --kube-context "$KUBE_CONTEXT" --repository-config "$helm_repo_config" --repository-cache "$helm_repo_cache" upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --version 3.5.0 --namespace kube-system --set-string "clusterName=$CLUSTER_NAME" \
  --set-string "region=$AWS_REGION" --set-string "vpcId=$vpc_id" \
  --set enableShield=false --set enableWaf=false --set enableWafv2=true \
  --set serviceAccount.create=false --set serviceAccount.name=aws-load-balancer-controller \
  --wait --timeout 5m
helm --kube-context "$KUBE_CONTEXT" --repository-config "$helm_repo_config" --repository-cache "$helm_repo_cache" upgrade --install live-lab-observability prometheus-community/kube-prometheus-stack \
  --version 91.4.0 --namespace monitoring --create-namespace \
  --values platform/live-lab/manifests/bootstrap/kube-prometheus-stack-values.yaml \
  --wait --timeout 8m
kubectl --context "$KUBE_CONTEXT" -n kube-system rollout status deployment/aws-load-balancer-controller --timeout=180s
kubectl --context "$KUBE_CONTEXT" -n monitoring rollout status deployment/live-lab-observability-grafana --timeout=180s
kubectl --context "$KUBE_CONTEXT" -n monitoring get prometheus live-lab-observability-prometheus >/dev/null
echo "Bootstrap installed pinned metrics-server, Argo Rollouts, AWS Load Balancer Controller, and bounded Prometheus/Grafana."
echo "Grafana admin password is stored with mode 0600 at platform/live-lab/evidence/grafana-admin-password.txt."
