# Failure drills와 운영 런북

> 상태: 실행 전 계획. 아래 표의 결과는 클러스터를 다시 생성한 뒤 실제 실행하고 채워야 한다.

## 공통 실행 규칙

- 테스트 전 대상 커밋 SHA, 이미지 SHA, namespace, AWS 리전, 담당자를 기록한다.
- 운영 사용자와 운영 자격증명을 사용하지 않는다.
- 각 drill은 감지 시간, 완화 시작 시간, 복구 시간, 데이터 손실 여부를 남긴다.
- `kubectl delete`, DB failover, 잘못된 이미지 배포는 테스트 namespace와 short-lived 환경에서만 실행한다.
- 복구 후에는 alert가 해소되었는지, replica 수·DB connection·응모 중복 건수를 확인한다.

## Drill 목록

| ID | 주입 상황 | 기대 감지 | 복구 기준 | 현재 상태 |
| --- | --- | --- | --- | --- |
| FD-001 | 새 이미지가 `/readyz`에 실패 | Rollout analysis/readiness | Argo Rollouts abort 후 stable 복귀 | 실행 필요 |
| FD-002 | canary Pod 강제 종료 | Pod restart/rollout metric | ReplicaSet 재생성, error budget 영향 없음 | 실행 필요 |
| FD-003 | read replica 연결 불가 | `RaffleDatabaseReadinessLost` + `/readyz` 503 | traffic 격리 후 DB 복구/endpoint 확인 | 실행 필요 |
| FD-004 | writer DB 연결 불가 | `database_error`와 5xx/503 증가 | 재시도 폭풍 없이 복구, 응모 중복 0건 | 실행 필요 |
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
| 미실행 | 5분 이내 | - | 응모 확정 데이터 0건 손실 | - | 미실행 | 클러스터 재생성 후 실행 |

RTO/RPO는 설계 문구가 아니라 장애 주입 결과로만 확정한다. 특히 RDS failover와 read replica lag를 실행하지 않은 상태에서는 데이터 손실·복구 시간을 주장하지 않는다.
