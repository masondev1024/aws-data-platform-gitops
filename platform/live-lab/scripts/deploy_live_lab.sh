#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
: "${AWS_PROFILE:?AWS_PROFILE must name the approved SSO profile}"
: "${AWS_REGION:?AWS_REGION must be explicitly set to ap-northeast-2}"
: "${EXPECTED_ACCOUNT_ID:?EXPECTED_ACCOUNT_ID must be the approved 12-digit account ID}"
: "${SESSION_ID:?SESSION_ID must be the Terraform session_id}"
: "${APPROVAL_ID:?APPROVAL_ID must be the recorded approval_id}"
: "${KUBE_CONTEXT:?KUBE_CONTEXT must point to the exact session EKS cluster}"

project="kyobo-platform-live-lab"
cluster_name="kyobo-${SESSION_ID}"
namespace="${NAMESPACE:-platform-validation}"
terraform_dir="platform/live-lab/terraform"
evidence_dir="platform/live-lab/evidence"
bootstrap_manifest="$evidence_dir/rendered-bootstrap-manifests.yaml"
app_manifest="$evidence_dir/rendered-app-manifests.yaml"

[[ "$AWS_REGION" == ap-northeast-2 && "$EXPECTED_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || { echo "BLOCKED: account/region do not match the approved scope." >&2; exit 2; }
[[ "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ && "$APPROVAL_ID" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]] || { echo "BLOCKED: invalid session or approval ID." >&2; exit 2; }
for command in aws kubectl terraform python3; do
  command -v "$command" >/dev/null || { echo "BLOCKED: $command is required." >&2; exit 2; }
done
[[ -s "$bootstrap_manifest" && ! -L "$bootstrap_manifest" && -s "$app_manifest" && ! -L "$app_manifest" ]] || { echo "BLOCKED: protected runtime-rendered bootstrap/app manifests are required." >&2; exit 2; }

identity="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity --query Account --output text)"
[[ "$identity" == "$EXPECTED_ACCOUNT_ID" ]] || { echo "BLOCKED: AWS account mismatch." >&2; exit 2; }
cluster_json="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" eks describe-cluster --name "$cluster_name" --output json)"
read -r cluster_arn cluster_endpoint cluster_status project_tag session_tag approval_tag < <(python3 -c 'import json,sys; c=json.loads(sys.argv[1])["cluster"]; t=c.get("tags", {}); print(c.get("arn", ""), c.get("endpoint", ""), c.get("status", ""), t.get("Project", ""), t.get("Session", ""), t.get("Approval", ""))' "$cluster_json")
[[ "$cluster_arn" == "arn:aws:eks:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:cluster/${cluster_name}" && "$cluster_status" == ACTIVE && "$project_tag" == "$project" && "$session_tag" == "$SESSION_ID" && "$approval_tag" == "$APPROVAL_ID" ]] || { echo "BLOCKED: EKS identity/status/ownership tags do not match this session." >&2; exit 2; }
context_endpoint="$(kubectl --context "$KUBE_CONTEXT" config view --minify -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["clusters"][0]["cluster"]["server"])')"
[[ "$context_endpoint" == "$cluster_endpoint" ]] || { echo "BLOCKED: KUBE_CONTEXT endpoint does not match the approved EKS cluster." >&2; exit 2; }
kube() { kubectl --context "$KUBE_CONTEXT" "$@"; }

writer_host="$(terraform -chdir="$terraform_dir" output -raw db_writer_endpoint)"
reader_host="$(terraform -chdir="$terraform_dir" output -raw db_reader_endpoint)"
repository="$(terraform -chdir="$terraform_dir" output -raw ecr_repository_url)"
image_ref="$(< "$evidence_dir/image-ref.txt")"
[[ "$writer_host" =~ ^[A-Za-z0-9.-]+$ && "$reader_host" =~ ^[A-Za-z0-9.-]+$ ]] || { echo "BLOCKED: Terraform database endpoints are not hostnames." >&2; exit 2; }
[[ "$repository" == "${EXPECTED_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/kyobo-${SESSION_ID}/data-pipeline-app" && "$image_ref" == "$repository"@sha256:* ]] || { echo "BLOCKED: image digest is not pinned to this session's ECR repository." >&2; exit 2; }
chmod 600 "$bootstrap_manifest" "$app_manifest" "$evidence_dir/image-ref.txt"

python3 - "$app_manifest" "$repository" "$namespace" <<'PY'
import sys
import yaml

path, repository, namespace = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    documents = [item for item in yaml.safe_load_all(handle) if item]
jobs = [item for item in documents if item.get("kind") == "Job" and item.get("metadata", {}).get("name") == "data-pipeline-schema-migration"]
if len(jobs) != 1:
    raise SystemExit("BLOCKED: rendered app manifest must contain exactly one schema migration Job")
if any(item.get("kind") in {"Secret", "Namespace"} for item in documents):
    raise SystemExit("BLOCKED: runtime app manifest must not carry Secrets or namespace resources")
rollouts = [item for item in documents if item.get("kind") == "Rollout" and item.get("metadata", {}).get("name") == "data-pipeline-rollout"]
if len(rollouts) != 1:
    raise SystemExit("BLOCKED: expected exactly one application Rollout")
image = rollouts[0]["spec"]["template"]["spec"]["containers"][0]["image"]
if not image.startswith(repository + "@sha256:") or len(image.rsplit("@sha256:", 1)[1]) != 64:
    raise SystemExit("BLOCKED: Rollout image must use this session's immutable ECR digest")
for item in documents:
    object_namespace = item.get("metadata", {}).get("namespace")
    if object_namespace not in {None, namespace}:
        raise SystemExit("BLOCKED: rendered manifest contains an object in a different namespace")
PY

echo "Applying bootstrap namespace and database endpoint ConfigMap to exact cluster context."
kube apply --filename "$bootstrap_manifest"
AWS_PROFILE="$AWS_PROFILE" AWS_REGION="$AWS_REGION" EXPECTED_ACCOUNT_ID="$EXPECTED_ACCOUNT_ID" \
  SESSION_ID="$SESSION_ID" APPROVAL_ID="$APPROVAL_ID" KUBE_CONTEXT="$KUBE_CONTEXT" NAMESPACE="$namespace" \
  platform/live-lab/scripts/prepare_rds_ca_bundle.sh
kube get namespace "$namespace" --output name >/dev/null
app_secret="$(kube --namespace "$namespace" get secret raffle-secret --ignore-not-found --output name)"
migration_secret="$(kube --namespace "$namespace" get secret raffle-migration-secret --ignore-not-found --output name)"
if [[ -z "$app_secret" && -z "$migration_secret" ]]; then
  AWS_PROFILE="$AWS_PROFILE" AWS_REGION="$AWS_REGION" EXPECTED_ACCOUNT_ID="$EXPECTED_ACCOUNT_ID" \
    SESSION_ID="$SESSION_ID" APPROVAL_ID="$APPROVAL_ID" KUBE_CONTEXT="$KUBE_CONTEXT" NAMESPACE="$namespace" \
    platform/live-lab/scripts/create_runtime_k8s_secret.sh
elif [[ -n "$app_secret" && -n "$migration_secret" ]]; then
  for secret_name in raffle-secret raffle-migration-secret; do
    secret_json="$(kube --namespace "$namespace" get secret "$secret_name" -o json)"
    python3 - "$secret_json" "$SESSION_ID" "$APPROVAL_ID" <<'PY'
import json
import sys

secret = json.loads(sys.argv[1])
labels = secret.get("metadata", {}).get("labels", {})
if labels.get("live-lab-session") != sys.argv[2] or labels.get("live-lab-approval") != sys.argv[3]:
    raise SystemExit("BLOCKED: existing runtime Secret is not owned by the exact approved session")
PY
  done
else
  echo "BLOCKED: only one runtime Secret exists; preserve state and recover the incomplete credential install manually." >&2
  exit 2
fi

existing_job="$(kube --namespace "$namespace" get job data-pipeline-schema-migration --ignore-not-found --output json)"
if [[ -n "$existing_job" ]]; then
  migration_job_state="$(python3 - "$existing_job" "$SESSION_ID" "$APPROVAL_ID" <<'PY'
import json
import sys

job = json.loads(sys.argv[1])
labels = job.get("metadata", {}).get("labels", {})
if labels.get("live-lab-session") != sys.argv[2] or labels.get("live-lab-approval") != sys.argv[3]:
    raise SystemExit("BLOCKED: existing migration Job does not belong to this approved session")
succeeded = job.get("status", {}).get("succeeded", 0)
failed = job.get("status", {}).get("failed", 0)
active = job.get("status", {}).get("active", 0)
if succeeded == 1:
    print("succeeded")
elif failed > 0 and active == 0:
    print("failed")
else:
    raise SystemExit("BLOCKED: an existing migration Job is still active or has an unknown state")
PY
  )"
  if [[ "$migration_job_state" == succeeded ]]; then
    echo "Verified the session-owned migration Job is already complete; not recreating it."
  else
    echo "Removing the failed session-owned migration Job and retrying its idempotent schema migration."
    kube --namespace "$namespace" delete job data-pipeline-schema-migration --wait=true
    existing_job=""
  fi
fi
if [[ -z "$existing_job" ]]; then
  python3 - "$app_manifest" <<'PY' | kube --namespace "$namespace" apply --filename - >/dev/null
import json
import sys
import yaml

with open(sys.argv[1], encoding="utf-8") as handle:
    jobs = [item for item in yaml.safe_load_all(handle) if item and item.get("kind") == "Job" and item.get("metadata", {}).get("name") == "data-pipeline-schema-migration"]
if len(jobs) != 1:
    raise SystemExit("BLOCKED: migration Job count changed after manifest verification")
print(json.dumps(jobs[0]))
PY
  kube --namespace "$namespace" wait --for=condition=complete job/data-pipeline-schema-migration --timeout=300s
fi

echo "Applying application, services, monitors, canary analysis, HPA, Ingress, and scheduled draw job."
python3 - "$app_manifest" <<'PY' | kube --namespace "$namespace" apply --filename -
import sys
import yaml

with open(sys.argv[1], encoding="utf-8") as handle:
    documents = [
        item for item in yaml.safe_load_all(handle)
        if item and not (item.get("kind") == "Job" and item.get("metadata", {}).get("name") == "data-pipeline-schema-migration")
    ]
if not documents:
    raise SystemExit("BLOCKED: no application resources remain after excluding the already-completed migration Job")
print("---\n".join(yaml.safe_dump(item, sort_keys=False) for item in documents))
PY
kube --namespace "$namespace" wait --for=condition=Available rollout/data-pipeline-rollout --timeout=600s
kube --namespace "$namespace" wait --for=jsonpath='{.status.loadBalancer.ingress[0].hostname}' ingress/data-pipeline-ingress --timeout=600s
alb_dns="$(kube --namespace "$namespace" get ingress data-pipeline-ingress -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
[[ "$alb_dns" == *.ap-northeast-2.elb.amazonaws.com ]] || { echo "BLOCKED: session ALB DNS did not resolve to the expected Seoul ELB domain." >&2; exit 2; }
printf '%s\n' "$alb_dns" > "$evidence_dir/alb-dns.txt"
chmod 600 "$evidence_dir/alb-dns.txt"
waf_arn="$(terraform -chdir="$terraform_dir" output -raw waf_web_acl_arn)"
python3 platform/live-lab/scripts/associate_waf_live.py \
  --account "$EXPECTED_ACCOUNT_ID" --region "$AWS_REGION" --session "$SESSION_ID" \
  --approval "$APPROVAL_ID" --cluster "$cluster_name" --alb-dns "$alb_dns" \
  --web-acl-arn "$waf_arn" --profile "$AWS_PROFILE" \
  --evidence "$evidence_dir/waf-association.json"
read -r alb_metric_name alb_metric_id < <(python3 - "$evidence_dir/waf-association.json" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
if report.get("status") != "verified":
    raise SystemExit("BLOCKED: verified ALB association evidence is required before configuring canary metrics")
name = report.get("alb_cloudwatch_load_balancer_name", "")
resource_id = report.get("alb_cloudwatch_load_balancer_id", "")
dimension = report.get("alb_cloudwatch_load_balancer_dimension", "")
if not re.fullmatch(r"[A-Za-z0-9-]{1,32}", name):
    raise SystemExit("BLOCKED: invalid session ALB CloudWatch name")
if not re.fullmatch(r"[a-f0-9]{16,32}", resource_id):
    raise SystemExit("BLOCKED: invalid session ALB CloudWatch resource ID")
if dimension != f"app/{name}/{resource_id}":
    raise SystemExit("BLOCKED: ALB CloudWatch dimension does not match the verified ARN")
print(name, resource_id)
PY
)
[[ "$alb_metric_name" =~ ^[A-Za-z0-9-]{1,32}$ && "$alb_metric_id" =~ ^[a-f0-9]{16,32}$ ]] || {
  echo "BLOCKED: verified ALB CloudWatch identifiers are invalid." >&2
  exit 2
}
rollout_labels="$(python3 - "$alb_metric_name" "$alb_metric_id" <<'PY'
import json
import sys

print(json.dumps({"metadata": {"labels": {
    "live-lab.aws/alb-name": sys.argv[1],
    "live-lab.aws/alb-id": sys.argv[2],
}}}))
PY
)"
kube --namespace "$namespace" patch rollout data-pipeline-rollout --type=merge --patch "$rollout_labels" >/dev/null
echo "Deployment is Available; scoped WAF association is verified. ALB DNS is at $evidence_dir/alb-dns.txt."
