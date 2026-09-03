# Failure drills와 운영 런북

> 상태: Pod·DB endpoint·잘못된 canary·RDS Multi-AZ failover의 short-lived 장애 주입을 완료했다. read replica lag, Prometheus scrape 중단, Alertmanager webhook 장애는 별도 미실행이다.

## 공통 실행 규칙

- 테스트 전 대상 커밋 SHA, 이미지 SHA, namespace, AWS 리전, 담당자를 기록한다.
- 운영 사용자와 운영 자격증명을 사용하지 않는다.
- 각 drill은 감지 시간, 완화 시작 시간, 복구 시간, 데이터 손실 여부를 남긴다.
- `kubectl delete`, DB failover, 잘못된 이미지 배포는 테스트 namespace와 short-lived 환경에서만 실행한다.
- 복구 후에는 alert가 해소되었는지, replica 수·DB connection·응모 중복 건수를 확인한다.

## Drill 목록

| ID | 주입 상황 | 기대 감지 | 복구 기준 | 현재 상태 |
| --- | --- | --- | --- | --- |
| FD-001 | 새 이미지가 `/readyz`에 실패 | Rollout analysis/readiness | Argo Rollouts abort 후 stable 복귀 | 완료 · Analysis Error 후 stable 100% |
| FD-002 | canary Pod 강제 종료 | Pod restart/rollout metric | ReplicaSet 재생성, error budget 영향 없음 | 완료 · 약 2초에 2 Ready |
| FD-003 | read replica 연결 불가 | `RaffleDatabaseReadinessLost` + `/readyz` 503 | traffic 격리 후 DB 복구/endpoint 확인 | 실행 필요 |
| FD-004 | writer DB 연결 불가 | `database_error`와 5xx/503 증가 | 재시도 폭풍 없이 복구, 응모 중복 0건 | 부분 실행 · fault Pod endpoint 격리 |
| FD-007 | RDS Multi-AZ writer failover | `/readyz` timeout/복구 probe | 같은 writer endpoint로 복귀, 애플리케이션 RTO 기록 | 완료 · application RTO 21.112초 |
| FD-005 | Prometheus scrape 중단 | Analysis inconclusive | 배포 자동 중단, 관측 복구 후 재시도 | 실행 필요 |
| FD-006 | Alertmanager webhook 실패 | Alertmanager delivery error | 알림 채널 복구, 대시보드에서 alert 확인 | 실행 필요 |

## FD-001: 잘못된 이미지와 자동 중단

1. 테스트 브랜치에서 의도적으로 존재하지 않는 DB host를 가진 이미지를 만든다.
2. Kustomize의 이미지 SHA를 테스트 이미지로 변경하고 Rollout을 시작한다.
3. 10% 단계에서 `/readyz` 실패와 analysis 상태를 확인한다.
4. `kubectl argo rollouts get rollout data-pipeline-rollout --watch`로 abort 여부를 확인한다.
5. stable ReplicaSet이 복구되고 사용자 요청의 5xx가 정상화되는지 확인한다.

기록할 값: detection latency, abort latency, stable 복귀 시간, 최종 Pod 수, 에러율.

## FD-003/FD-004: DB 장애와 정확성

DB 장애 중에 k6 one-shot 응모를 실행해 오류 응답이 503인지, 클라이언트 재시도 후 `UNIQUE(user_id, item_id)` 위반이 추가 레코드를 만들지 않는지 확인한다. DB 복구 뒤에는 다음을 비교한다.

```sql
SELECT user_id, item_id, COUNT(*) AS entries
FROM raffle_entries
GROUP BY user_id, item_id
HAVING COUNT(*) > 1;
```

이 쿼리 결과가 비어 있어야 하며, 응모 성공 수·DB 레코드 수·`raffle_apply_requests_total{result="success"}`가 설명 가능하게 일치해야 한다.

## RTO/RPO 기록 템플릿

| Drill | RTO 목표 | 실제 RTO | RPO 목표 | 실제 RPO | 데이터 검증 | 후속 작업 |
| --- | --- | --- | --- | --- | --- | --- |
| baseline/soak | 5분 이내 | Pod 약 2초, DB endpoint 약 9초, RDS Multi-AZ 21.112초 | 응모 확정 데이터 손실 없음 확인 범위 | row 대조 범위 내 미관측 | 정상화·teardown 감사 완료 | read replica lag 후속 |

RTO/RPO는 설계 문구가 아니라 장애 주입 결과로만 확정한다. 이번에는 RDS Multi-AZ failover의 애플리케이션 RTO를 기록했지만, read replica lag과 failover 전후 DB row 대조는 아직 별도 검증하지 않았다.

## 비용 가드레일과 검증 순서

