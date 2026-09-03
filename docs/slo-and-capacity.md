# SLO와 용량 측정 기준

> 상태: 2026-08-24 실제 AWS short-lived validation을 완료했다. 아래 수치는 단일 t3.medium·단일 계정/리전의 검증 증거이며 production capacity로 확대 해석하지 않는다. 정상 baseline과 RDS Multi-AZ failover가 포함된 soak 결과는 서로 분리해 기록한다.

## 서비스 범위

대상은 래플 응모 API와 해당 API가 의존하는 MySQL read/write 경로다. `/healthz`는 프로세스 생존성, `/readyz`는 읽기 DB 의존성, `/metrics`는 내부 관측 경로로 분류한다. `/metrics` 자체는 사용자 트래픽 SLO 계산에서 제외한다.

## SLI와 초기 SLO 가설

| 영역 | SLI | 초기 목표 | 측정 소스 |
| --- | --- | --- | --- |
| 가용성 | `/api/apply`의 5xx 비율 | 99.9% 성공 응답 | `raffle_http_requests_total` |
| 지연시간 | `/api/apply` p95 / p99 | p95 500ms 이하, p99 1s 이하 | `raffle_http_request_duration_seconds` |
| 정확성 | 동일 사용자·상품 중복 응모 | 중복 레코드 0건 | DB unique key + 응모 결과 메트릭 |
| 의존성 | `/readyz` 성공률 | 장애 감지 후 30초 이내 트래픽 격리 | `raffle_db_readiness`, Kubernetes probe |
| 포화도 | HPA replicas, CPU/memory, DB connections | connection exhaustion 및 OOM 없음 | Kubernetes/RDS metrics |

`duplicate` 응답은 재시도 안전성을 나타내는 정상적인 비즈니스 결과일 수 있으므로 HTTP 실패율과 별도로 집계한다. 반대로 `database_error`, 5xx, connection exhaustion은 SLO 위반 후보로 분류한다.

## 용량 테스트 순서

1. Health-only smoke로 애플리케이션 프로세스와 네트워크 경로를 확인한다.
2. Readiness baseline을 1~5 RPS의 짧은 구간으로 실행하고, DB 연결 및 probe 상태를 확인한다.
3. 10 RPS부터 단계적으로 부하를 높이면서 각 구간을 충분히 유지한다.
4. 마지막으로 고유 계정 기반 one-shot 응모 시나리오를 실행해 쓰기 경로와 정확성을 확인한다.
5. SLO를 처음 위반한 지점과 직전의 연속 통과 지점을 모두 기록한다. 지속 가능 용량은 후자로 보고한다.

## 결과 템플릿

| 실행 ID | 모드 | 부하 | 지속시간 | p50/p95/p99 | 오류율 | HPA | DB 상태 | 판정 |
| --- | --- | ---: | --- | --- | ---: | --- | --- | --- |
| 20260824-baseline | readiness | 20 req/s | 30초 | 320.1 / 354.5 / 406.5ms | 0% | 2 replicas | RDS primary | 지속 용량 하한 통과 |
| 20260824-soak-failover | readiness | 19.94 req/s | 45분 | 318.8 / 355.49 / 634.52ms | 0.45%* | 1~2 replicas | RDS Multi-AZ failover | 장애 주입 결과 별도 기록 |

실행 결과에는 테스트 대상 커밋 SHA, 이미지 SHA, 리전, replica 수, DB instance class, k6 옵션을 함께 기록한다. 그래야 용량 숫자가 환경과 분리되어 재현 불가능한 성과 지표가 되는 것을 막을 수 있다.

## Prometheus 연동 계획

현재 애플리케이션은 `/metrics`를 노출하고, Prometheus Operator용 stable/canary `ServiceMonitor`와 Grafana SRE overview dashboard를 제공한다. Terraform이 Prometheus/Grafana/Alertmanager stack을 먼저 설치한 뒤 Argo Rollouts의 metric-based `AnalysisTemplate`이 실행되도록 구성한다. Prometheus가 없는 클러스터에서 분석 단계를 먼저 켜면 정상 배포도 자동 중단될 수 있기 때문이다.

Alertmanager는 webhook URL을 Terraform 변수로 명시하지 않으면 인프라를 적용할 수 없도록 했다. 알림 수신자가 없는 “모니터링이 설치된 것처럼 보이는” 상태를 운영 성공으로 간주하지 않기 위한 안전장치다. webhook URL은 Terraform state에 들어갈 수 있으므로 원격 state 암호화와 접근 제어가 전제다.

