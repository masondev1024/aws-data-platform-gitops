#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
: "${AWS_PROFILE:?AWS_PROFILE is required}"
: "${AWS_REGION:?AWS_REGION must be ap-northeast-2}"
: "${EXPECTED_ACCOUNT_ID:?EXPECTED_ACCOUNT_ID is required}"
: "${SESSION_ID:?SESSION_ID is required}"
: "${APPROVAL_ID:?APPROVAL_ID is required}"
: "${KUBE_CONTEXT:?KUBE_CONTEXT must point to this session EKS cluster}"
: "${LIVE_LAB_TFVARS:?LIVE_LAB_TFVARS must be the protected session tfvars file}"

PROJECT="kyobo-platform-live-lab"
CLUSTER_NAME="kyobo-${SESSION_ID}"
TERRAFORM_DIR="platform/live-lab/terraform"
EVIDENCE_DIR="platform/live-lab/evidence"
STATUS_FILE="$EVIDENCE_DIR/teardown-status.json"
EXPECTED_CONFIRM="destroy-${SESSION_ID}-${AWS_REGION}"
tmp_dir=""
finish_status() { python3 platform/live-lab/scripts/live_lab_lifecycle.py write-status "$STATUS_FILE" "$1" "$2" --session "$SESSION_ID" --region "$AWS_REGION"; }
on_error() {
  code=$?
  if (( code != 0 )); then
    finish_status incomplete "teardown stopped with exit $code; inspect residual inventory and preserved session evidence" || true
    echo "INCOMPLETE: cleanup stopped safely; evidence is at $STATUS_FILE" >&2
  fi
  [[ -z "$tmp_dir" ]] || rm -rf -- "$tmp_dir"
}

