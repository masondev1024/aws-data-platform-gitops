# Lessons Learned

> 기준일: 2026-08-24

## 1. “CI/CD test” 커밋은 배포 완료가 아니다

workflow 파일이 존재하는 것과 실제로 운영 가능한 pipeline인 것은 다르다. 경로, 권한, secret, image context, cluster target, manifest render를 각각 독립적으로 검증해야 한다.

## 2. 실패는 가장 작은 재현 단위로 줄여야 한다

전체 pipeline을 반복 실행하기보다 다음 순서가 빠르고 안전했다.

1. pytest/compileall
2. Docker build
3. Kustomize render
4. Terraform init/fmt/validate
5. AWS identity 검증
6. Terraform plan
7. 최소 bootstrap apply

이 순서를 지키면 AWS 리소스를 만들지 않고도 대부분의 구조적 오류를 제거할 수 있다.

## 3. AWS 계정, 리전, 주체는 항상 로그로 확인한다

사람이 콘솔에서 올바르게 로그인했다고 느끼는 것만으로는 부족하다. 작업 직전에 다음을 확인해야 한다.

```bash
aws sts get-caller-identity
aws configure get region
```

특히 Root, IAM user, IAM Identity Center permission set은 ARN과 권한 경계가 다르다.

## 4. 임시 권한이 장기 자격 증명보다 안전하다

GitHub Actions에는 access key를 저장하지 않고 OIDC role을 사용한다. 개발자 CLI도 IAM Identity Center SSO profile을 사용한다. Root password, access key, MFA code는 터미널 로그·스크린샷·Git history에 남기지 않는다.

## 5. Plan보다 state가 먼저다

`terraform plan`이 성공해도 apply 결과를 기록할 state 저장소가 불안정하면 운영 신뢰성이 없다. 특히 CloudShell처럼 용량이 제한된 환경에서 provider cache와 state 저장공간을 별도로 고려해야 한다.

## 6. `-target`은 정상 배포 전략이 아니다

이번에는 CI/CD bootstrap을 분리하기 위해 예외적으로 사용했다. 그러나 target plan은 전체 의존성 그래프를 대표하지 않는다. bootstrap이 끝난 뒤에는 전체 plan으로 drift와 누락 리소스를 다시 검증해야 한다.

## 7. 비용은 장애와 같은 운영 리스크다

EKS, NAT Gateway, RDS replica, ALB는 테스트가 끝나도 자동으로 사라지지 않는다. 테스트 환경에는 생성과 종료를 하나의 runbook으로 묶고, 예상 비용과 teardown owner를 기록해야 한다.

## 8. 데이터 플랫폼은 “실행됨”보다 “검증됨”이 중요하다

애플리케이션이 뜨는 것만으로는 충분하지 않다. 운영 단계에서는 freshness, completeness, duplicate, schema evolution, lineage, retry/backfill, alerting을 데이터 계약에 포함해야 한다.

## 9. 다음 개선 항목

- Terraform remote backend와 lock 구성
- `test`/`prod` 비용 profile 분리
- RDS replica와 NAT 개수 feature flag 도입
- Kubernetes Secret을 AWS Secrets Manager/External Secrets로 전환
- GitHub Actions role을 ECR push와 Terraform plan/apply role로 분리
- CloudWatch/GitHub/Argo CD 알림과 SLO 정의
- 실제 ingestion 데이터에 대한 data quality check 및 backfill runbook 추가
- GitHub OIDC trust subject에 저장소 이름 대신 안정적인 owner/repository ID 사용
- 메트릭을 추가할 때 공개 ALB 경로와 내부 scrape 경계를 함께 설계
- canary 자동 롤백은 Prometheus 설치·ServiceMonitor·알림 수신자까지 준비된 뒤 활성화
- 부하 테스트 결과가 없으면 처리량과 SLA를 주장하지 않고, 측정 템플릿을 먼저 커밋

