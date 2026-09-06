# AWS Data Platform GitOps

기존 래플 트래픽 응모 시스템에서 구현하지 못했던 운영 인프라와 배포 자동화를 확장한 프로젝트입니다. AWS와 Terraform을 기반으로 EKS, ECR, GitHub Actions OIDC, Argo CD GitOps, Argo Rollouts canary 배포 흐름을 구성하고, 응모 승인과 이벤트 발행의 정합성을 transactional outbox로 보장합니다.

핵심 운영 가설은 `응모 승인 → 동일 DB 트랜잭션의 outbox 기록 → DB 기준 parity SLI → canary gate → 실패 시 stable 복귀`입니다. 애플리케이션 카운터의 단순 차이가 아니라 writer DB에서 최근 응모와 outbox row를 직접 대조하며, 측정 불능은 `-1`로 표시되어 canary를 fail-closed 합니다.

## CI/CD

`main`에 변경 사항이 들어오면 다음 검증이 실행됩니다.

- Python 애플리케이션 테스트와 Docker 이미지 빌드
- `k8s/overlays/prod` Kustomize 렌더링
- Terraform provider 초기화, 포맷 검사, validate
- Python dependency audit, Bandit, Trivy IaC/secret/image gate, CodeQL

애플리케이션 변경 시 CD workflow가 `app/`을 `eu-west-1` ECR에 SHA 태그로 빌드·push하고, `k8s/overlays/prod/kustomization.yaml`의 이미지 태그를 갱신합니다. Argo CD는 `main`의 `k8s/overlays/prod`를 감시하고 Argo Rollouts canary 절차를 수행합니다.

## GitHub 설정

Terraform 적용 후 출력되는 `github_actions_role_arn` 값을 CD용 저장소 변수로 등록합니다.

```text
Settings → Secrets and variables → Actions → Variables
AWS_ROLE_TO_ASSUME=<terraform output github_actions_role_arn>
```

Terraform plan은 CD role과 분리된 별도 role을 사용해야 합니다. 전체 인프라 plan 권한과 EKS bootstrap CIDR을 정했을 때만 다음 변수를 추가합니다.

```text
AWS_TERRAFORM_ROLE_TO_ASSUME=<separate terraform plan role ARN>
CLUSTER_API_ALLOWED_CIDRS='["<your-public-ip>/32"]'
```

Terraform plan을 실행하려면 다음 저장소 secret도 등록해야 합니다.

```text
TF_VAR_DB_PASSWORD=<16자 이상인 새 RDS 비밀번호>
```

`AWS_TERRAFORM_ROLE_TO_ASSUME` 또는 `CLUSTER_API_ALLOWED_CIDRS`가 없거나 pull request인 경우 Terraform cloud plan은 의도적으로 건너뛰고 정적 검증만 수행합니다. CD는 `AWS_ROLE_TO_ASSUME`이 없으면 명확한 오류로 중단됩니다.

## 로컬 검증

```bash
python3 -m pip install -r app/requirements-dev.txt
python3 -m pytest -q app/tests
python3 -m pytest -q app/tests scripts/tests
docker build --pull --tag data-pipeline-app-ci ./app
kubectl kustomize k8s/overlays/prod >/tmp/rendered-production.yaml

python scripts/scaffold_service.py \
  --name catalog-api \
  --owner team-d2c-platform \
  --output-dir /tmp/catalog-api
python -m pytest -q /tmp/catalog-api/app/tests
kubectl kustomize /tmp/catalog-api/k8s/base >/tmp/rendered-golden-path.yaml

cd terraform
terraform init -backend=false -input=false
terraform fmt -check -recursive
terraform validate
```

## 비용 안전 게이트

기본 Terraform 프로필은 EKS·EC2·NAT·ALB·RDS를 포함하므로 비용 승인 없이 실행되지 않도록 `allow_full_stack_apply=false`가 기본값입니다. 전체 애플리케이션 인프라를 실제로 검증할 때만 종료 담당자와 비용 상한을 정한 뒤 다음 변수를 명시합니다.

```bash
cd terraform
terraform plan \
  -var='allow_full_stack_apply=true' \
  -var='cluster_endpoint_public_access=true' \
  -var='cluster_api_allowed_cidrs=["<your-public-ip>/32"]'
```

EKS API는 기본적으로 private endpoint이며, 로컬에서 Terraform/Helm bootstrap을 수행할 때만 `cluster_endpoint_public_access=true`와 `CLUSTER_API_ALLOWED_CIDRS`를 함께 지정합니다. Terraform과 Helm을 VPC 내부 SSM runner에서 실행하는 운영형 경로는 private endpoint를 그대로 사용합니다. CIDR 없이 public endpoint를 열 수 없도록 validation에서 fail-closed 합니다.

