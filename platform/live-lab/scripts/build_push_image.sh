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

[[ "$AWS_REGION" == "ap-northeast-2" ]] || { echo "BLOCKED: region must be ap-northeast-2." >&2; exit 2; }
[[ "$EXPECTED_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || { echo "BLOCKED: EXPECTED_ACCOUNT_ID must be 12 digits." >&2; exit 2; }
[[ "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ ]] || { echo "BLOCKED: invalid SESSION_ID." >&2; exit 2; }
[[ "$APPROVAL_ID" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]] || { echo "BLOCKED: invalid APPROVAL_ID." >&2; exit 2; }
command -v docker >/dev/null || { echo "BLOCKED: docker is required." >&2; exit 2; }
command -v terraform >/dev/null || { echo "BLOCKED: terraform is required." >&2; exit 2; }

identity="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity --query Account --output text)"
[[ "$identity" == "$EXPECTED_ACCOUNT_ID" ]] || { echo "BLOCKED: AWS identity account mismatch." >&2; exit 2; }
repository="$(terraform -chdir=platform/live-lab/terraform output -raw ecr_repository_url)"
expected_repository="${EXPECTED_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/kyobo-${SESSION_ID}/data-pipeline-app"
[[ "$repository" == "$expected_repository" ]] || { echo "BLOCKED: ECR repository output is outside the exact session path." >&2; exit 2; }
repository_arn="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecr describe-repositories --repository-names "kyobo-${SESSION_ID}/data-pipeline-app" --query 'repositories[0].repositoryArn' --output text)"
tag_response="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecr list-tags-for-resource --resource-arn "$repository_arn" --output json)"
read -r project_tag session_tag approval_tag < <(python3 -c 'import json,sys; tags={item.get("Key"):item.get("Value", "") for item in json.loads(sys.argv[1]).get("tags", [])}; print(tags.get("Project", ""), tags.get("Session", ""), tags.get("Approval", ""))' "$tag_response")
[[ "$project_tag" == "kyobo-platform-live-lab" && "$session_tag" == "$SESSION_ID" && "$approval_tag" == "$APPROVAL_ID" ]] || { echo "BLOCKED: ECR repository ownership tags mismatch." >&2; exit 2; }

docker info >/dev/null
registry="${repository%%/*}"
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecr get-login-password | docker login --username AWS --password-stdin "$registry" >/dev/null
image_tag="${IMAGE_TAG:-live-${SESSION_ID}-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
[[ "$image_tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || { echo "BLOCKED: IMAGE_TAG is not a valid immutable ECR tag." >&2; exit 2; }
docker buildx build --platform linux/amd64 --provenance=false --push \
  --file app/Dockerfile --tag "${repository}:${image_tag}" app
digest="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecr describe-images \
  --repository-name "kyobo-${SESSION_ID}/data-pipeline-app" \
  --image-ids "imageTag=${image_tag}" --query 'imageDetails[0].imageDigest' --output text)"
[[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "BLOCKED: ECR did not return a valid SHA-256 image digest." >&2; exit 2; }
image_ref="${repository}@${digest}"
mkdir -p platform/live-lab/evidence
chmod 700 platform/live-lab/evidence
tmp_file="$(mktemp platform/live-lab/evidence/.image-ref.XXXXXX)"
trap 'rm -f "$tmp_file"' EXIT
printf '%s\n' "$image_ref" > "$tmp_file"
chmod 600 "$tmp_file"
/bin/mv "$tmp_file" platform/live-lab/evidence/image-ref.txt
trap - EXIT
echo "Published session image by digest: $image_ref"
