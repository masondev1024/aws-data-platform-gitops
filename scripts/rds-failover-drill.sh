#!/usr/bin/env bash
set -Eeuo pipefail
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

if [[ "${NAMESPACE:-platform-validation}" != "platform-validation" ]]; then
  printf 'NAMESPACE is fixed to platform-validation; refusing override\n' >&2
  exit 2
fi
NAMESPACE="platform-validation"
SESSION_ID="${SESSION_ID:?SESSION_ID is required; use the bare Terraform session_id tag value}"
APPROVAL_ID="${APPROVAL_ID:?APPROVAL_ID is required; use the exact approved Terraform approval_id}"
RESOURCE_PREFIX="${RESOURCE_PREFIX:-kyobo-${SESSION_ID}}"
AWS_PROFILE="${AWS_PROFILE:?AWS_PROFILE is required; ambient AWS defaults are not accepted}"
AWS_REGION="${AWS_REGION:?AWS_REGION is required}"
EXPECTED_AWS_ACCOUNT_ID="${EXPECTED_AWS_ACCOUNT_ID:?EXPECTED_AWS_ACCOUNT_ID is required}"
KUBE_CONTEXT="${KUBE_CONTEXT:?KUBE_CONTEXT is required; default context is never used}"
BASE_URL="${BASE_URL:?BASE_URL is required for the Multi-AZ RTO /readyz probe}"
ALLOW_SELF_SIGNED_ALB_TLS="${ALLOW_SELF_SIGNED_ALB_TLS:-false}"
RUN_ID="${RUN_ID:-rds-recovery-$(date -u +%Y%m%dT%H%M%SZ)}"
SYNTHETIC_USERNAME="${SYNTHETIC_USERNAME:-rds-drill-${RUN_ID}}"
REPLICA_LAG_THRESHOLD_SECONDS="${REPLICA_LAG_THRESHOLD_SECONDS:?REPLICA_LAG_THRESHOLD_SECONDS is required}"
DRILL_MODE="${DRILL_MODE:-plan}"
ENABLE_REPLICA_PROMOTION="${ENABLE_REPLICA_PROMOTION:-false}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-5}"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-900}"
QUIESCE_TIMEOUT_SECONDS="${QUIESCE_TIMEOUT_SECONDS:-180}"
REPLICA_MARKER_TIMEOUT_SECONDS="${REPLICA_MARKER_TIMEOUT_SECONDS:-300}"
REPLICA_MARKER_POLL_SECONDS="${REPLICA_MARKER_POLL_SECONDS:-5}"
OUTPUT_DIR="${OUTPUT_DIR:-evidence/rds-recovery-drill-${RUN_ID}}"

WRITER_ID="${WRITER_ID:-${RESOURCE_PREFIX}-mysql-primary}"
READER_ID="${READER_ID:-${RESOURCE_PREFIX}-mysql-reader}"
ROLLOUT_NAME="${ROLLOUT_NAME:-data-pipeline-rollout}"
APP_CONTAINER_NAME="${APP_CONTAINER_NAME:-app-container}"
APP_LABEL_SELECTOR="${APP_LABEL_SELECTOR:-app=data-pipeline-app}"
CRONJOB_NAME="${CRONJOB_NAME:-raffle-draw-job}"
CONFIGMAP_NAME="${CONFIGMAP_NAME:-raffle-config}"
SECRET_NAME="${SECRET_NAME:-raffle-secret}"
CA_CONFIGMAP_NAME="${CA_CONFIGMAP_NAME:-rds-ca-bundle}"
CA_PATH="${CA_PATH:-/etc/rds-ca/global-bundle.pem}"
HELPER_SOURCE="${HELPER_SOURCE:-app/replica_drill.py}"
FENCE_OUTPUT_NAME="${FENCE_OUTPUT_NAME:-db_fence_security_group_id}"
TERRAFORM_DIR="${TERRAFORM_DIR:-platform/live-lab/terraform}"
PROMOTE_REPLICA_CONFIRM_EXPECTED="promote-${READER_ID}-for-${RUN_ID}"
PROMOTE_REPLICA_CONFIRM="${PROMOTE_REPLICA_CONFIRM:-}"
EXECUTE_CONFIRM_EXPECTED="execute-rds-recovery-drill-${SESSION_ID}-${AWS_REGION}"
EXECUTE_CONFIRM="${EXECUTE_CONFIRM:-}"

RUN_SLUG="$(python3 - "$RUN_ID" <<'PY'
import re
import sys
slug = re.sub(r"[^a-z0-9-]+", "-", sys.argv[1].lower()).strip("-")
print((slug or "run")[:36])
PY
)"
HELPER_SCOPE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
HELPER_NAME_SLUG="${RUN_SLUG:0:24}"
HELPER_CONFIGMAP_NAME="rds-drill-helper-${HELPER_NAME_SLUG}-${HELPER_SCOPE_TOKEN}"

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
CHECKPOINT_FILE="$OUTPUT_DIR/checkpoint.env"
EVENTS_FILE="$OUTPUT_DIR/events.jsonl"
PROBES_FILE="$OUTPUT_DIR/rto-probes.csv"
RECOVERY_FILE="$OUTPUT_DIR/recovery-instructions.txt"

PROMOTION_STARTED=0
ORIGINAL_REPLICAS=""
ORIGINAL_CRON_SUSPEND=""
ORIGINAL_WRITER_ENDPOINT=""
ORIGINAL_WRITER_SECURITY_GROUPS=""
PROMOTED_WRITER_ENDPOINT=""
CURL_TLS_ARGS=()
APP_IMAGE=""
BASE_HOST=""
BASE_SCHEME=""

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

event() {
  local phase="$1"
  local status="$2"
  local detail="${3:-}"
  python3 - "$EVENTS_FILE" "$phase" "$status" "$detail" <<'PY'
import json
import sys
from datetime import datetime, timezone

path, phase, status, detail = sys.argv[1:5]
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "phase": phase,
        "status": status,
        "detail": detail,
    }, sort_keys=True) + "\n")