## 10. 전체 스택을 검증용으로 재사용하면 비용과 실패 범위가 커진다

로봇 파이프라인에 대해 기존 Terraform 전체 plan을 실행했을 때 104개 리소스가 생성 대상이었다. EKS control plane과 NAT Gateway 생성 중 GitHub OIDC Provider 중복 생성 충돌이 발생했고, 적용을 중단한 뒤 39개 부분 생성 리소스를 destroy해야 했다. EKS 생성에는 약 4분 50초, NAT Gateway에는 약 1분 59초가 걸렸다.

이 경험을 바탕으로 다음 원칙을 적용했다.

- Kinesis → Firehose → S3 Parquet과 SLO만 확인할 때는 14개 리소스의 `terraform/validation` 프로필을 사용한다.
- EKS, EC2, NAT, ALB, ECR, RDS, SageMaker, Slack/Lambda를 단기 스트리밍 검증에서 제외한다.
- Kinesis main shard는 4개에서 2개로 줄이고, Firehose buffer는 Parquet 변환 제약 때문에 128MB/300초에서 64MB/60초로 줄인다. 5MB는 raw JSON 검증에서만 가능하다.
- 비용 예측은 “고정 인프라 비용”과 “Firehose/Kinesis 데이터량 비용”을 분리한다. 1,000Hz 데이터량을 오래 유지하면 인프라를 줄여도 데이터 비용이 남는다.
- `plan`의 리소스 수와 실제 apply/destroy 시간을 함께 기록해, 비용 절감을 성능 저하와 혼동하지 않는다.

단기 프로필의 예상 비용은 100Hz 중심 4시간 기준 `$1~$3`, 1,000Hz 연속 4시간 기준 `$4~$6`이다. 실제 실행에서는 100Hz 약 72초 동안 7,200건을 전송했고 실패 0건, Parquet 객체 2개, Kinesis throttle 0을 확인했다. Firehose CloudWatch metric은 같은 시점 `NO_DATA`였으므로 SLO PASS로 과장하지 않았다. 14개 리소스 destroy는 41.6초에 완료했고 Cost Explorer read-only 조회는 `Estimated=true`, `$0`이었다.

## 11. 계정 공용 리소스는 workload state가 소유하지 않는다

GitHub OIDC Provider는 계정 단위 공유 리소스인데 애플리케이션 Terraform이 생성하려 해 `EntityAlreadyExists`가 발생했다. 이후 workload stack에서는 OIDC Provider를 data source로 읽고, bootstrap stack이 소유하도록 경계를 분리했다. 이 원칙은 ECR registry, VPC Endpoint, IAM Identity Center와 같은 계정 공용 리소스에도 동일하게 적용한다.

## 12. 지원서의 숫자는 검증 범위와 함께 쓴다

SOOP SRE Engineer 공고의 핵심은 성능·안정성 지표, 변경 안전성과 복구, 멀티리전·멀티CDN 관점이다. 현재 레플 프로젝트에서는 실제 application EKS/RDS p95·p99와 canary RTO를 아직 실행하지 않았으므로 수치를 만들어 쓰지 않는다. 대신 로컬 CI, Terraform precondition, Argo Rollouts 설정, 장애 runbook을 구현 증거로 제시하고, 로봇 프로젝트에서는 실제 단기 AWS streaming 증거와 무비용 edge failover simulation을 분리한다.

- 실제 AWS: 100Hz, 7,200 records, 실패 0, Parquet 2개, Kinesis throttle 0, destroy 41.6초
- 로컬 결정론 lab: 3,600 requests, 2초 failover RTO, failover 후 추가 실패 0, `cdn-a → cdn-b`
- 미검증: 실제 멀티리전 CDN, 라이브 미디어 segment/rebuffering, application HTTP p95/p99, RDS replica lag

면접에서는 “운영했다”와 “정책을 구현·검증했다”를 구분해야 신뢰성을 잃지 않는다.