[[ "$AWS_REGION" == ap-northeast-2 && "$EXPECTED_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || { echo "BLOCKED: account/region mismatch." >&2; exit 2; }
[[ "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ && "$APPROVAL_ID" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]] || { echo "BLOCKED: invalid session/approval ID." >&2; exit 2; }
[[ "${TEARDOWN_CONFIRM:-}" == "$EXPECTED_CONFIRM" ]] || { echo "BLOCKED: set TEARDOWN_CONFIRM=$EXPECTED_CONFIRM to delete only this session." >&2; exit 2; }
[[ -f "$LIVE_LAB_TFVARS" && ! -L "$LIVE_LAB_TFVARS" ]] || { echo "BLOCKED: tfvars file missing or symlinked." >&2; exit 2; }
tfvars_path="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$LIVE_LAB_TFVARS")"
[[ "$tfvars_path" == "$repo_root/$EVIDENCE_DIR/"* ]] || { echo "BLOCKED: tfvars must stay in the session evidence directory." >&2; exit 2; }
tfvars_mode="$(stat -f '%Lp' "$tfvars_path" 2>/dev/null || stat -c '%a' "$tfvars_path")"
[[ "$tfvars_mode" == 600 ]] || { echo "BLOCKED: tfvars mode must be 0600." >&2; exit 2; }

identity="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity --query Account --output text)"
[[ "$identity" == "$EXPECTED_ACCOUNT_ID" ]] || { echo "BLOCKED: AWS identity account mismatch." >&2; exit 2; }
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/live-lab-teardown.XXXXXX")"
trap on_error EXIT

if aws --profile "$AWS_PROFILE" --region "$AWS_REGION" eks describe-cluster --name "$CLUSTER_NAME" --output json > "$tmp_dir/cluster.json" 2> "$tmp_dir/cluster.err"; then
  read -r cluster_arn cluster_endpoint cluster_status project_tag session_tag approval_tag < <(python3 -c 'import json,sys; c=json.load(open(sys.argv[1]))["cluster"]; t=c.get("tags", {}); print(c.get("arn", ""), c.get("endpoint", ""), c.get("status", ""), t.get("Project", ""), t.get("Session", ""), t.get("Approval", ""))' "$tmp_dir/cluster.json")
  [[ "$cluster_arn" == "arn:aws:eks:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:cluster/${CLUSTER_NAME}" && "$project_tag" == "$PROJECT" && "$session_tag" == "$SESSION_ID" && "$approval_tag" == "$APPROVAL_ID" ]] || { echo "BLOCKED: EKS ownership identity mismatch." >&2; exit 2; }
  [[ "$cluster_status" == ACTIVE ]] || { echo "BLOCKED: EKS must be ACTIVE before controller-dependent cleanup; observed $cluster_status." >&2; exit 2; }
  context_endpoint="$(kubectl --context "$KUBE_CONTEXT" config view --minify -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["clusters"][0]["cluster"]["server"])')"
  [[ "$context_endpoint" == "$cluster_endpoint" ]] || { echo "BLOCKED: Kubernetes context endpoint is not the exact session cluster." >&2; exit 2; }
  # Stop GitOps reconciliation before deleting its destination namespace.
  # Older direct-deployment sessions may not have the Application CRD at all.
  application_crd="$(kubectl --context "$KUBE_CONTEXT" get crd applications.argoproj.io --ignore-not-found -o name)"
  if [[ -n "$application_crd" ]]; then
    application_json="$(kubectl --context "$KUBE_CONTEXT" -n argocd get application data-pipeline-validation --ignore-not-found -o json)"
    if [[ -n "$application_json" ]]; then
      python3 - "$application_json" "$SESSION_ID" "$APPROVAL_ID" <<'PY'
import json
import sys
application = json.loads(sys.argv[1])
labels = application.get("metadata", {}).get("labels", {})
if labels.get("live-lab-session") != sys.argv[2] or labels.get("live-lab-approval") != sys.argv[3]:
    raise SystemExit("BLOCKED: refusing to delete an Application from another session")
if application.get("spec", {}).get("destination", {}).get("namespace") != "platform-validation":
    raise SystemExit("BLOCKED: Application destination is outside the session namespace")
spec = application.get("spec", {})
if (spec.get("destination", {}).get("server") != "https://kubernetes.default.svc"
        or spec.get("project") != "kyobo-platform-validation"
        or spec.get("source", {}).get("repoURL") != "https://github.com/masondev1024/aws-data-platform-gitops"
        or spec.get("source", {}).get("path") != "k8s/overlays/validation"
        or spec.get("sources") or application.get("metadata", {}).get("finalizers")):
    raise SystemExit("BLOCKED: unexpected Application scope/finalizers; do not cascade delete")
PY
      kubectl --context "$KUBE_CONTEXT" -n argocd delete application data-pipeline-validation --wait=true --timeout=120s
    fi
  fi
  kubectl --context "$KUBE_CONTEXT" delete namespace platform-validation --ignore-not-found --wait=true --timeout=300s
  lb_deadline=$(( $(date +%s) + 600 ))
  while (( $(date +%s) < lb_deadline )); do
    aws --profile "$AWS_PROFILE" --region "$AWS_REGION" resourcegroupstaggingapi get-resources \
      --tag-filters "Key=Project,Values=$PROJECT" "Key=Session,Values=$SESSION_ID" "Key=Approval,Values=$APPROVAL_ID" \
      --resource-type-filters elasticloadbalancing:loadbalancer --output json > "$tmp_dir/load-balancers.json"
    lb_count="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("ResourceTagMappingList", [])))' "$tmp_dir/load-balancers.json")"
    (( lb_count == 0 )) && break
    echo "Waiting for the in-cluster controller to remove session load balancers ($lb_count remain)." >&2
    sleep 10
  done
  (( lb_count == 0 )) || { echo "BLOCKED: session ALB remains; preserving EKS for controller cleanup." >&2; exit 2; }
  for release in live-lab-observability aws-load-balancer-controller; do
    release_namespace=monitoring
    [[ "$release" == aws-load-balancer-controller ]] && release_namespace=kube-system
    if [[ "$(helm --kube-context "$KUBE_CONTEXT" -n "$release_namespace" list --short --filter "^${release}$")" == "$release" ]]; then
      helm --kube-context "$KUBE_CONTEXT" -n "$release_namespace" uninstall "$release" --wait --timeout 300s
    fi
  done
  kubectl --context "$KUBE_CONTEXT" delete namespace monitoring argo-rollouts --ignore-not-found --wait=true --timeout=300s
elif ! grep -q ResourceNotFoundException "$tmp_dir/cluster.err"; then
  cat "$tmp_dir/cluster.err" >&2
  echo "BLOCKED: unable to prove the session EKS cluster absent." >&2
  exit 2
fi

tfstate="$repo_root/$TERRAFORM_DIR/terraform.tfstate"
if [[ -s "$tfstate" ]]; then
  managed_state="$(terraform -chdir="$TERRAFORM_DIR" state list)"
  if [[ -n "$managed_state" ]]; then
    [[ "$(terraform -chdir="$TERRAFORM_DIR" workspace show)" == default ]] || { echo "BLOCKED: unexpected Terraform workspace." >&2; exit 2; }
    terraform -chdir="$TERRAFORM_DIR" show -json > "$tmp_dir/state.json"
    python3 platform/live-lab/scripts/live_lab_lifecycle.py validate-state "$tmp_dir/state.json" --session "$SESSION_ID" --approval "$APPROVAL_ID"
    terraform -chdir="$TERRAFORM_DIR" plan -destroy -input=false -var-file="$tfvars_path" \
      -var='apply_approval_phrase=APPROVED_FOR_EPHEMERAL_APPLY' -out="$repo_root/$EVIDENCE_DIR/teardown.tfplan"
    chmod 600 "$repo_root/$EVIDENCE_DIR/teardown.tfplan"
    terraform -chdir="$TERRAFORM_DIR" apply -input=false -auto-approve "$repo_root/$EVIDENCE_DIR/teardown.tfplan"
    [[ -z "$(terraform -chdir="$TERRAFORM_DIR" state list)" ]] || { echo "BLOCKED: Terraform state still contains resources." >&2; exit 2; }
  else
    echo "Terraform state is already empty; skip destroy plan."
  fi
elif [[ -e "$repo_root/$TERRAFORM_DIR/terraform.tfstate.backup" ]]; then
  echo "BLOCKED: only a Terraform state backup remains; inspect/recover it before deletion." >&2
  exit 2
fi

secret_name="${DB_MASTER_PASSWORD_SECRET_ID:-kyobo-live-lab/${SESSION_ID}/mysql-master-password}"
if aws --profile "$AWS_PROFILE" --region "$AWS_REGION" secretsmanager describe-secret --secret-id "$secret_name" > "$tmp_dir/secret.json" 2> "$tmp_dir/secret.err"; then
  python3 platform/live-lab/scripts/live_lab_lifecycle.py validate-tags "$tmp_dir/secret.json" --project "$PROJECT" --session "$SESSION_ID" --approval "$APPROVAL_ID"
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" secretsmanager delete-secret --secret-id "$secret_name" --force-delete-without-recovery >/dev/null
elif ! grep -q ResourceNotFoundException "$tmp_dir/secret.err"; then
  cat "$tmp_dir/secret.err" >&2
  echo "BLOCKED: cannot prove session Secrets Manager entry is absent." >&2
  exit 2
fi

aws --profile "$AWS_PROFILE" --region "$AWS_REGION" resourcegroupstaggingapi get-resources \
  --tag-filters "Key=Project,Values=$PROJECT" "Key=Session,Values=$SESSION_ID" "Key=Approval,Values=$APPROVAL_ID" \
  --resource-type-filters acm:certificate --output json > "$tmp_dir/certificates.json"
python3 -c 'import json,sys; print("\n".join(item["ResourceARN"] for item in json.load(open(sys.argv[1])).get("ResourceTagMappingList", [])))' "$tmp_dir/certificates.json" > "$tmp_dir/certificate-arns.txt"
while IFS= read -r cert_arn; do
  [[ -n "$cert_arn" ]] || continue
  [[ "$cert_arn" == "arn:aws:acm:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:certificate/"* ]] || { echo "BLOCKED: tagged certificate ARN is outside the approved account/region." >&2; exit 2; }
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" acm delete-certificate --certificate-arn "$cert_arn"
done < "$tmp_dir/certificate-arns.txt"

aws --profile "$AWS_PROFILE" --region "$AWS_REGION" resourcegroupstaggingapi get-resources \
  --tag-filters "Key=Project,Values=$PROJECT" "Key=Session,Values=$SESSION_ID" "Key=Approval,Values=$APPROVAL_ID" \
  --resources-per-page 100 --output json > "$tmp_dir/residuals.json"
python3 platform/live-lab/scripts/live_lab_lifecycle.py reconcile-inventory \
  "$tmp_dir/residuals.json" "$EVIDENCE_DIR/residual-inventory.json" \
  --project "$PROJECT" --session "$SESSION_ID" --approval "$APPROVAL_ID" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --account-id "$EXPECTED_ACCOUNT_ID"
finish_status completed "Terraform state is empty and every tagged inventory ARN was individually rechecked; no live session-tagged resources were observed. Stale tag-index entries are recorded separately. This is not proof about untagged resources or delayed billing."
trap - EXIT
rm -rf "$tmp_dir"
echo "Teardown complete for session $SESSION_ID. Inventory is at $EVIDENCE_DIR/residual-inventory.json."