PY
}

save_checkpoint() {
  {
    printf 'RUN_ID=%q\n' "$RUN_ID"
    printf 'SESSION_ID=%q\n' "$SESSION_ID"
    printf 'RESOURCE_PREFIX=%q\n' "$RESOURCE_PREFIX"
    printf 'AWS_PROFILE=%q\n' "$AWS_PROFILE"
    printf 'AWS_REGION=%q\n' "$AWS_REGION"
    printf 'KUBE_CONTEXT=%q\n' "$KUBE_CONTEXT"
    printf 'NAMESPACE=%q\n' "$NAMESPACE"
    printf 'WRITER_ID=%q\n' "$WRITER_ID"
    printf 'READER_ID=%q\n' "$READER_ID"
    printf 'ORIGINAL_REPLICAS=%q\n' "$ORIGINAL_REPLICAS"
    printf 'ORIGINAL_CRON_SUSPEND=%q\n' "$ORIGINAL_CRON_SUSPEND"
    printf 'ORIGINAL_WRITER_ENDPOINT=%q\n' "$ORIGINAL_WRITER_ENDPOINT"
    printf 'ORIGINAL_WRITER_SECURITY_GROUPS=%q\n' "$ORIGINAL_WRITER_SECURITY_GROUPS"
    printf 'PROMOTION_STARTED=%q\n' "$PROMOTION_STARTED"
    printf 'PROMOTED_WRITER_ENDPOINT=%q\n' "$PROMOTED_WRITER_ENDPOINT"
    printf 'CHECKPOINT_FILE=%q\n' "$CHECKPOINT_FILE"
  } > "$CHECKPOINT_FILE"
}

write_recovery_instructions() {
  {
    echo "RDS recovery drill checkpoint: $RUN_ID"
    echo "Evidence directory: $OUTPUT_DIR"
    echo "Do not resume writers until marker, entry, and outbox parity are verified on the promoted writer."
    echo
    echo "If promotion was not started:"
    echo "- Confirm ${ROLLOUT_NAME} replicas and ${CRONJOB_NAME} suspend state from $CHECKPOINT_FILE."
    echo "- Restore only these namespace-scoped resources if they differ:"
    echo "  kubectl --context $KUBE_CONTEXT -n $NAMESPACE patch cronjob $CRONJOB_NAME --type merge -p '{\"spec\":{\"suspend\":${ORIGINAL_CRON_SUSPEND:-false}}}'"
    echo "  kubectl --context $KUBE_CONTEXT -n $NAMESPACE patch rollout $ROLLOUT_NAME --type merge -p '{\"spec\":{\"replicas\":${ORIGINAL_REPLICAS:-2}}}'"
    echo
    echo "If promotion was started:"
    echo "- Keep ${CRONJOB_NAME} suspended and ${ROLLOUT_NAME} at zero until parity is proven."
    echo "- Verify the promoted endpoint: ${PROMOTED_WRITER_ENDPOINT:-unknown}."
    echo "- Verify marker parity with app/replica_drill.py verify-marker for run_id=$RUN_ID username=$SYNTHETIC_USERNAME."
    echo "- Reattach only scoped security groups after old-writer fencing has been reviewed."
  } > "$RECOVERY_FILE"
}

fail_closed() {
  local message="$1"
  event "fail-closed" "failed" "$message"
  save_checkpoint
  write_recovery_instructions
  echo "$message" >&2
  echo "Checkpoint preserved at $CHECKPOINT_FILE" >&2
  echo "Recovery instructions written to $RECOVERY_FILE" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  trap - EXIT ERR
  set +e
  if (( exit_code != 0 )); then
    if [[ "$DRILL_MODE" == "execute" && -n "$ORIGINAL_REPLICAS" ]]; then
      if [[ "$PROMOTION_STARTED" == "1" ]]; then
        kube patch cronjob "$CRONJOB_NAME" --type merge -p '{"spec":{"suspend":true}}' >/dev/null 2>&1 || true
        kube patch rollout "$ROLLOUT_NAME" --type merge -p '{"spec":{"replicas":0}}' >/dev/null 2>&1 || true
        echo "Post-promotion failure: workloads left quiesced for manual recovery." >&2
      else
        kube patch cronjob "$CRONJOB_NAME" --type merge -p "{\"spec\":{\"suspend\":${ORIGINAL_CRON_SUSPEND:-false}}}" >/dev/null 2>&1 || true
        kube patch rollout "$ROLLOUT_NAME" --type merge -p "{\"spec\":{\"replicas\":${ORIGINAL_REPLICAS}}}" >/dev/null 2>&1 || true
        echo "Pre-promotion failure: attempted to restore original writer workload state." >&2
      fi
    fi
    save_checkpoint
    write_recovery_instructions
    echo "Drill failed closed. Helper ConfigMap/Jobs are preserved for inspection." >&2
    echo "Checkpoint: $CHECKPOINT_FILE" >&2
    echo "Recovery instructions: $RECOVERY_FILE" >&2
  fi
}
trap on_error EXIT

require_tool() {
  command -v "$1" >/dev/null || fail_closed "$1 is required"
}

aws_cmd() {
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" "$@"
}

kube() {
  kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" "$@"
}

run_mutating() {
  if [[ "$DRILL_MODE" != "execute" ]]; then
    log "[plan] $*"
    return 0
  fi
  "$@"
}

require_execute_confirmation() {
  if [[ "$DRILL_MODE" != "execute" ]]; then
    log "DRILL_MODE=$DRILL_MODE; mutating commands will be printed, not executed"
    return
  fi
  [[ "$EXECUTE_CONFIRM" == "$EXECUTE_CONFIRM_EXPECTED" ]] || fail_closed "EXECUTE_CONFIRM must be exactly '$EXECUTE_CONFIRM_EXPECTED'"
}

