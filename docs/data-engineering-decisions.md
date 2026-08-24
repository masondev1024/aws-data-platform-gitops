# Data Engineering Decisions

> 기준일: 2026-08-24

이 문서는 단순히 애플리케이션을 실행하는 방법이 아니라, 운영 가능한 데이터 플랫폼으로 발전시키기 위해 선택한 설계와 trade-off를 기록한다.

## 1. CI와 CD 분리

### 결정

CI는 외부 AWS 인증 없이도 재현 가능한 정적 검증을 수행하고, CD만 GitHub OIDC로 AWS에 접근한다.

### 이유

- PR 검증이 AWS 계정 상태에 종속되지 않는다.
- AWS 권한 실패와 애플리케이션/manifest 품질 실패를 분리할 수 있다.
- 장기 보관 access key를 GitHub secret에 저장하지 않는다.

### 운영 기준

- CI: pytest, compileall, Docker build, Kustomize render, Terraform validate
- CD: SHA immutable image push, manifest update, Argo CD sync 대상 변경
- 배포 role은 repository와 branch 조건으로 trust policy를 제한한다.

## 2. GitOps 기반 배포

### 결정

GitHub Actions가 Kubernetes API에 직접 `kubectl apply`하지 않고, ECR push 후 `kustomization.yaml`의 image tag만 변경한다. Argo CD가 `main`을 감시하고 Argo Rollouts로 canary를 수행한다.

### 이유

- 배포 선언 상태가 Git에 남아 audit 가능하다.
- 재시도와 rollback의 기준점이 명확하다.
- CI credential이 클러스터 관리자 권한을 직접 가질 필요가 없다.

### 주의점

GitHub Actions의 ECR push role과 Argo CD의 cluster access role은 분리해야 한다. 이미지 push 성공은 애플리케이션이 실제로 ready 상태라는 의미가 아니므로 readiness probe와 rollout analysis가 필요하다.

## 3. 이미지와 데이터 계약

### 결정

이미지는 `latest`가 아니라 Git commit SHA로 배포한다. Kubernetes는 `raffle-config` ConfigMap과 `raffle-secret` Secret을 통해 DB 접속 정보를 주입한다.

### 이유

- 동일 tag 재사용으로 인한 재현성 저하를 방지한다.
- 환경별 설정과 애플리케이션 이미지를 분리한다.
- DB writer/reader endpoint를 명시해 read/write 경계를 데이터 계약으로 만든다.

### 데이터 계약

```text
ConfigMap: DB_WRITER_HOST, DB_READER_HOST, DB_NAME, DB_USER
Secret:    DB_PASSWORD, SECRET_KEY
```

스키마 변경이 발생할 때는 애플리케이션 배포와 DB migration의 backward-compatible 순서를 별도로 설계해야 한다.

## 4. Health check와 데이터 품질 경계

### 결정

- `/healthz`: 프로세스가 요청을 처리할 수 있는지 확인한다.
- `/readyz`: MySQL `SELECT 1`을 수행해 DB 의존성까지 확인한다.
- DB 연결에는 timeout을 설정하고 실패 시 503을 반환한다.

### 이유

liveness에 DB 상태를 넣으면 DB 장애가 전체 Pod 재시작 폭풍으로 이어질 수 있다. 반대로 readiness에 DB 검사를 넣으면 트래픽을 정상 Pod로만 보내고 장애를 빠르게 격리할 수 있다.

이것은 데이터 품질의 첫 번째 운영 경계다. 더 나아가 ingestion/processing 단계에는 row count, null ratio, freshness, duplicate key, schema compatibility 검사를 추가해야 한다.

## 5. RDS primary/replica 선택

### 결정

Terraform 설계는 MySQL primary와 read replica를 분리하고 애플리케이션은 writer/reader endpoint를 구분한다.

### Trade-off

- 장점: 읽기 확장과 장애 격리의 기반이 된다.
- 단점: replica lag, failover, 비용, 백업/복구 테스트가 필요하다.
- replica는 exactly-once 처리를 보장하지 않으며 애플리케이션 idempotency가 별도로 필요하다.

테스트 환경에서는 primary만 먼저 생성하는 cost profile을 지원하는 것이 바람직하다. 현재 구성은 primary/replica가 unconditional이라 전체 apply 전에 환경별 feature flag를 추가하는 후속 작업이 필요하다.

## 6. 네트워크와 비용

### 결정

운영형 네트워크를 고려해 private app/db subnet, public subnet, NAT Gateway 2개를 계획했다. 하지만 테스트에서는 전체 인프라를 한 번에 만들지 않고 CI/CD bootstrap과 application infrastructure를 분리한다.

### 비용 위험

전체 Terraform plan은 74개 리소스이며 EKS control plane, node group 2대, NAT Gateway 2개, RDS primary/replica, ALB가 포함된다. 테스트 수명주기가 짧다면 이 구조는 기능 검증 대비 비용이 크다.

### 권장 운영

