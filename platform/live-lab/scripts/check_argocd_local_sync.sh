#!/usr/bin/env bash
set -euo pipefail

if ! command -v argocd >/dev/null 2>&1; then
  echo "BLOCKED: argocd CLI is not installed; remote Git approval is required for GitOps evidence." >&2
  exit 2
fi

if argocd app sync --help | grep -F -- '--local' >/dev/null; then
  echo "PASS: argocd app sync --local is supported by this CLI."
  exit 0
fi

echo "BLOCKED: argocd app sync --local is unsupported; do not replace GitOps evidence with kubectl apply." >&2
exit 2
