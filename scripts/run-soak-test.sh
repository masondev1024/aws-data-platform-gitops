#!/usr/bin/env bash
set -Eeuo pipefail

: "${BASE_URL:?BASE_URL is required, for example http://my-alb.eu-west-1.elb.amazonaws.com}"

MODE="${MODE:-readiness}"
RATE="${RATE:-20}"
DURATION="${DURATION:-45m}"
PRE_ALLOCATED_VUS="${PRE_ALLOCATED_VUS:-40}"
MAX_VUS="${MAX_VUS:-100}"
RUN_ID="${RUN_ID:-soak-$(date -u +%Y%m%dT%H%M%SZ)}"
OUTPUT_DIR="${OUTPUT_DIR:-evidence/${RUN_ID}}"
K6_IMAGE="${K6_IMAGE:-grafana/k6@sha256:5221b620a4f874faff6e32ba597aa667c058391fe4898b1c6f6377f062c6cdec}"

command -v docker >/dev/null || {
  echo "docker is required to run the pinned k6 image" >&2
  exit 1
}

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

cat > "$OUTPUT_DIR/metadata.env" <<EOF
BASE_URL=$BASE_URL
MODE=$MODE
RATE=$RATE
DURATION=$DURATION
PRE_ALLOCATED_VUS=$PRE_ALLOCATED_VUS
MAX_VUS=$MAX_VUS
RUN_ID=$RUN_ID
K6_IMAGE=$K6_IMAGE
STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

echo "[soak] base=$BASE_URL mode=$MODE rate=$RATE duration=$DURATION"
echo "[soak] evidence=$OUTPUT_DIR"

docker run --rm -i \
  -v "$OUTPUT_DIR:/results" \
  -e BASE_URL \
  -e MODE \
  -e RATE \
  -e DURATION \
  -e PRE_ALLOCATED_VUS \
  -e MAX_VUS \
  -e RUN_ID \
  "$K6_IMAGE" run \
  --out json=/results/k6.json \
  --summary-export=/results/summary.json \
  - < loadtest/raffle.js \
  2>&1 | tee "$OUTPUT_DIR/k6.log"

cat >> "$OUTPUT_DIR/metadata.env" <<EOF
FINISHED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

echo "[soak] completed; summary=$OUTPUT_DIR/summary.json"