- CI/CD bootstrap: ECR, GitHub OIDC role만 먼저 생성
- short-lived test: 단일 NAT, 최소 node count, primary-only DB profile
- 종료: `terraform destroy` 후 EIP, load balancer, ECR image, CloudWatch 로그 잔여물까지 확인
- 비용 경보: AWS Budgets와 예상 월말 비용 알람 설정

## 7. Terraform state

### 결정

Terraform state는 원격 backend와 locking을 사용해야 한다. 로컬 state만 사용한 현재 복구 과정에서 CloudShell 저장공간 부족으로 리소스 생성과 state 저장이 분리되는 문제가 실제로 발생했다.

### 운영 기준

- bootstrap backend를 먼저 생성한다.
- state bucket은 versioning과 public access block을 활성화한다.
- state lock을 사용해 동시 apply를 방지한다.
- plan artifact와 state를 같은 수명주기로 관리하지 않는다.
- 비밀번호 같은 sensitive 값은 state에도 남을 수 있으므로 backend 접근 권한을 최소화한다.

## 8. 관측 데이터와 canary 판정

### 결정

Flask 애플리케이션은 bounded route label을 가진 Prometheus metrics를 제공하고, Kubernetes는 stable/canary ServiceMonitor를 분리한다. Argo Rollouts는 Prometheus에서 canary 5xx 비율과 응모 API p95를 조회한 뒤 단계별로 진행하거나 자동 중단한다.

### 이유

이미지 push 성공이나 readiness 통과만으로는 새 버전이 사용자 요청을 정상 처리한다는 것을 증명할 수 없다. 배포 중 실제 canary 트래픽의 오류율과 지연시간을 확인해야 rollback 기준이 데이터에 연결된다.

### 안전장치

- `/metrics`는 internet-facing ALB에서 404 fixed response로 차단한다.
- Prometheus가 설치되지 않았거나 canary 데이터가 없으면 analysis를 성공으로 간주하지 않는다.
- Alertmanager webhook을 Terraform 변수로 명시하지 않으면 monitoring stack을 apply할 수 없다.
- 현재 analysis 주소와 ServiceMonitor label은 release 이름에 의존하므로 stack release 이름을 변경할 때 함께 검토한다.

## 9. 부하 테스트의 증거 기준

### 결정

k6 시나리오는 health/readiness와 one-shot 응모를 분리하고, 결과에는 커밋 SHA·이미지 SHA·부하·지속시간·p50/p95/p99·HPA·DB 연결·replica lag·정확성 결과를 함께 기록한다.

### 이유

최고 순간 처리량 하나는 지속 가능한 용량이나 SLO 준수를 설명하지 못한다. 연속 구간에서 SLO를 통과한 가장 높은 부하를 용량 하한으로 보고, 실패 지점과 원인을 별도로 기록해야 capacity planning과 장애 대응에 재사용할 수 있다.

## 10. 단기 검증 비용 프로필 분리

### 결정

로봇 데이터 파이프라인의 스트리밍 SLO 검증에는 전체 EKS 플랫폼 스택을 사용하지 않고 `terraform/validation` 전용 프로필을 사용한다. 이 프로필은 Kinesis → Firehose → S3 Parquet과 CloudWatch SLO만 검증하며, 검증이 끝나면 즉시 destroy한다.

### 변경 전후

| 항목 | 기존 전체 스택 | 단기 검증 프로필 | 효과 |
|---|---:|---:|---|
| Terraform plan 신규 리소스 | 104개 | 14개 | 약 86.5% 감소 |
| EKS/EC2/NAT/ALB/ECR/RDS | 생성 | 생성하지 않음 | 고정 시간비용 제거 |
| Kinesis main shard | 4개 + alert 1개 | main 2개 | shard 고정비 60% 감소 |
| Firehose buffer | 128MB/300초 | 64MB/60초 | Parquet 변환을 유지하면서 freshness 피드백 최대 4분 단축 |
| Slack/Lambda 알림 | Secret·Lambda 필요 | 검증 프로필에서 비활성 | Secret 의존성·정리 누락 제거 |
| GitHub OIDC Provider | stack이 생성 시도 | 계정 공용 Provider를 data source로 읽기 | EntityAlreadyExists 재발 방지 |

### 비용과 속도 근거

기존 전체 스택의 월 환산 고정/저변동 비용은 약 `$218`이었고, 4시간 검증 안전 예산은 `$4~$10`으로 추정했다. 단기 프로필은 EKS control plane, worker, NAT, ECR, ALB가 없어 100Hz 단계 검증 기준 약 `$1~$3`으로 예산을 낮춘다. 1,000Hz를 4시간 내내 유지하면 Firehose 데이터량 비용이 지배하므로 약 `$4~$6`으로 별도 상한을 둔다.

이번 실제 중단 사례에서 EKS 생성은 약 4분 50초, NAT Gateway 생성은 약 1분 59초가 걸렸다. 단기 프로필은 두 리소스를 생성하지 않으므로 해당 대기 구간을 제거한다. 실제 저비용 실행에서는 부분 state의 corrected apply 3개 리소스가 7.9초, 14개 destroy가 41.6초였으며, 빈 계정 기준 전체 apply 시간은 비용 상한 때문에 재실행하지 않았다.

