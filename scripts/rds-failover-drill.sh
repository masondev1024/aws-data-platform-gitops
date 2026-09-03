#!/usr/bin/env bash
set -Eeuo pipefail

: "${BASE_URL:?BASE_URL is required, for example http://my-alb.eu-west-1.elb.amazonaws.com}"
: "${DB_INSTANCE_IDENTIFIER:?DB_INSTANCE_IDENTIFIER is required}"

AWS_REGION="${AWS_REGION:-eu-west-1}"
AWS_PROFILE="${AWS_PROFILE:-develope-test}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-1}"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-900}"
OUTPUT_DIR="${OUTPUT_DIR:-evidence/rds-failover-$(date -u +%Y%m%dT%H%M%SZ)}"

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
CSV_FILE="$OUTPUT_DIR/probes.csv"
SUMMARY_FILE="$OUTPUT_DIR/summary.txt"
COMMAND_FILE="$OUTPUT_DIR/reboot-command.json"

command -v aws >/dev/null || { echo "aws CLI is required" >&2; exit 1; }
if ! command -v curl >/dev/null && ! command -v python3 >/dev/null; then
  echo "curl or python3 is required for readiness probes" >&2
  exit 1
fi

export AWS_PROFILE AWS_REGION

read -r DB_STATUS DB_MULTI_AZ DB_ENDPOINT < <(
  aws rds describe-db-instances \
    --db-instance-identifier "$DB_INSTANCE_IDENTIFIER" \
    --query 'DBInstances[0].[DBInstanceStatus,MultiAZ,Endpoint.Address]' \
    --output text
)

if [[ "$DB_MULTI_AZ" != "True" ]]; then
  echo "RDS instance is not Multi-AZ: status=$DB_STATUS multi_az=$DB_MULTI_AZ" >&2
  exit 1
fi

printf 'timestamp,elapsed_ms,http_status,latency_seconds\n' > "$CSV_FILE"

now_ms() {
  python3 -c 'import time; print(int(time.time() * 1000))'
}

probe() {
  local timestamp elapsed_ms response status latency
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
  if command -v curl >/dev/null; then
    response="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code},%{time_total}' "$BASE_URL/readyz" 2>/dev/null || echo '000,timeout')"
  else
    response="$(BASE_URL="$BASE_URL" python3 - <<'PY'
import os
import time
import urllib.request

started = time.monotonic()
try:
    with urllib.request.urlopen(os.environ["BASE_URL"] + "/readyz", timeout=5) as response:
        response.read(1)
        print(f"{response.status},{time.monotonic() - started:.6f}")
except Exception:
    print("000,timeout")
PY
)"
  fi
  IFS=, read -r status latency <<< "$response"
  elapsed_ms=$(( $(now_ms) - COMMAND_STARTED_MS ))
  printf '%s,%s,%s,%s\n' "$timestamp" "$elapsed_ms" "$status" "$latency" >> "$CSV_FILE"
  echo "$status $latency"
  [[ "$status" == "200" ]]
}

echo "[rds-failover] instance=$DB_INSTANCE_IDENTIFIER endpoint=$DB_ENDPOINT status=$DB_STATUS multi_az=$DB_MULTI_AZ"
echo "[rds-failover] probing $BASE_URL/readyz"

for _ in 1 2 3; do
  COMMAND_STARTED_MS="$(now_ms)"
  probe >/dev/null || {
    echo "readiness precheck failed" >&2
    exit 1
  }
  sleep 1
done

COMMAND_STARTED_MS="$(now_ms)"
aws rds reboot-db-instance \
  --db-instance-identifier "$DB_INSTANCE_IDENTIFIER" \
  --force-failover \
  --region "$AWS_REGION" \
  --output json > "$COMMAND_FILE"

echo "[rds-failover] reboot requested at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

deadline=$(( $(date +%s) + MAX_WAIT_SECONDS ))
seen_failure=0
recovery_ms=""
failure_ms=""
success_streak=0

while [[ $(date +%s) -lt $deadline ]]; do
  if probe; then
    if (( seen_failure == 1 )); then
      success_streak=$((success_streak + 1))
      if (( success_streak >= 5 )); then
        recovery_ms=$(( $(now_ms) - COMMAND_STARTED_MS ))
        break
      fi
    fi
  else
    if (( seen_failure == 0 )); then
      seen_failure=1
      failure_ms=$(( $(now_ms) - COMMAND_STARTED_MS ))
      echo "[rds-failover] first readiness failure at ${failure_ms}ms"
    fi
    success_streak=0
  fi
  sleep "$POLL_INTERVAL_SECONDS"
done

final_status="$(aws rds describe-db-instances \
  --db-instance-identifier "$DB_INSTANCE_IDENTIFIER" \
  --query 'DBInstances[0].[DBInstanceStatus,MultiAZ,Endpoint.Address]' \
  --output text)"

{
  echo "instance=$DB_INSTANCE_IDENTIFIER"
  echo "endpoint=$DB_ENDPOINT"
  echo "final_status=$final_status"
  echo "first_failure_ms=${failure_ms:-not-observed}"
  echo "recovery_after_command_ms=${recovery_ms:-not-observed}"
  if [[ -n "$failure_ms" && -n "$recovery_ms" ]]; then
    echo "observed_application_rto_ms=$((recovery_ms - failure_ms))"
  else
    echo "observed_application_rto_ms=not-observed"
  fi
  echo "csv=$CSV_FILE"
} | tee "$SUMMARY_FILE"

if [[ -z "$recovery_ms" ]]; then
  echo "RDS failover did not produce five consecutive healthy readiness probes within ${MAX_WAIT_SECONDS}s" >&2
  exit 1
fi