terraform_output_value() {
  local name="$1"
  terraform -chdir="$TERRAFORM_DIR" output -raw "$name" 2>/dev/null || true
}

require_fence_output() {
  local fence_sg
  fence_sg="$(terraform_output_value "$FENCE_OUTPUT_NAME")"
  [[ -n "$fence_sg" && "$fence_sg" != "null" ]] || fail_closed "Terraform output '$FENCE_OUTPUT_NAME' is required before controlled replica promotion/fencing"
  [[ "$fence_sg" =~ ^sg-[0-9a-f]+$ ]] || fail_closed "Terraform output '$FENCE_OUTPUT_NAME' is not a security group id: $fence_sg"
  printf '%s' "$fence_sg"
}

validate_identifiers() {
  [[ "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ ]] || fail_closed "SESSION_ID must match Terraform session_id: lowercase letters, digits, and hyphens, 6-41 chars"
  [[ "$APPROVAL_ID" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]] || fail_closed "APPROVAL_ID must match the recorded approval_id"
  (( ${#APPROVAL_ID} <= 63 )) && [[ "$APPROVAL_ID" =~ [A-Za-z0-9]$ ]] || fail_closed "APPROVAL_ID must fit a Kubernetes label value"
  [[ "$AWS_REGION" == "ap-northeast-2" ]] || fail_closed "This recovery drill is approved only for ap-northeast-2"
  [[ "$RESOURCE_PREFIX" =~ ^[a-z0-9][a-z0-9-]{5,64}$ ]] || fail_closed "RESOURCE_PREFIX must be lowercase letters, digits, and hyphens"
  [[ "$RESOURCE_PREFIX" == "kyobo-${SESSION_ID}" ]] || fail_closed "RESOURCE_PREFIX must be exactly kyobo-${SESSION_ID} for this approved session"
  [[ "$RUN_ID" =~ ^[A-Za-z0-9_.:-]{6,64}$ ]] || fail_closed "RUN_ID must be 6-64 chars using letters, numbers, ., _, :, or -"
  [[ "$SYNTHETIC_USERNAME" =~ ^[A-Za-z0-9_.-]{3,50}$ ]] || fail_closed "SYNTHETIC_USERNAME must match the app username contract"
  [[ "$REPLICA_LAG_THRESHOLD_SECONDS" =~ ^[0-9]+$ ]] || fail_closed "REPLICA_LAG_THRESHOLD_SECONDS must be an integer"
  [[ "$REPLICA_MARKER_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] && (( REPLICA_MARKER_TIMEOUT_SECONDS >= 1 && REPLICA_MARKER_TIMEOUT_SECONDS <= 600 )) || fail_closed "REPLICA_MARKER_TIMEOUT_SECONDS must be 1-600"
  [[ "$REPLICA_MARKER_POLL_SECONDS" =~ ^[0-9]+$ ]] && (( REPLICA_MARKER_POLL_SECONDS >= 1 && REPLICA_MARKER_POLL_SECONDS <= 30 )) || fail_closed "REPLICA_MARKER_POLL_SECONDS must be 1-30"
  [[ "$WRITER_ID" == "${RESOURCE_PREFIX}-mysql-primary" ]] || fail_closed "WRITER_ID must be ${RESOURCE_PREFIX}-mysql-primary"
  [[ "$READER_ID" == "${RESOURCE_PREFIX}-mysql-reader" ]] || fail_closed "READER_ID must be ${RESOURCE_PREFIX}-mysql-reader"
  [[ "$ALLOW_SELF_SIGNED_ALB_TLS" == "true" || "$ALLOW_SELF_SIGNED_ALB_TLS" == "false" ]] || fail_closed "ALLOW_SELF_SIGNED_ALB_TLS must be true or false"
}

validate_base_url() {
  local parsed
  parsed="$(python3 - "$BASE_URL" <<'PY'
import sys
from urllib.parse import urlparse

url = urlparse(sys.argv[1])
if url.scheme not in {"http", "https"}:
    raise SystemExit("BASE_URL scheme must be http or https")
if not url.hostname or url.username or url.password:
    raise SystemExit("BASE_URL must include a host and must not include credentials")
if url.params or url.query or url.fragment:
    raise SystemExit("BASE_URL must not include params, query, or fragment")
if url.path not in {"", "/"}:
    raise SystemExit("BASE_URL must be an origin URL; /readyz is appended by the drill")
print(f"{url.scheme} {url.hostname}")
PY
)" || fail_closed "$parsed"
  read -r base_scheme base_host <<< "$parsed"
  BASE_SCHEME="$base_scheme"
  BASE_HOST="$base_host"
  [[ "$base_host" == *".elb.amazonaws.com" || "$base_host" == *".elb.amazonaws.com.cn" ]] || fail_closed "BASE_URL host must be an AWS ELB DNS name: $base_host"
  if [[ "$ALLOW_SELF_SIGNED_ALB_TLS" == "true" ]]; then
    [[ "$base_scheme" == "https" ]] || fail_closed "ALLOW_SELF_SIGNED_ALB_TLS=true is only allowed for https BASE_URL"
    CURL_TLS_ARGS=(--insecure)
    event "base-url" "self-signed-tls-allowed" "host=$base_host"
  else
    CURL_TLS_ARGS=()
    event "base-url" "public-tls-required" "host=$base_host"
  fi
}

validate_aws_identity() {
  local account
  account="$(aws_cmd sts get-caller-identity --query Account --output text)"
  [[ "$account" == "$EXPECTED_AWS_ACCOUNT_ID" ]] || fail_closed "AWS account mismatch: expected $EXPECTED_AWS_ACCOUNT_ID got $account"
  event "validate-aws-account" "ok" "profile=$AWS_PROFILE account=$account region=$AWS_REGION"
}

validate_eks_scope() {
  local cluster_json cluster_arn cluster_endpoint cluster_status cluster_project cluster_session cluster_approval context_server
  cluster_json="$(aws_cmd eks describe-cluster --name "$RESOURCE_PREFIX" --output json)"
  read -r cluster_arn cluster_endpoint cluster_status cluster_project cluster_session cluster_approval < <(
    python3 - "$cluster_json" <<'PY'
import json
import sys

cluster = json.loads(sys.argv[1])["cluster"]
tags = cluster.get("tags", {})
print(cluster["arn"], cluster["endpoint"], cluster["status"], tags.get("Project", ""), tags.get("Session", ""), tags.get("Approval", ""))
PY
  )
  local expected_arn="arn:aws:eks:${AWS_REGION}:${EXPECTED_AWS_ACCOUNT_ID}:cluster/${RESOURCE_PREFIX}"
  [[ "$cluster_arn" == "$expected_arn" ]] || fail_closed "EKS cluster ARN mismatch: expected exact session cluster $expected_arn"
  [[ "$cluster_status" == "ACTIVE" ]] || fail_closed "EKS cluster is not ACTIVE: $cluster_status"
  [[ "$cluster_project" == "kyobo-platform-live-lab" && "$cluster_session" == "$SESSION_ID" && "$cluster_approval" == "$APPROVAL_ID" ]] || fail_closed "EKS Project/Session/Approval tags do not match this approved session"
  context_server="$(kubectl --context "$KUBE_CONTEXT" config view --minify -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["clusters"][0]["cluster"]["server"])')"
  [[ "$context_server" == "$cluster_endpoint" ]] || fail_closed "KUBE_CONTEXT endpoint does not match the exact approved EKS cluster"
  event "validate-eks-scope" "ok" "cluster=${RESOURCE_PREFIX} account=$EXPECTED_AWS_ACCOUNT_ID region=$AWS_REGION"
}

validate_alb_scope() {
  local load_balancers alb_arn alb_state tag_response tag_values project_tag session_tag approval_tag
  load_balancers="$(aws_cmd elbv2 describe-load-balancers --output json)"
  read -r alb_arn alb_state < <(python3 -c 'import json,sys; items=[item for item in json.loads(sys.argv[1]).get("LoadBalancers", []) if item.get("DNSName")==sys.argv[2]]; len(items)==1 or sys.exit("BASE_URL must match exactly one load balancer in the approved region"); item=items[0]; print(item.get("LoadBalancerArn", ""), item.get("State", {}).get("Code", ""))' "$load_balancers" "$BASE_HOST") || fail_closed "Could not bind BASE_URL to a unique ALB"
  [[ "$alb_arn" == "arn:aws:elasticloadbalancing:${AWS_REGION}:${EXPECTED_AWS_ACCOUNT_ID}:loadbalancer/app/"* ]] || fail_closed "BASE_URL ALB ARN is outside the approved account/region"
  [[ "$alb_state" == "active" ]] || fail_closed "BASE_URL ALB is not active: $alb_state"
  tag_response="$(aws_cmd elbv2 describe-tags --resource-arns "$alb_arn" --output json)"
  tag_values="$(python3 -c 'import json,sys; items=json.loads(sys.argv[1]).get("TagDescriptions", []); len(items)==1 or sys.exit("expected exactly one ALB tag result"); values={tag.get("Key"):tag.get("Value", "") for tag in items[0].get("Tags", [])}; print(" ".join(values.get(key, "") for key in ("Project", "Session", "Approval")))' "$tag_response")" || fail_closed "Could not read exact ALB ownership tags"
  read -r project_tag session_tag approval_tag <<< "$tag_values"
  [[ "$project_tag" == "kyobo-platform-live-lab" && "$session_tag" == "$SESSION_ID" && "$approval_tag" == "$APPROVAL_ID" ]] || fail_closed "BASE_URL ALB Project/Session/Approval tags do not match this approved session"
  event "validate-alb-scope" "ok" "alb_arn=$alb_arn"
}

assert_rds_tags_and_endpoints() {
  local id="$1"
  local expected_role="$2"
  local output arn status endpoint multi_az security_groups tags project_tag tagged_session approval_tag source_id
  output="$(aws_cmd rds describe-db-instances \
    --db-instance-identifier "$id" \
    --query 'DBInstances[0].[DBInstanceArn,DBInstanceStatus,Endpoint.Address,MultiAZ,join(`,`,VpcSecurityGroups[].VpcSecurityGroupId),ReadReplicaSourceDBInstanceIdentifier]' \
    --output text)"
  read -r arn status endpoint multi_az security_groups source_id <<< "$output"
  [[ -n "$arn" && "$arn" != "None" ]] || fail_closed "RDS instance not found: $id"
  [[ "$arn" == "arn:aws:rds:${AWS_REGION}:${EXPECTED_AWS_ACCOUNT_ID}:db:${id}" ]] || fail_closed "RDS $id ARN is outside the exact approved account/region/resource name"
  [[ "$status" == "available" ]] || fail_closed "RDS instance $id is not available: $status"
  tags="$(aws_cmd rds list-tags-for-resource --resource-name "$arn" --output json)"
  read -r project_tag tagged_session approval_tag < <(python3 -c 'import json,sys; tags=json.loads(sys.argv[1]).get("TagList", []); values={item.get("Key"):item.get("Value") for item in tags}; print(values.get("Project", ""), values.get("Session", ""), values.get("Approval", ""))' "$tags")
  [[ "$project_tag" == "kyobo-platform-live-lab" && "$tagged_session" == "$SESSION_ID" && "$approval_tag" == "$APPROVAL_ID" ]] || fail_closed "RDS $id Project/Session/Approval tags do not match this exact live-lab session"
  event "validate-rds-${expected_role}" "ok" "id=$id endpoint=$endpoint multi_az=$multi_az"
  if [[ "$expected_role" == "writer" ]]; then
    ORIGINAL_WRITER_ENDPOINT="$endpoint"
    ORIGINAL_WRITER_SECURITY_GROUPS="$security_groups"
    [[ "$multi_az" == "True" ]] || fail_closed "Writer $id must be Multi-AZ for force-failover RTO probe"
  fi
  if [[ "$expected_role" == "reader" ]]; then
    [[ "$source_id" == "$WRITER_ID" ]] || fail_closed "Reader $id source mismatch: expected $WRITER_ID got ${source_id:-None}"
  fi
}

validate_kubernetes_scope() {
  local current_namespace image
  current_namespace="$(kubectl --context "$KUBE_CONTEXT" config view --minify --output 'jsonpath={..namespace}' 2>/dev/null || true)"
  [[ "$current_namespace" != "default" ]] || fail_closed "KUBE_CONTEXT resolves to default namespace; use a dedicated context for $NAMESPACE"
  kube get configmap "$CONFIGMAP_NAME" >/dev/null
  kube get secret "$SECRET_NAME" >/dev/null
  kube get configmap "$CA_CONFIGMAP_NAME" >/dev/null
  ORIGINAL_REPLICAS="$(kube get rollout "$ROLLOUT_NAME" -o jsonpath='{.spec.replicas}')"
  ORIGINAL_CRON_SUSPEND="$(kube get cronjob "$CRONJOB_NAME" -o jsonpath='{.spec.suspend}')"
  [[ -n "$ORIGINAL_CRON_SUSPEND" ]] || ORIGINAL_CRON_SUSPEND="false"
  image="$(kube get rollout "$ROLLOUT_NAME" -o "jsonpath={.spec.template.spec.containers[?(@.name=='${APP_CONTAINER_NAME}')].image}")"
  [[ "$image" == *@sha256:* ]] || fail_closed "Rollout image must be digest-pinned before a recovery drill: $image"
  event "validate-kubernetes-scope" "ok" "namespace=$NAMESPACE rollout=$ROLLOUT_NAME image=$image"
  APP_IMAGE="$image"
}

now_ms() {
  python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
}

probe_readyz() {
  local started_ms timestamp response status latency elapsed_ms
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
  started_ms="$(now_ms)"
  response="$(curl -sS "${CURL_TLS_ARGS[@]}" --max-time 5 -o /dev/null -w '%{http_code},%{time_total}' "$BASE_URL/readyz" 2>/dev/null || echo '000,timeout')"
  IFS=, read -r status latency <<< "$response"
  elapsed_ms=$(( $(now_ms) - started_ms ))
  printf '%s,%s,%s,%s\n' "$timestamp" "$elapsed_ms" "$status" "$latency" >> "$PROBES_FILE"
  [[ "$status" == "200" ]]
}

run_multi_az_rto_probe() {
  printf 'timestamp,probe_elapsed_ms,http_status,latency_seconds\n' > "$PROBES_FILE"
  for _ in 1 2 3; do
    probe_readyz || fail_closed "Writer readiness precheck failed before force-failover"
    sleep 1
  done
  local command_started_ms deadline seen_failure=0 failure_ms="" recovery_ms="" streak=0
  command_started_ms="$(now_ms)"
  event "multi-az-force-failover" "started" "writer=$WRITER_ID"
  run_mutating aws_cmd rds reboot-db-instance --db-instance-identifier "$WRITER_ID" --force-failover --output json > "$OUTPUT_DIR/reboot-db-instance.json"
  if [[ "$DRILL_MODE" != "execute" ]]; then
    event "multi-az-force-failover" "planned" "writer=$WRITER_ID"
    return 0
  fi
  deadline=$(( $(date +%s) + MAX_WAIT_SECONDS ))
  while [[ $(date +%s) -lt $deadline ]]; do
    if probe_readyz; then
      if (( seen_failure == 1 )); then
        streak=$((streak + 1))
        if (( streak >= 5 )); then
          recovery_ms=$(( $(now_ms) - command_started_ms ))
          break
        fi
      fi
    else
      if (( seen_failure == 0 )); then
        seen_failure=1
        failure_ms=$(( $(now_ms) - command_started_ms ))
        event "multi-az-force-failover" "first-readiness-failure" "failure_ms=$failure_ms"
      fi
      streak=0
    fi
    sleep "$POLL_INTERVAL_SECONDS"
  done
  [[ -n "$recovery_ms" || "$DRILL_MODE" != "execute" ]] || fail_closed "No five-probe readiness recovery within ${MAX_WAIT_SECONDS}s after force-failover"
  event "multi-az-force-failover" "completed" "first_failure_ms=${failure_ms:-not-observed} recovery_ms=${recovery_ms:-plan-mode}"
}

helper_resource_state() {
  local resource
  if ! resource="$(kube get "$1" "$2" --ignore-not-found -o name 2>/dev/null)"; then
    return 2
  fi
  [[ -n "$resource" ]] && return 0
  return 1
}

ensure_helper_configmap() {
  local resource_state=0
  if [[ "$DRILL_MODE" != "execute" ]]; then
    log "[plan] create exact helper ConfigMap $HELPER_CONFIGMAP_NAME from $HELPER_SOURCE"
    return 0
  fi
  [[ -f "$HELPER_SOURCE" ]] || fail_closed "Helper source not found: $HELPER_SOURCE"
  helper_resource_state configmap "$HELPER_CONFIGMAP_NAME" || resource_state=$?
  case "$resource_state" in
    0) fail_closed "Helper ConfigMap name already exists; refusing to reuse or replace it" ;;
    1) ;;
    *) fail_closed "Could not safely inspect helper ConfigMap; refusing to create it" ;;
  esac
  python3 - "$HELPER_CONFIGMAP_NAME" "$NAMESPACE" "$SESSION_ID" "$APPROVAL_ID" \
    "$RUN_SLUG" "$HELPER_SCOPE_TOKEN" "$HELPER_SOURCE" <<'PY' | kube create -f - >/dev/null
import json
import pathlib
import sys

name, namespace, session, approval, run_slug, token, source = sys.argv[1:8]
manifest = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {
        "name": name,
        "namespace": namespace,
        "labels": {
            "app.kubernetes.io/managed-by": "rds-failover-drill",
            "live-lab-session": session,
            "live-lab-approval": approval,
            "live-lab-run": run_slug,
            "live-lab-helper-token": token,
        },
    },
    "data": {"replica_drill.py": pathlib.Path(source).read_text(encoding="utf-8")},
}
print(json.dumps(manifest))
PY
}

helper_job() {
  local action="$1"
  shift
  local image="$1"
  shift
  local job_name job_token action_slug resource_state=0
  job_token="$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
  action_slug="${action//[^a-z0-9-]/-}"
  job_name="rds-drill-${RUN_SLUG:0:17}-${HELPER_SCOPE_TOKEN}-${job_token}-${action_slug}"
  (( ${#job_name} <= 63 )) || fail_closed "Helper Job name exceeds the Kubernetes DNS limit"
  local args_json result
  args_json="$(python3 - "$action" "$@" <<'PY'
import json
import sys
print(json.dumps(["python", "/drill/replica_drill.py", sys.argv[1], *sys.argv[2:]]))
PY
)"
  if [[ "$DRILL_MODE" != "execute" ]]; then
    log "[plan] helper job $job_name action=$action args=$*"
    return 0
  fi
  helper_resource_state job "$job_name" || resource_state=$?
  case "$resource_state" in
    0) fail_closed "Helper Job name already exists; refusing to reuse or replace it: $job_name" ;;
    1) ;;
    *) fail_closed "Could not safely inspect helper Job $job_name; refusing to create it" ;;
  esac
  python3 - "$job_name" "$image" "$args_json" "$CA_PATH" "$HELPER_CONFIGMAP_NAME" \
    "$NAMESPACE" "$SESSION_ID" "$APPROVAL_ID" "$RUN_SLUG" "$HELPER_SCOPE_TOKEN" "$job_token" <<'PY' | kube create -f - >/dev/null
import json
import sys

job_name, image, args_json, ca_path, helper_configmap_name, namespace, session_id, approval_id, run_slug, token, job_token = sys.argv[1:12]
command = json.loads(args_json)
labels = {
    "app.kubernetes.io/managed-by": "rds-failover-drill",
    "live-lab-session": session_id,
    "live-lab-approval": approval_id,
    "live-lab-run": run_slug,
    "live-lab-helper-token": token,
    "live-lab-helper-job-token": job_token,
}
manifest = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {
        "name": job_name,
        "namespace": namespace,
        "labels": labels,
    },
    "spec": {
        "backoffLimit": 0,
        "activeDeadlineSeconds": 180,
        "ttlSecondsAfterFinished": 600,
        "template": {
            "metadata": {
                "labels": labels,
            },
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 10001,
                    "runAsGroup": 10001,
                    "fsGroup": 10001,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [{
                    "name": "replica-drill",
                    "image": image,
                    "command": command,
                    "env": [
                        {"name": "DB_REQUIRE_TLS", "value": "true"},
                        {"name": "DB_SSL_CA", "value": ca_path},
                        {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                    ],
                    "envFrom": [
                        {"configMapRef": {"name": "raffle-config"}},
                        {"secretRef": {"name": "raffle-secret"}},
                    ],
                    "volumeMounts": [{
                        "name": "replica-drill-helper",
                        "mountPath": "/drill",
                        "readOnly": True,
                    }, {
                        "name": "rds-ca-bundle",
                        "mountPath": "/etc/rds-ca",
                        "readOnly": True,
                    }],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }],
                "volumes": [{
                    "name": "replica-drill-helper",
                    "configMap": {
                        "name": helper_configmap_name,
                        "items": [{"key": "replica_drill.py", "path": "replica_drill.py"}],
                    },
                }, {
                    "name": "rds-ca-bundle",
                    "configMap": {
                        "name": "rds-ca-bundle",
                        "items": [{"key": "global-bundle.pem", "path": "global-bundle.pem"}],
                    },
                }],
            },
        },
    },
}
print(json.dumps(manifest))
PY
  local job_deadline job_state
  job_deadline=$(( $(date +%s) + 180 ))
  while [[ $(date +%s) -lt $job_deadline ]]; do
    job_state="$(kube get job "$job_name" -o json | python3 -c 'import json,sys; j=json.load(sys.stdin); c=j.get("status",{}).get("conditions",[]); print("complete" if any(x.get("type")=="Complete" and x.get("status")=="True" for x in c) else "failed" if any(x.get("type")=="Failed" and x.get("status")=="True" for x in c) else "pending")')"
    [[ "$job_state" != "complete" ]] || break
    if [[ "$job_state" == "failed" ]]; then
      kube logs "job/$job_name" >&2 || true
      fail_closed "Helper job failed: $job_name"
    fi
    sleep 2
  done
  if [[ "$job_state" != "complete" ]]; then
    kube logs "job/$job_name" >&2 || true
    fail_closed "Helper job timed out: $job_name"
  fi
  result="$(kube logs "job/$job_name")"
  printf '%s\n' "$result" > "$OUTPUT_DIR/${job_name}.json"
  printf '%s\n' "$result"
}

