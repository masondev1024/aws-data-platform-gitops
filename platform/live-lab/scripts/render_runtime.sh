#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
umask 077

mode="${1:-runtime}"
if [[ "$mode" != "runtime" && "$mode" != "--static" ]]; then
  echo "Usage: render_runtime.sh [--static]" >&2
  exit 2
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "BLOCKED: kubectl is required for local kustomize rendering." >&2
  exit 2
fi

mkdir -p platform/live-lab/evidence
tmp_dir="$(mktemp -d platform/live-lab/evidence/.render.XXXXXX)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

write_render() {
  local target="$1"
  shift
  local tmp_file="$tmp_dir/$(basename "$target")"
  kubectl kustomize "$@" > "$tmp_file"
  chmod 600 "$tmp_file"
  /bin/mv "$tmp_file" "$target"
}

if [[ "$mode" == "--static" ]]; then
  write_render platform/live-lab/evidence/rendered-app-manifests.yaml platform/live-lab/manifests/app
  echo "Wrote platform/live-lab/evidence/rendered-app-manifests.yaml"
  exit 0
fi

terraform_dir="platform/live-lab/terraform"
if [[ -z "${DB_WRITER_HOST:-}" ]]; then
  DB_WRITER_HOST="$(terraform -chdir="$terraform_dir" output -raw db_writer_endpoint 2>/dev/null || true)"
fi
if [[ -z "${DB_READER_HOST:-}" ]]; then
  DB_READER_HOST="$(terraform -chdir="$terraform_dir" output -raw db_reader_endpoint 2>/dev/null || true)"
fi
if [[ -z "${ALB_CERTIFICATE_ARN:-}" && -s "platform/live-lab/evidence/acm-certificate-arn.txt" ]]; then
  ALB_CERTIFICATE_ARN="$(< platform/live-lab/evidence/acm-certificate-arn.txt)"
fi
if [[ -z "${OPERATOR_CIDR:-}" ]]; then
  OPERATOR_CIDR="$(terraform -chdir="$terraform_dir" output -raw operator_cidr 2>/dev/null || true)"
fi
if [[ -z "${SESSION_ID:-}" ]]; then
  SESSION_ID="$(terraform -chdir="$terraform_dir" output -raw session_id 2>/dev/null || true)"
fi
if [[ -z "${APPROVAL_ID:-}" ]]; then
  APPROVAL_ID="$(terraform -chdir="$terraform_dir" output -raw approval_id 2>/dev/null || true)"
fi
APP_IMAGE_REF="${APP_IMAGE_REF:-}"
if [[ -z "$APP_IMAGE_REF" && -s "platform/live-lab/evidence/image-ref.txt" ]]; then
  APP_IMAGE_REF="$(< platform/live-lab/evidence/image-ref.txt)"
fi
for endpoint in "$DB_WRITER_HOST" "$DB_READER_HOST"; do
  if [[ ! "$endpoint" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "BLOCKED: Terraform DB endpoint outputs are required; endpoint values must be DNS hostnames." >&2
    exit 2
  fi
done
if [[ ! "$ALB_CERTIFICATE_ARN" =~ ^arn:aws:acm:[a-z0-9-]+:[0-9]{12}:certificate/[A-Fa-f0-9-]+$ ]]; then
  echo "BLOCKED: a session ACM certificate ARN is required for the HTTPS-only live ingress." >&2
  exit 2
fi
if [[ ! "$OPERATOR_CIDR" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]]; then
  echo "BLOCKED: operator_cidr must be a single IPv4 /32 for the ALB ingress." >&2
  exit 2
fi
if [[ ! "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ || ! "$APPROVAL_ID" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]]; then
  echo "BLOCKED: session_id and approval_id Terraform outputs are required for resource tagging." >&2
  exit 2
fi
expected_image_repository="${ECR_REPOSITORY_URL:-$(terraform -chdir="$terraform_dir" output -raw ecr_repository_url 2>/dev/null || true)}"
if [[ ! "$expected_image_repository" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/kyobo-${SESSION_ID}/data-pipeline-app$ ]] || \
   [[ ! "$APP_IMAGE_REF" =~ @sha256:[a-f0-9]{64}$ ]] || \
   [[ "${APP_IMAGE_REF%@*}" != "$expected_image_repository" ]]; then
  echo "BLOCKED: a digest-pinned image from this session's ECR repository is required." >&2
  exit 2
fi

write_render platform/live-lab/evidence/rendered-bootstrap-manifests.yaml platform/live-lab/manifests/bootstrap
sed -e "s/__DB_WRITER_HOST__/${DB_WRITER_HOST}/g" \
  -e "s/__DB_READER_HOST__/${DB_READER_HOST}/g" \
  platform/live-lab/evidence/rendered-bootstrap-manifests.yaml > "$tmp_dir/rendered-bootstrap.yaml"
chmod 600 "$tmp_dir/rendered-bootstrap.yaml"
/bin/mv "$tmp_dir/rendered-bootstrap.yaml" platform/live-lab/evidence/rendered-bootstrap-manifests.yaml
write_render platform/live-lab/evidence/rendered-app-manifests.yaml platform/live-lab/manifests/app
sed -e "s|__ALB_CERTIFICATE_ARN__|${ALB_CERTIFICATE_ARN}|g" \
  -e "s|__OPERATOR_CIDR__|${OPERATOR_CIDR}|g" \
  -e "s|__SESSION_ID__|${SESSION_ID}|g" \
  -e "s|__APPROVAL_ID__|${APPROVAL_ID}|g" \
  -e "s|image: data-pipeline-app:latest|image: ${APP_IMAGE_REF}|g" \
  platform/live-lab/evidence/rendered-app-manifests.yaml > "$tmp_dir/rendered-app.yaml"
chmod 600 "$tmp_dir/rendered-app.yaml"
/bin/mv "$tmp_dir/rendered-app.yaml" platform/live-lab/evidence/rendered-app-manifests.yaml
echo "Wrote platform/live-lab/evidence/rendered-bootstrap-manifests.yaml"
echo "Wrote platform/live-lab/evidence/rendered-app-manifests.yaml"
