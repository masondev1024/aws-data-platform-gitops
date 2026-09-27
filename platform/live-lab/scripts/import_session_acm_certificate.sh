#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

: "${AWS_PROFILE:?AWS_PROFILE must name the approved SSO profile}"
: "${AWS_REGION:?AWS_REGION must be explicitly set to ap-northeast-2}"
: "${EXPECTED_ACCOUNT_ID:?EXPECTED_ACCOUNT_ID must be the approved 12-digit account ID}"
: "${SESSION_ID:?SESSION_ID must be the Terraform session_id}"
: "${APPROVAL_ID:?APPROVAL_ID must be the recorded approval_id}"

PROJECT="kyobo-platform-live-lab"
OUTPUT_FILE="platform/live-lab/evidence/acm-certificate-arn.txt"

if [[ "$AWS_REGION" != "ap-northeast-2" || ! "$EXPECTED_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "BLOCKED: this live lab is approved only for the exact Seoul region and a 12-digit account." >&2
  exit 2
fi
if [[ ! "$SESSION_ID" =~ ^[a-z0-9][a-z0-9-]{5,40}$ || ! "$APPROVAL_ID" =~ ^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$ ]]; then
  echo "BLOCKED: invalid session_id or approval_id format." >&2
  exit 2
fi

identity="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity --query Account --output text)"
if [[ "$identity" != "$EXPECTED_ACCOUNT_ID" ]]; then
  echo "BLOCKED: active SSO identity account does not match EXPECTED_ACCOUNT_ID." >&2
  exit 2
fi

mkdir -p platform/live-lab/evidence
chmod 700 platform/live-lab/evidence
if [[ -s "$OUTPUT_FILE" ]]; then
  existing_arn="$(<"$OUTPUT_FILE")"
  if [[ ! "$existing_arn" =~ ^arn:aws:acm:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:certificate/[A-Fa-f0-9-]+$ ]]; then
    echo "BLOCKED: existing certificate ARN evidence does not match this account and region." >&2
    exit 2
  fi
  certificate_status="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" acm describe-certificate --certificate-arn "$existing_arn" --query Certificate.Status --output text)"
  if [[ "$certificate_status" != "ISSUED" ]]; then
    echo "BLOCKED: recorded ACM certificate is not ready (status=$certificate_status)." >&2
    exit 2
  fi
  echo "$existing_arn"
  exit 0
fi

umask 077
temp_dir="$(mktemp -d platform/live-lab/evidence/.acm-cert.XXXXXX)"
chmod 700 "$temp_dir"
cleanup() { rm -rf "$temp_dir"; }
trap cleanup EXIT

openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 1 \
  -keyout "$temp_dir/private-key.pem" \
  -out "$temp_dir/certificate.pem" \
  -subj "/CN=live-lab.invalid" \
  -addext "subjectAltName=DNS:live-lab.invalid"
chmod 600 "$temp_dir/private-key.pem" "$temp_dir/certificate.pem"

certificate_arn="$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" acm import-certificate \
  --certificate "fileb://$temp_dir/certificate.pem" \
  --private-key "fileb://$temp_dir/private-key.pem" \
  --tags "Key=Project,Value=$PROJECT" \
         "Key=Session,Value=$SESSION_ID" \
         "Key=Approval,Value=$APPROVAL_ID" \
         "Key=ManagedBy,Value=local-live-lab" \
  --query CertificateArn --output text)"

if [[ ! "$certificate_arn" =~ ^arn:aws:acm:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:certificate/[A-Fa-f0-9-]+$ ]]; then
  echo "BLOCKED: ACM returned an unexpected certificate ARN." >&2
  exit 2
fi
printf '%s\n' "$certificate_arn" > "$OUTPUT_FILE"
chmod 600 "$OUTPUT_FILE"
echo "$certificate_arn"