cleanup_helpers() {
  if [[ "$DRILL_MODE" != "execute" ]]; then
    return 0
  fi
  log "Retaining helper ConfigMap for namespace-scoped teardown; completed Jobs expire by TTL"
  event "helper-cleanup" "retained" "configmap=$HELPER_CONFIGMAP_NAME cleanup=namespace-teardown job_ttl_seconds=600"
}

quiesce_writers() {
  event "quiesce-writers" "started" "cronjob=$CRONJOB_NAME rollout=$ROLLOUT_NAME"
  run_mutating kube patch cronjob "$CRONJOB_NAME" --type merge -p '{"spec":{"suspend":true}}'
  if [[ "$DRILL_MODE" == "execute" ]]; then
    local drain_deadline active_jobs
    drain_deadline=$(( $(date +%s) + QUIESCE_TIMEOUT_SECONDS ))
    while [[ $(date +%s) -lt $drain_deadline ]]; do
      active_jobs="$(kube get jobs -o json | python3 -c 'import json,sys; name=sys.argv[1]; data=json.load(sys.stdin); print(" ".join(job["metadata"]["name"] for job in data.get("items", []) if any(ref.get("kind")=="CronJob" and ref.get("name")==name for ref in job.get("metadata", {}).get("ownerReferences", [])) and job.get("status", {}).get("active", 0)))' "$CRONJOB_NAME")"
      [[ -n "$active_jobs" ]] || break
      log "Waiting for scheduled writer jobs to finish: $active_jobs"
      sleep "$POLL_INTERVAL_SECONDS"
    done
    [[ -z "${active_jobs:-}" ]] || fail_closed "Scheduled writer jobs did not drain before replica promotion: $active_jobs"
  fi
  run_mutating kube patch rollout "$ROLLOUT_NAME" --type merge -p '{"spec":{"replicas":0}}'
  if [[ "$DRILL_MODE" == "execute" ]]; then
    kube wait --for=delete pod -l "$APP_LABEL_SELECTOR" --timeout="${QUIESCE_TIMEOUT_SECONDS}s" || fail_closed "Application Pods did not terminate after scaling rollout to zero"
  fi
  event "quiesce-writers" "completed" "writers stopped"
}