테스트 비용을 줄이기 위해 기본값은 EKS worker 1대(`t3.medium`), NAT Gateway 1개, RDS primary-only·Single-AZ로 조정했습니다. 고가용성 검증이 필요한 경우에만 `enable_multi_az_nat=true`, `enable_rds_replica=true`, `enable_rds_multi_az=true`, node 수 증가를 별도로 선택합니다. RDS Multi-AZ는 동기 standby와 failover drill을 위한 명시적 비용 선택이며, 전체 선택 근거와 실제 측정값은 [`docs/data-engineering-decisions.md`](docs/data-engineering-decisions.md), [`docs/lessons-learned.md`](docs/lessons-learned.md), [`docs/failure-drills.md`](docs/failure-drills.md)에 기록합니다.

## 배포 전 조건

클러스터에는 `raffle-config` ConfigMap과 `raffle-secret` Secret이 미리 있어야 합니다. 애플리케이션은 다음 환경 변수를 사용합니다.

- ConfigMap: `DB_WRITER_HOST`, `DB_READER_HOST`, `DB_NAME`, `DB_USER`
- Secret: `DB_PASSWORD`, `SECRET_KEY`

기존 Terraform 코드에 평문으로 있던 RDS 비밀번호는 제거했습니다. 이전 값이 Git 이력에 남아 있으므로 실제 AWS 환경에서 즉시 비밀번호를 회전해야 합니다. 운영자 접속은 기본적으로 public SSH가 아니라 private subnet의 SSM 경로를 사용하며, SSH가 필요할 때만 `allowed_ssh_location`에 제한된 CIDR을 명시합니다.

## IDP 골든패스

[`platform/golden-path`](platform/golden-path)는 이 저장소의 배포 패턴을 다른 서비스가 재사용할 수 있게 만든 첫 번째 IDP slice입니다. 스캐폴더는 서비스 소유자, Backstage catalog metadata, CI, 비-root 컨테이너, health probe, Kustomize, Argo Rollouts canary, Prometheus error-rate/p95 latency 분석 템플릿을 한 번에 생성합니다.

이것은 Backstage 전체 설치를 이미 운영한다는 주장이 아니라, 내부 개발자 플랫폼으로 승격할 수 있는 실행 가능한 service template과 계약입니다. 다음 확장 단계는 중앙 reusable workflow와 Argo CD ApplicationSet에 연결하는 것입니다.

## 검증된 운영 증거와 범위

- EKS/Argo CD/Argo Rollouts canary, k6 부하 검증, RDS failover, soak 및 teardown 결과는 [`docs/`](docs/)와 `evidence/`에 기록되어 있습니다.
- 현재 기본 Terraform 프로필은 비용 보호를 위해 full-stack apply가 차단되어 있으며, 검증이 끝난 AWS 리소스는 상시 유지하지 않습니다.
- Kafka relay/consumer와 S3 lakehouse 흐름은 별도 [`d2c-event-data-platform`](https://github.com/masondev1024/d2c-event-data-platform) 저장소에서 운영 설계와 로컬 검증 증거를 관리합니다. 이 저장소는 그 앞단의 실제 D2C 서비스 승인 경계와 배포 플랫폼 증거를 담당합니다.

## 포트폴리오 핵심 시나리오

이 프로젝트는 개별 애플리케이션을 배포한 사례가 아니라, 개발팀이 반복해서
사용할 수 있는 플랫폼 제품의 작은 수직 슬라이스입니다.

1. `scripts/scaffold_service.py`로 소유자·CI·보안 기본값·canary 리소스를 갖춘
   새 서비스를 생성합니다.
2. PR에서 테스트·dependency audit·IaC/secret/image scan·manifest render를
   통과시킵니다.
3. 승인 요청은 DB row와 transactional outbox event를 같은 트랜잭션으로 기록하고,
   writer DB parity SLI가 실제 데이터 정합성을 측정합니다.
4. Argo Rollouts는 HTTP 5xx·p95 latency·도메인 무결성 parity를 기준으로 canary를
   승격하거나 stable로 자동 복귀시킵니다.
5. 승인된 outbox는 별도 Kafka 이벤트 플랫폼에서 계약 검증·중복 제거·DLQ를 거쳐
   S3 Parquet/Iceberg 계층으로 적재됩니다.

따라서 면접에서는 “Kafka를 사용했다”가 아니라, 내부 개발자 경험·배포 안전성·
트랜잭션 정합성·데이터 레이크 소비까지 하나의 운영 경계로 설계한 이유와 실패 시
복구 경로를 시연할 수 있습니다.