## 18. ECR 이미지는 Terraform destroy의 숨은 의존성이다

ECR repository가 Terraform state에 있어도 이미지가 하나라도 남아 있으면 기본 repository delete가 실패한다. 단기 실험 스택은 이미지 보존보다 teardown 완결성이 중요하므로 `force_delete = true`를 선언하고, destroy runbook에 ECR repository와 image manifest 잔여 확인을 포함한다. 실제 Robot teardown에서 이 문제가 한 번 발생했고, repository를 정리한 뒤 state refresh로 최종 0 resource를 확인했다.

## 19. teardown도 실험의 성공 조건이다

애플리케이션이 정상 동작한 것만으로 실험을 완료하지 않는다. Terraform destroy 결과, EKS/RDS/ALB/Kinesis/Firehose/ECR/S3/Secrets Manager/VPC/NAT/EIP 잔여, state resource count, Cost Explorer `Estimated` 상태를 함께 기록해야 한다. 이번 검증은 두 stack의 state를 각각 0개로 수렴시키고 AWS 전용 리소스가 absent인지 확인한 뒤 종료했다.
## 13. 실제 부하에서는 “오류율 0%”만 보면 안 된다

readiness 50 req/s에서 HTTP error는 0%였지만 k6 arrival iteration 20개가 drop됐다. 요청이 서버에 도착하지 못한 포화도 신호이므로, capacity report에는 `http_req_failed`와 `dropped_iterations`를 함께 기록해야 한다. 현재 검증에서 20 req/s는 30초 동안 p95 354.5ms/p99 406.5ms로 통과했고, 50 req/s는 지속 가능 용량으로 인정하지 않았다.

## 14. HPA 설정은 노드와 함께 설계해야 한다

HPA가 정상적으로 CPU를 읽어도 node autoscaling이 없으면 Pending Pod가 된다. 단일 t3.medium 검증 profile에서는 max replicas와 scale rate를 비용 가드레일로 제한하고, production profile에서는 Karpenter/node capacity·Pod IP·RDS connection budget을 함께 검증해야 한다.

## 15. Canary 분석의 no-data는 운영 의사결정이다

메트릭이 없을 때 AnalysisRun을 성공으로 처리하면 관측 불능 배포가 통과한다. 이번 실험은 Prometheus query가 빈 결과를 반환하자 Rollout을 자동 중단하고 stable revision으로 복귀했다. 다음 개선은 no-data를 명시적 `Inconclusive`로 표시하고, synthetic canary traffic으로 정상 분석을 보장하는 것이다.

## 16. ServiceMonitor가 만든 중복은 데이터 품질 문제다

Rollout 완료 후 stable/canary Service가 같은 Pod hash를 가리킬 수 있어 단순 Prometheus `sum`은 카운터를 중복 집계했다. 대시보드와 alert는 `max by (pod, ...)` 후 합산해야 하며, metric query 자체를 데이터 계약의 일부로 관리해야 한다.

## 17. 로컬 Flink 구현은 runtime source와 계약을 분리해야 한다

Git 이력의 PyFlink SQL 구조는 확인할 수 있었지만 현재 AWS 계정에는 Managed Flink application이 없었다. 그래서 배포 가능한 가짜 Flink 소스를 새로 만들지 않고, Studio Notebook을 runtime source로 명시한 순수 이상치 계약·테스트·Kinesis black-box validator를 추가했다. 실제 Notebook이 다시 실행되면 validator로 z-score branch, 다변량 branch, watermark/window, alert KDS sink를 확인한다.

역사적으로 `85°C/1.8`과 현재 smoke/Notebook tuning의 `92°C/2.5`가 문서에 혼재했으므로, threshold는 코드에 묻지 않고 `FLINK-ANOMALY-CONTRACT.md`에서 출처와 변경 이력을 함께 관리해야 한다.