assert_api_quiesced() {
  if [[ "$DRILL_MODE" != "execute" ]]; then
    log "[plan] assert no Pods match $APP_LABEL_SELECTOR before promotion"
    return 0
  fi
  local pods
  pods="$(kube get pods -l "$APP_LABEL_SELECTOR" --field-selector=status.phase!=Succeeded,status.phase!=Failed -o name)"
  [[ -z "$pods" ]] || fail_closed "API is not quiesced; active Pods remain: $pods"
  event "api-quiescence" "ok" "selector=$APP_LABEL_SELECTOR"
}

wait_for_reader_replication_health() {
  if [[ "$DRILL_MODE" != "execute" ]]; then
    log "[plan] wait for RDS read replication status Normal=true before quiescing writers"
    return 0
  fi
  local deadline replication_normal
  deadline=$(( $(date +%s) + MAX_WAIT_SECONDS ))
  while [[ $(date +%s) -lt $deadline ]]; do
    replication_normal="$(aws_cmd rds describe-db-instances \
      --db-instance-identifier "$READER_ID" \
      --query "DBInstances[0].StatusInfos[?StatusType=='read replication'].Normal | [0]" \
      --output text)"
    if [[ "$replication_normal" == "True" ]]; then
      event "reader-replication-health" "ok" "reader=$READER_ID normal=true"
      return 0
    fi
    sleep "$POLL_INTERVAL_SECONDS"
  done
  fail_closed "RDS reader replication did not report Normal=true within ${MAX_WAIT_SECONDS}s"
}