스트리밍 SLO만 확인하는 날에는 EKS 기반 장애 주입을 시작하지 않는다. 먼저 `terraform/validation` 프로필에서 100Hz Smoke와 1,000Hz 단계 부하를 실행하고 iterator age, Firehose freshness, throttle, S3 Parquet 생성 여부를 확인한다. 이 프로필은 14개 리소스만 생성하며 EKS·NAT·EC2·ALB를 만들지 않는다.

2026-08-24 실제 단기 실행은 100Hz 약 72초, 7,200 records, generator 실패 0건으로 종료했고 S3 Parquet 2개를 확인했다. Kinesis iterator age는 0ms, write throttle은 0이었다. Firehose CloudWatch 지표는 `NO_DATA`였으므로 freshness SLO PASS는 보류했다. destroy는 14개 리소스 기준 41.6초였고, 이후 Kinesis/Firehose/S3/state 잔여가 없었다.

EKS/RDS 장애 drill이 필요한 경우에만 별도 비용 승인을 받고 전체 애플리케이션 프로필을 만든다. 테스트 종료 순서는 `generator/k6 종료 → CloudWatch metric 정지 확인 → workload/Ingress/ALB 확인 → Terraform destroy → EIP/NAT/ECR/S3/Secrets/로그 잔여 확인`으로 고정한다. Billing alarm은 알림 장치일 뿐 자동 중지 장치가 아니므로 destroy 확인이 실제 비용 통제 수단이다.
## 2026-08-24 실제 AWS 검증 결과

### Pod 장애

- 대상: `data-pipeline-rollout` stable Pod 1개
- 조치: 정확한 Pod 이름을 확인한 뒤 삭제
- 결과: ALB `/healthz`는 계속 200, replacement Pod Ready 관측 약 2초
- 최종 상태: Rollout `Healthy`, 2 replicas/2 Ready

### DB 장애

- 주입: 새 Pod에만 DB writer/reader endpoint를 loopback으로 바꾸는 ConfigMap을 적용
- 기대: DB readiness 실패 Pod가 Service endpoint에서 제외되고 기존 Pod가 트래픽 유지
- 결과: fault 구간에도 ALB 200, Ready 1개로 격리
- 복구: 원래 endpoint 복원과 failed Pod 교체 후 약 9초 내 2 replicas/2 Ready

### 잘못된 배포와 자동 rollback

- 주입: canary AnalysisTemplate 조건을 검증 중에만 실패하도록 변경하고 template annotation으로 새 revision 생성
- 결과: 10% canary → 2분 pause → AnalysisRun Error → Rollout `Degraded` → canary ReplicaSet 0 → stable revision 복귀
- 복구: AnalysisTemplate 원복, annotation 제거
- 최종 상태: Rollout `Healthy`, current step 6, stable 100%

AnalysisRun에서 canary metric vector가 준비되지 않아 `slice index out of range`가 발생했다. 이를 실패로 분류한 것은 안전했지만, 다음에는 synthetic canary traffic과 no-data 상태를 별도 `Inconclusive`로 표현하는 개선을 진행한다.

## 부하 검증과 정확성

| 시나리오 | 결과 |
|---|---|
| readiness 5 req/s, 30초 | 151/151, p95 343.4ms |
| readiness 20 req/s, 30초 | 601/601, p50 320.1ms, p95 354.5ms, p99 406.5ms |
| readiness 50 req/s, 30초 | HTTP error 0%, 그러나 20 arrival iteration drop — capacity 실패 경계 |
| apply one-shot 30 VU | signup/login/apply 90/90 성공, `/api/apply` p95/p99 510.9/534.1ms |
| 정확성 수동 검증 | 비로그인 401, 최초 응모 200, 중복 응모 400 |

50 req/s는 HTTP 오류율만 보면 통과처럼 보이므로 arrival iteration drop까지 함께 봐야 한다. 이 훈련에서 처리량을 과장하지 않고 20 req/s를 현재 readiness 지속 용량 하한으로 기록했다.

## RTO/RPO 표 보정 — 2026-08-24 실행분

앞의 초기 템플릿에서 `미실행`으로 남아 있던 항목은 아래 실제 short-lived EKS 실행 결과로 보정한다. RDS Multi-AZ failover는 실행했고, read replica lag은 별도 미검증으로 남긴다.

| Drill | 실제 RTO | 실제 RPO | 판정 |
|---|---:|---:|---|
| Pod 삭제 | 약 2초 | 해당 없음 | 2 Ready 복구, Rollout Healthy |
| DB 연결 장애 주입 | 약 9초 | 데이터 손실 없음 확인 범위 | 장애 Pod 격리, 기존 Pod는 ALB 200 |
| 잘못된 canary 배포 | 약 2분 10초(10% weight + pause 포함) | stable 데이터 유지 | Analysis Error 후 stable 100% 복귀 |
| RDS Multi-AZ writer failover | 21.112초 | row 대조 미기록 | 동일 writer endpoint 복구, failover 구간 실패를 별도 집계 |
| RDS read replica lag | 미실행 | 미실행 | 실제 replica 지연 수치로 주장하지 않음 |