인터넷-facing ALB에서는 `/metrics` 경로를 fixed-response 404로 차단하고, Prometheus는 클러스터 내부 Pod IP를 직접 스크랩해야 한다. Prometheus를 외부에 공개해야 하는 경우에는 별도 인증·네트워크 경계를 설계한다.

## 운영상 주의

- 테스트 계정과 상품은 전용 namespace/prefix로 격리하고 실행 후 삭제한다.
- 운영 데이터와 실제 사용자 자격증명을 부하 테스트에 사용하지 않는다.
- EKS/RDS/NAT/ALB를 다시 생성할 때는 최소 비용 profile과 종료 체크리스트를 먼저 적용한다.
- 실제 readiness 처리량·p95/p99·HPA 동작·Pod/DB 장애 복구와 RDS Multi-AZ failover는 검증했다. RDS read replica lag과 node-level eviction은 검증하지 않았다.

## 비용 프로필별 측정 경계

| 프로필 | 검증 대상 | 생성하지 않는 항목 | 권장 순서 |
|---|---|---|---|
| 스트리밍 단기 검증 | Kinesis lag, Firehose freshness, throttle, S3 적재 | EKS, EC2, NAT, ALB, RDS, HPA | 100Hz → 1,000Hz → verifier → destroy |
| 애플리케이션 부하 검증 | HTTP SLO, HPA, DB 연결, replica lag, Canary | 별도 선택 | 비용 승인 → 최소 EKS/RDS → k6 → 증거 저장 → destroy |

스트리밍 프로필의 64MB/60초 Firehose buffer는 Parquet 변환을 유지할 수 있는 최소 크기이며, 기존 128MB/300초보다 데이터 신선도 피드백을 최대 약 4분 빠르게 한다. 낮은 처리량에서는 크기보다 60초 interval이 flush를 결정한다. 이 설정은 장시간 운영 설정으로 승격하지 않으며, 실제 처리량과 비용은 프로필별로 별도 기록하고 스트리밍 검증 결과를 애플리케이션 API 용량으로 확대 해석하지 않는다.

2026-08-24 streaming evidence: 100Hz 약 72초, 7,200 records, 실패 0, Parquet 2개, Kinesis iterator age 0ms, write throttle 0. Firehose freshness/success metric query는 당시 `NO_DATA`였고, 이는 CloudWatch 관측 지연 가능성이 있어 해당 metric을 PASS로 판정하지 않았다.

## 2026-08-24 실제 래플 AWS 검증 증거

| 항목 | 관측값 |
|---|---:|
| 리전/구성 | `eu-west-1`, EKS 1개 t3.medium, RDS `db.t3.micro` Multi-AZ failover profile, ALB |
| 이미지 | `live-validation-20260824`, digest `sha256:fdfac508194695697019b545764478daefadeb5ade9a75129b5b537010d3484e` |
| readiness 5 req/s, 30초 | 151/151, error 0%, p95 343.4ms |
| readiness 20 req/s, 30초 | 601/601, error 0%, p50 320.1ms, p95 354.5ms, p99 406.5ms |
| readiness 50 req/s, 30초 | HTTP error 0%, dropped arrival 20건, 지속 용량으로 불인정 |
| apply one-shot 30 VU | signup/login/apply 90/90 성공; `/api/apply` p95/p99 510.9/534.1ms |
| HPA | CPU 2%/50%, 2 desired/current, max 4 |
| 정확성 | unauthenticated 401, first apply 200, duplicate apply 400 |
| Pod 장애 | ALB 200 유지, replacement Ready 약 2초 |
| DB 장애 | faulty Pod readiness 격리, endpoint 복구 후 2 Ready 약 9초 |
| canary rollback | AnalysisRun Error → Degraded → stable 복귀 → Healthy |

50 req/s에서 HTTP error만 보면 통과처럼 보이므로 k6 `dropped_iterations`를 함께 봤다. 이 profile에서 기록할 수 있는 capacity 하한은 readiness 기준 20 req/s이며, 응모 API의 one-shot 30 VU 결과는 sustained RPS가 아니라 쓰기 경로/정확성 검증이다.

45분 soak 결과는 RDS Multi-AZ force failover를 포함한 장애 주입 실행이다. 총 53,833회 readiness 요청에서 247건(0.45%)이 failover 구간에 실패했고, p95 355.49ms·p99 634.52ms, 관측 애플리케이션 RTO 21.112초를 기록했다. 이 실패율은 정상 steady-state 오류율이 아니므로 baseline capacity 판정과 합산하지 않는다. read replica lag과 RPO row 대조는 별도 검증 범위다.