require_two_fresh_lag_readings() {
  if [[ "$DRILL_MODE" != "execute" ]]; then
    log "[plan] require two fresh CloudWatch ReplicaLag datapoints for $READER_ID <= ${REPLICA_LAG_THRESHOLD_SECONDS}s"
    return 0
  fi
  local end_time start_time datapoints
  end_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  start_time="$(date -u -v-15M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '15 minutes ago' +%Y-%m-%dT%H:%M:%SZ)"
  datapoints="$(aws_cmd cloudwatch get-metric-statistics \
    --namespace AWS/RDS \
    --metric-name ReplicaLag \
    --dimensions "Name=DBInstanceIdentifier,Value=${READER_ID}" \
    --start-time "$start_time" \
    --end-time "$end_time" \
    --period 60 \
    --statistics Average \
    --output json)"
  printf '%s\n' "$datapoints" > "$OUTPUT_DIR/cloudwatch-replica-lag.json"
  python3 scripts/validate_rds_replica_lag.py \
    --input "$OUTPUT_DIR/cloudwatch-replica-lag.json" \
    --threshold-seconds "$REPLICA_LAG_THRESHOLD_SECONDS" \
    > "$OUTPUT_DIR/cloudwatch-replica-lag-gate.json" \
    || fail_closed "CloudWatch ReplicaLag gate failed; promotion is unsafe"
  event "replica-lag-gate" "ok" "$(tr -d '\n' < "$OUTPUT_DIR/cloudwatch-replica-lag-gate.json")"
}

