# Soak Test와 RDS Multi-AZ Failover 검증

> 목적: short-lived 환경을 단순 smoke test가 아니라, 일정 시간의 정상 상태·장애 복구·teardown 증거로 확장한다.

## 검증 범위

- 45분 readiness steady-state (`20 req/s` 기본값)
- soak 중 Prometheus HTTP SLI, p95/p99, dropped iteration, HPA, Pod restart 수집
- RDS `MultiAZ=true` 확인 후 `reboot-db-instance --force-failover` 실행
- `/readyz` probe로 최초 장애 시각과 5회 연속 정상 복구 시각 기록
- 기존 Pod 장애와 잘못된 Canary rollback runbook 재실행
- 모든 검증 후 Terraform destroy와 EKS/RDS/ALB/NAT/EIP/EBS 잔여 감사

## 구현 선택

RDS Replica와 Multi-AZ standby는 다른 목적이다. Replica는 비동기 읽기 확장·lag 검증 대상이고, 이번 failover drill은 writer endpoint가 동기 standby로 전환되는 복구 경계를 검증하므로 `enable_rds_multi_az=true`를 별도 플래그로 추가했다. 두 옵션을 동시에 켜지 않아도 Multi-AZ failover 자체는 수행할 수 있다.

`enable_rds_multi_az`는 기본값이 `false`이며 `allow_full_stack_apply=true` 없이는 Terraform precondition에서 차단된다. 비용과 장애 범위를 명시적으로 승인해야만 활성화된다.

## 실행 명령

```bash
cd work/develope-project/terraform

AWS_PROFILE=develope-test AWS_REGION=eu-west-1 \
TF_VAR_db_password="$TEST_DB_PASSWORD" \
TF_VAR_alertmanager_webhook_url="$ALERTMANAGER_WEBHOOK_URL" \
terraform apply -auto-approve \
  -var='allow_full_stack_apply=true' \
  -var='enable_rds_multi_az=true' \
  -var='enable_rds_replica=false' \
  -var='enable_multi_az_nat=false'
```

애플리케이션이 Ready가 된 뒤 ALB 주소를 받아 다음을 실행한다.

```bash
cd ..
BASE_URL="http://<alb-hostname>" \
RATE=20 DURATION=45m \
OUTPUT_DIR="evidence/soak-$(date -u +%Y%m%dT%H%M%SZ)" \
./scripts/run-soak-test.sh

BASE_URL="http://<alb-hostname>" \
DB_INSTANCE_IDENTIFIER="data-pipeline-primary" \
OUTPUT_DIR="evidence/rds-failover-$(date -u +%Y%m%dT%H%M%SZ)" \
./scripts/rds-failover-drill.sh
```

## 판정 기준

| 항목 | PASS 기준 |
|---|---|
| Soak readiness | 45분 동안 `http_req_failed < 1%`, dropped iteration 0, p95 500ms 이하·p99 1s 이하 |
| 데이터 정합성 | soak 후 중복 응모 레코드 0건, 정상 응모 결과와 DB row 설명 가능 |
| RDS failover | `MultiAZ=true`, failover command 성공, `/readyz`가 5회 연속 200으로 복구 |
| 복구 | 기존 Pod/DB fault Pod 격리, 서비스가 200을 유지하거나 정의한 RTO 안에 복구 |
| Teardown | Terraform state 0, 전용 EKS/RDS/ALB/NAT/EIP/EBS 잔여 없음 |

failover 중 잠시 5xx가 발생하는 것은 장애 주입의 관측 결과다. 전체 soak SLO와 분리해 `first_failure_ms`, `observed_application_rto_ms`, 오류 수, 데이터 손실 여부를 함께 기록한다. RDS Multi-AZ failover가 실제로 실행되지 않으면 해당 수치는 포트폴리오에 쓰지 않는다.

## 결과 기록

| 실행일 | Soak | RDS Multi-AZ | 최초 장애 | 애플리케이션 RTO | 데이터 손실 | Teardown |
|---|---|---|---:|---:|---|---|
| 2026-08-24 | 45분·19.94 req/s·53,833회 | `MultiAZ=true`, force failover 실행 | 19.094초 | 21.112초 | 별도 row 대조 미기록 | Terraform destroy·잔여 감사 완료 |

이번 soak은 정상 steady-state만 평가한 실행이 아니다. RDS failover를 의도적으로 주입했기 때문에 247건(0.45%)의 요청 실패가 관측됐고, k6 threshold는 장애 구간을 포함한 결과로 표시됐다. 정상 capacity baseline은 별도 20 req/s·30초 실행에서 601/601 성공으로 판정했다. failover 증거는 `evidence/rds-failover-multi-az-20260824/summary.txt`, soak 요약은 `evidence/soak-multi-az-20260824/summary.json`에 보관한다. k6 원본 JSON은 GitHub 단일 파일 제한을 넘지 않도록 `evidence/soak-multi-az-20260824/k6.json.gz`로 보관했으며, 압축 해제한 내용의 SHA-256은 `176ca5a1780e8d15e3174d8467228d2c891dd5a2e950e888014db4e3d895e2e0`이다.