### 트레이드오프

단기 프로필은 Kubernetes workload, HPA, ALB, RDS 복제 지연, Canary, Slack 전송을 검증하지 않는다. 이 항목들은 비용 승인 후 별도의 EKS 애플리케이션 프로필에서 실행해야 하며, 스트리밍 SLO 검증 결과와 섞어 주장하지 않는다.

## 11. 지원 역량을 구현 증거와 검증 경계로 번역

| SOOP/Prime Career 축 | 구현·증거 | 말할 수 있는 범위 |
|---|---|---|
| 성능·안정성의 지표화 | Prometheus SLI/SLO, Kinesis iterator age/throttle, Firehose freshness guardrail, capacity 문서 | streaming path 단기 AWS 검증 |
| 변경 사전 차단·복구 | GitHub OIDC, SHA 이미지, Kustomize/Argo Rollouts analysis, Terraform cost precondition, failure drill | 코드·CI·runbook 구현 |
| 멀티리전·멀티CDN·미디어 | 3-region/2-CDN topology와 outage failover lab | 로컬 simulation; 실제 CDN 운영은 미검증 |

세 번째 축은 향후 synthetic segment probe, cache hit ratio, rebuffering, origin error, DNS/Anycast 전환시간을 실제 계층에서 측정해야 production claim으로 승격할 수 있다.
## 12. 실제 short-lived 애플리케이션 검증 프로필

2026-08-24에는 추정만 남기지 않고 `eu-west-1`의 EKS/RDS/ALB를 실제로 올려 API 경로와 장애 복구를 측정했다. 비용과 장애 범위를 제한하기 위해 EKS worker 1개(t3.medium), RDS `db.t3.micro` primary-only, HPA 2~4 replicas, Prometheus/Grafana 1개 stack을 사용했다. read replica, Multi-AZ, NAT 이중화는 이번 실험의 목표가 아니므로 만들지 않았다.

이 선택은 고가용성의 증거가 아니라 변경·관측·복구 경로의 최소 재현이다. 따라서 결과에서 RDS replica lag, Multi-AZ failover, node-level eviction을 주장하지 않는다.

## 13. HPA 상한과 scale rate를 비용 예산에 포함

단일 검증 노드는 system/observability Pod가 이미 Pod capacity를 사용한다. CPU 기반 HPA의 `maxReplicas=20`을 그대로 두면 순간 부하가 11 replicas를 요구하면서 7개가 Pending이 될 수 있었다. 검증 profile은 `maxReplicas=4`, scale-up/down 각 1 Pod/60초로 제한했다.

운영 profile에서 더 높은 용량이 필요하면 HPA 상한만 올리지 않고 다음을 함께 변경한다.

- node autoscaling capacity와 Pod IP 상한
- RDS connection budget과 DB CPU
- ALB target health 및 canary traffic
- scale-up 폭주를 막는 stabilization window
- 부하 단계별 비용 상한

## 14. Canary 분석은 메트릭 부재를 성공으로 간주하지 않음

Argo Rollouts AnalysisTemplate은 canary 5xx 비율과 `/api/apply` p95를 Prometheus에서 조회한다. 실제 실패훈련에서 canary metric vector가 아직 생성되지 않자 AnalysisRun이 Error가 되었고 stable revision으로 자동 rollback했다. 이 fail-closed 정책은 잘못된 배포를 통과시키지 않는 장점이 있지만, 정상 운영 배포에는 synthetic canary traffic과 metric warm-up/no-data 판정을 runbook으로 묶어야 한다.

stable/canary Service가 동일한 Pod hash를 가리키는 Rollout 완료 상태에서는 ServiceMonitor가 같은 Pod를 두 번 스크랩할 수 있다. Dashboard와 alert query는 `max by (pod, ...)` 후 집계하도록 바꾸어 정확성 지표의 중복을 제거했다.

## 15. 실제 검증 결과를 용량 하한으로만 사용

동일 이미지와 primary-only RDS 환경에서 readiness 20 req/s를 30초 유지했을 때 601/601 요청 성공, p50 약 320ms, p95 354.5ms, p99 406.5ms였다. 50 req/s 단계는 HTTP 오류 0%였지만 20개 arrival iteration이 drop됐으므로 지속 가능 용량으로 인정하지 않았다. 응모 one-shot 30 VU에서는 회원가입·로그인·응모 90개 요청이 모두 성공했고, `/api/apply` p95/p99는 약 510.9/534.1ms였다.

따라서 현재 환경에서 말할 수 있는 것은 “20 req/s readiness 구간은 통과했고 50 req/s는 도착률 포화 신호를 보였다”이지, 일반 사용자 트래픽의 production capacity가 아니다. 다음 단계는 connection pooling, DB connection/lock metrics, 더 긴 steady-state 구간과 replica-enabled 비교다.