promote_replica_and_cutover() {
  local image="$1"
  local fence_sg="$2"
  [[ "$PROMOTE_REPLICA_CONFIRM" == "$PROMOTE_REPLICA_CONFIRM_EXPECTED" ]] || fail_closed "PROMOTE_REPLICA_CONFIRM must be exactly '$PROMOTE_REPLICA_CONFIRM_EXPECTED'"
  PROMOTION_STARTED=1
  save_checkpoint
  event "promote-replica" "started" "reader=$READER_ID"
  run_mutating aws_cmd rds promote-read-replica --db-instance-identifier "$READER_ID" --output json > "$OUTPUT_DIR/promote-read-replica.json"
  if [[ "$DRILL_MODE" == "execute" ]]; then
    aws_cmd rds wait db-instance-available --db-instance-identifier "$READER_ID"
    PROMOTED_WRITER_ENDPOINT="$(aws_cmd rds describe-db-instances --db-instance-identifier "$READER_ID" --query 'DBInstances[0].Endpoint.Address' --output text)"
    [[ -n "$PROMOTED_WRITER_ENDPOINT" && "$PROMOTED_WRITER_ENDPOINT" != "None" ]] || fail_closed "Promoted writer endpoint could not be resolved"
  else
    PROMOTED_WRITER_ENDPOINT="<promoted-reader-endpoint>"
  fi
  save_checkpoint
  event "fence-old-primary" "started" "writer=$WRITER_ID fence_sg=$fence_sg"
  run_mutating aws_cmd rds modify-db-instance --db-instance-identifier "$WRITER_ID" --vpc-security-group-ids "$fence_sg" --apply-immediately --output json > "$OUTPUT_DIR/fence-old-primary.json"
  if [[ "$DRILL_MODE" == "execute" ]]; then
    local fence_deadline observed_groups
    fence_deadline=$(( $(date +%s) + MAX_WAIT_SECONDS ))
    while [[ $(date +%s) -lt $fence_deadline ]]; do
      observed_groups="$(aws_cmd rds describe-db-instances --db-instance-identifier "$WRITER_ID" --query 'join(`,`,DBInstances[0].VpcSecurityGroups[].VpcSecurityGroupId)' --output text)"
      [[ "$observed_groups" == "$fence_sg" ]] && break
      sleep "$POLL_INTERVAL_SECONDS"
    done
    [[ "${observed_groups:-}" == "$fence_sg" ]] || fail_closed "Old writer security-group fence did not converge to the exact no-ingress group"
    event "fence-old-primary" "verified" "writer=$WRITER_ID security_group=$fence_sg"
  fi
  event "update-configmap-writer" "started" "endpoint=$PROMOTED_WRITER_ENDPOINT"
  run_mutating kube patch configmap "$CONFIGMAP_NAME" --type merge -p "{\"data\":{\"DB_WRITER_HOST\":\"$PROMOTED_WRITER_ENDPOINT\"}}"
  helper_job readiness "$image" --role writer >/dev/null
  helper_job verify-marker "$image" --role writer --run-id "$RUN_ID" --username "$SYNTHETIC_USERNAME" >/dev/null
  run_mutating kube patch rollout "$ROLLOUT_NAME" --type merge -p "{\"spec\":{\"replicas\":${ORIGINAL_REPLICAS:-2}}}"
  if [[ "$DRILL_MODE" == "execute" ]]; then
    kube wait --for=condition=available "rollout/$ROLLOUT_NAME" --timeout=300s || fail_closed "Rollout did not become available after promoted writer cutover"
  fi
  helper_job readiness "$image" --role writer >/dev/null
  helper_job verify-marker "$image" --role writer --run-id "$RUN_ID" --username "$SYNTHETIC_USERNAME" >/dev/null
  event "restore-cronjob" "started" "cronjob=$CRONJOB_NAME suspend=${ORIGINAL_CRON_SUSPEND:-false}"
  run_mutating kube patch cronjob "$CRONJOB_NAME" --type merge -p "{\"spec\":{\"suspend\":${ORIGINAL_CRON_SUSPEND:-false}}}"
  event "restore-cronjob" "completed" "cronjob=$CRONJOB_NAME suspend=${ORIGINAL_CRON_SUSPEND:-false}"
  event "promote-replica" "completed" "endpoint=$PROMOTED_WRITER_ENDPOINT"
}

