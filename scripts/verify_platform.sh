#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python}"

usage() {
  cat <<'USAGE'
Usage: scripts/verify_platform.sh [phase ...]

Phases:
  tests             Run Python unit tests.
  manifests         Render and check Kubernetes overlays.
  terraform         Run backendless Terraform validation.
  python-security   Run Python dependency and static security checks.
  all               Run every non-Docker platform phase.
USAGE
}

run_tests() {
  "$PYTHON_BIN" -m pytest -q app/tests scripts/tests
}

run_manifests() {
  command -v kubectl >/dev/null || {
    echo "kubectl is required for the manifests phase" >&2
    exit 127
  }

  local rendered_prod
  local rendered_validation
  rendered_prod="$(mktemp)"
  rendered_validation="$(mktemp)"
  trap "rm -f -- '$rendered_prod' '$rendered_validation'; trap - RETURN" RETURN

  kubectl kustomize k8s/overlays/prod >"$rendered_prod"
  kubectl kustomize k8s/overlays/validation >"$rendered_validation"
  kubectl kustomize platform/governance >/dev/null
  kubectl kustomize platform/live-lab/manifests >/dev/null
  test -s "$rendered_prod"
  test -s "$rendered_validation"

  grep -F "data-pipeline-schema-migration" "$rendered_prod" >/dev/null
  grep -F "canary-apply-outbox-parity" "$rendered_prod" >/dev/null
  grep -F "ALLOW_FAILURE_DRILL" k8s/overlays/validation/patch-rollout.yaml >/dev/null
  grep -F "value: validation" "$rendered_validation" >/dev/null
}

run_terraform() {
  command -v terraform >/dev/null || {
    echo "terraform is required for the terraform phase" >&2
    exit 127
  }

  local directory
  for directory in terraform platform/live-lab/terraform; do
    (
      cd "$directory"
      terraform init -backend=false -input=false
      terraform fmt -check -recursive
      terraform validate
    )
  done
}

run_python_security() {
  "$PYTHON_BIN" -m pip_audit -r app/requirements.txt
  "$PYTHON_BIN" -m pip_audit -r agentic_ops/requirements.txt
  "$PYTHON_BIN" -m pip_audit -r agentic_ops/requirements-bedrock.txt
  "$PYTHON_BIN" -m bandit --quiet --severity-level high --exclude app/tests -r app
  "$PYTHON_BIN" -m bandit --quiet --severity-level high -r agentic_ops
  "$PYTHON_BIN" -m bandit --quiet --severity-level high scripts/collect_agentic_incident.py scripts/triage_incident.py scripts/platform_doctor.py
  "$PYTHON_BIN" -m bandit --quiet --severity-level high scripts/verify_release_bundle.py scripts/verify_gitops_deployment.py
  "$PYTHON_BIN" -m bandit --quiet --severity-level high platform/live-lab/scripts/deploy_gitops_validation.py
  "$PYTHON_BIN" -m bandit --quiet --severity-level high platform/live-lab/scripts/bind_gitops_alb_identity.py
  "$PYTHON_BIN" -m bandit --quiet --severity-level high platform/live-lab/scripts/observe_gitops_as_developer.py
}

run_phase() {
  case "$1" in
    tests) run_tests ;;
    manifests) run_manifests ;;
    terraform) run_terraform ;;
    python-security) run_python_security ;;
    all)
      run_tests
      run_manifests
      run_terraform
      run_python_security
      ;;
    -h|--help) usage ;;
    *)
      echo "unknown platform verification phase: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
}

if [ "$#" -eq 0 ]; then
  set -- all
fi

for phase in "$@"; do
  run_phase "$phase"
done
