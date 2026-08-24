# SLO와 용량 측정 기준

> 상태: 초안. 아래 목표는 운영 측정 전의 가설이며, 실제 AWS 실행 결과가 아니다.

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
| 미실행 | - | - | - | - | - | - | AWS 인프라 종료 | 측정 필요 |

실행 결과에는 테스트 대상 커밋 SHA, 이미지 SHA, 리전, replica 수, DB instance class, k6 옵션을 함께 기록한다. 그래야 용량 숫자가 환경과 분리되어 재현 불가능한 성과 지표가 되는 것을 막을 수 있다.

## Prometheus 연동 계획

현재 애플리케이션은 `/metrics`를 노출하고, Prometheus Operator용 stable/canary `ServiceMonitor`와 Grafana SRE overview dashboard를 제공한다. Terraform이 Prometheus/Grafana/Alertmanager stack을 먼저 설치한 뒤 Argo Rollouts의 metric-based `AnalysisTemplate`이 실행되도록 구성한다. Prometheus가 없는 클러스터에서 분석 단계를 먼저 켜면 정상 배포도 자동 중단될 수 있기 때문이다.

Alertmanager는 webhook URL을 Terraform 변수로 명시하지 않으면 인프라를 적용할 수 없도록 했다. 알림 수신자가 없는 “모니터링이 설치된 것처럼 보이는” 상태를 운영 성공으로 간주하지 않기 위한 안전장치다. webhook URL은 Terraform state에 들어갈 수 있으므로 원격 state 암호화와 접근 제어가 전제다.

인터넷-facing ALB에서는 `/metrics` 경로를 fixed-response 404로 차단하고, Prometheus는 클러스터 내부 Pod IP를 직접 스크랩해야 한다. Prometheus를 외부에 공개해야 하는 경우에는 별도 인증·네트워크 경계를 설계한다.

## 운영상 주의

- 테스트 계정과 상품은 전용 namespace/prefix로 격리하고 실행 후 삭제한다.
- 운영 데이터와 실제 사용자 자격증명을 부하 테스트에 사용하지 않는다.
- EKS/RDS/NAT/ALB를 다시 생성할 때는 최소 비용 profile과 종료 체크리스트를 먼저 적용한다.
- 현재는 실제 처리량, p95/p99, HPA 동작, replica lag, RTO/RPO를 검증했다고 주장할 수 없다.