main() {
  require_tool aws
  require_tool kubectl
  require_tool terraform
  require_tool python3
  require_tool curl
  validate_identifiers
  validate_base_url
  require_execute_confirmation
  save_checkpoint

  validate_aws_identity
  assert_rds_tags_and_endpoints "$WRITER_ID" writer
  assert_rds_tags_and_endpoints "$READER_ID" reader
  local fence_sg recovery_started_ms recovery_completed_ms
  validate_eks_scope
  validate_alb_scope
  validate_kubernetes_scope
  local app_image="$APP_IMAGE"
  fence_sg="$(require_fence_output)"
  ensure_helper_configmap
  save_checkpoint

  run_multi_az_rto_probe

  if [[ "$ENABLE_REPLICA_PROMOTION" != "true" ]]; then
    event "replica-promotion" "skipped" "set ENABLE_REPLICA_PROMOTION=true and PROMOTE_REPLICA_CONFIRM=$PROMOTE_REPLICA_CONFIRM_EXPECTED"
    cleanup_helpers
    save_checkpoint
    write_recovery_instructions
    log "Multi-AZ RTO phase complete. Replica promotion skipped by ENABLE_REPLICA_PROMOTION=$ENABLE_REPLICA_PROMOTION"
    return 0
  fi

  wait_for_reader_replication_health
  recovery_started_ms="$(now_ms)"
  quiesce_writers
  helper_job record-marker "$app_image" --run-id "$RUN_ID" --username "$SYNTHETIC_USERNAME" >/dev/null
  helper_job verify-marker "$app_image" --role writer --run-id "$RUN_ID" --username "$SYNTHETIC_USERNAME" >/dev/null
  helper_job wait-marker "$app_image" --role reader --run-id "$RUN_ID" --username "$SYNTHETIC_USERNAME" \
    --timeout-seconds "$REPLICA_MARKER_TIMEOUT_SECONDS" --poll-interval-seconds "$REPLICA_MARKER_POLL_SECONDS" >/dev/null
  wait_for_reader_replication_health
  require_two_fresh_lag_readings
  assert_api_quiesced
  promote_replica_and_cutover "$app_image" "$fence_sg"
  recovery_completed_ms="$(now_ms)"

  event "recovery-time" "completed" "recovery_ms=$(( recovery_completed_ms - recovery_started_ms ))"
  cleanup_helpers
  save_checkpoint
  write_recovery_instructions
  log "Drill completed. Evidence: $OUTPUT_DIR"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
