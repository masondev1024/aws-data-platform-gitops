#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  validate_governance_auth.sh --context CONTEXT --namespace platform-validation --principal PRINCIPAL [--execute]

Default mode is read-only dry-run: the script prints the fixed kubectl auth
matrix without contacting the cluster. --execute runs only kubectl auth checks;
it never applies, syncs, deletes, patches, execs, or runs caller-supplied commands.
USAGE
}

context=""
namespace=""
principal=""
execute="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --context)
      context="${2:-}"
      shift 2
      ;;
    --namespace)
      namespace="${2:-}"
      shift 2
      ;;
    --principal)
      principal="${2:-}"
      shift 2
      ;;
    --execute)
      execute="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unexpected argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$context" || -z "$namespace" || -z "$principal" ]]; then
  echo "--context, --namespace, and --principal are required" >&2
  usage >&2
  exit 2
fi

if [[ "$namespace" != "platform-validation" ]]; then
  echo "refusing namespace '$namespace'; expected platform-validation" >&2
  exit 2
fi

checks=(
  "allow|get|pods|"
  "allow|get|events|"
  "allow|get|rollouts.argoproj.io|"
  "allow|get|analysisruns.argoproj.io|"
  "allow|get|servicemonitors.monitoring.coreos.com|"
  "deny|get|secrets|"
  "deny|create|pods|"
  "deny|create|pods|exec"
  "deny|create|serviceaccounts|"
  "deny|create|roles.rbac.authorization.k8s.io|"
  "deny|create|rolebindings.rbac.authorization.k8s.io|"
  "deny|bind|clusterrole/admin|"
  "deny|create|networkpolicies.networking.k8s.io|"
)

print_matrix() {
  printf 'mode=%s context=%s namespace=%s principal=%s\n' "$1" "$context" "$namespace" "$principal"
  printf 'expectation,verb,resource,subresource\n'
  for check in "${checks[@]}"; do
    IFS='|' read -r expected verb resource subresource <<<"$check"
    printf '%s,%s,%s,%s\n' "$expected" "$verb" "$resource" "$subresource"
  done
}

if [[ "$execute" != "true" ]]; then
  print_matrix "dry-run"
  exit 0
fi

kubectl_base=(kubectl --context "$context" --namespace "$namespace" --as "$principal" --request-timeout=10s)
stderr_file=$(mktemp)
trap 'rm -f "$stderr_file"' EXIT

if ! identity=$("${kubectl_base[@]}" auth whoami -o json 2>"$stderr_file") || [[ -s "$stderr_file" ]]; then
  echo "principal identity check failed; impersonation/setup failure is not an expected deny" >&2
  exit 10
fi
if ! printf '%s' "$identity" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["status"]["userInfo"]["username"] == sys.argv[1] else 1)' "$principal" 2>/dev/null; then
  echo "returned identity does not match requested principal" >&2
  exit 10
fi

check_access() {
  local verb="$1" resource="$2" subresource="${3:-}" output status=0
  if [[ -n "$subresource" ]]; then
    output=$("${kubectl_base[@]}" auth can-i "$verb" "$resource" --subresource="$subresource" 2>"$stderr_file") || status=$?
  elif [[ "$resource" == clusterrole/admin ]]; then
    output=$(kubectl --context "$context" --as "$principal" --request-timeout=10s auth can-i "$verb" "$resource" --all-namespaces 2>"$stderr_file") || status=$?
  else
    output=$("${kubectl_base[@]}" auth can-i "$verb" "$resource" 2>"$stderr_file") || status=$?
  fi
  if [[ ! -s "$stderr_file" && "$status" == 0 && "$output" == yes ]]; then
    printf 'allow'
  elif [[ ! -s "$stderr_file" && "$status" == 1 && "$output" == no ]]; then
    printf 'deny'
  else
    echo "authorization query failed or returned an invalid response" >&2
    return 2
  fi
}

if ! positive=$(check_access get pods) || [[ "$positive" != allow ]]; then
  echo "positive control failed: principal cannot get pods in platform-validation" >&2
  exit 11
fi

failed=0
for check in "${checks[@]}"; do
  IFS='|' read -r expected verb resource subresource <<<"$check"
  if ! actual=$(check_access "$verb" "$resource" "$subresource"); then
    echo "auth check failed for $verb $resource; not an expected deny" >&2
    failed=1
    continue
  fi
  printf '%s,%s,%s,%s,%s\n' "$expected" "$actual" "$verb" "$resource" "$subresource"
  if [[ "$expected" != "$actual" ]]; then
    failed=1
  fi
done

exit "$failed"
