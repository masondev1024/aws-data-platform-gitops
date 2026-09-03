# AWS Data Platform GitOps

기존 래플 트래픽 응모 시스템에서 구현하지 못했던 운영 인프라와 배포 자동화를 확장한 프로젝트입니다. AWS와 Terraform을 기반으로 EKS, ECR, GitHub Actions OIDC, Argo CD GitOps, Argo Rollouts canary 배포 흐름을 구성했습니다.

## CI/CD

`main`에 변경 사항이 들어오면 다음 검증이 실행됩니다.

- Python 애플리케이션 테스트와 Docker 이미지 빌드
- `k8s/overlays/prod` Kustomize 렌더링
- Terraform provider 초기화, 포맷 검사, validate

애플리케이션 변경 시 CD workflow가 `app/`을 `eu-west-1` ECR에 SHA 태그로 빌드·push하고, `k8s/overlays/prod/kustomization.yaml`의 이미지 태그를 갱신합니다. Argo CD는 `main`의 `k8s/overlays/prod`를 감시하고 Argo Rollouts canary 절차를 수행합니다.

## GitHub 설정

Terraform 적용 후 출력되는 `github_actions_role_arn` 값을 CD용 저장소 변수로 등록합니다.

```text
Settings → Secrets and variables → Actions → Variables
AWS_ROLE_TO_ASSUME=<terraform output github_actions_role_arn>
```

Terraform plan은 CD role과 분리된 별도 role을 사용해야 합니다. 전체 인프라 plan 권한을 부여한 role을 만들었을 때만 다음 변수를 추가합니다.

```text
AWS_TERRAFORM_ROLE_TO_ASSUME=<separate terraform plan role ARN>
```

Terraform plan을 실행하려면 다음 저장소 secret도 등록해야 합니다.

```text
TF_VAR_DB_PASSWORD=<16자 이상인 새 RDS 비밀번호>
```

`AWS_TERRAFORM_ROLE_TO_ASSUME`이 없거나 pull request인 경우 Terraform cloud plan은 의도적으로 건너뛰고 정적 검증만 수행합니다. CD는 `AWS_ROLE_TO_ASSUME`이 없으면 명확한 오류로 중단됩니다.

## 로컬 검증

```bash
python3 -m pip install -r app/requirements-dev.txt
python3 -m pytest -q app/tests
docker build --pull --tag data-pipeline-app-ci ./app
kubectl kustomize k8s/overlays/prod >/tmp/rendered-production.yaml

cd terraform
terraform init -backend=false -input=false
terraform fmt -check -recursive
terraform validate
```

## 비용 안전 게이트

기본 Terraform 프로필은 EKS·EC2·NAT·ALB·RDS를 포함하므로 비용 승인 없이 실행되지 않도록 `allow_full_stack_apply=false`가 기본값입니다. 전체 애플리케이션 인프라를 실제로 검증할 때만 종료 담당자와 비용 상한을 정한 뒤 다음 변수를 명시합니다.

```bash
cd terraform
terraform plan -var='allow_full_stack_apply=true'
```

테스트 비용을 줄이기 위해 기본값은 EKS worker 1대(`t3.medium`), NAT Gateway 1개, RDS primary-only·Single-AZ로 조정했습니다. 고가용성 검증이 필요한 경우에만 `enable_multi_az_nat=true`, `enable_rds_replica=true`, `enable_rds_multi_az=true`, node 수 증가를 별도로 선택합니다. RDS Multi-AZ는 동기 standby와 failover drill을 위한 명시적 비용 선택이며, 전체 선택 근거와 실제 측정값은 [`docs/data-engineering-decisions.md`](docs/data-engineering-decisions.md), [`docs/lessons-learned.md`](docs/lessons-learned.md), [`docs/failure-drills.md`](docs/failure-drills.md)에 기록합니다.

## 배포 전 조건

클러스터에는 `raffle-config` ConfigMap과 `raffle-secret` Secret이 미리 있어야 합니다. 애플리케이션은 다음 환경 변수를 사용합니다.

- ConfigMap: `DB_WRITER_HOST`, `DB_READER_HOST`, `DB_NAME`, `DB_USER`
- Secret: `DB_PASSWORD`, `SECRET_KEY`

기존 Terraform 코드에 평문으로 있던 RDS 비밀번호는 제거했습니다. 이전 값이 Git 이력에 남아 있으므로 실제 AWS 환경에서 즉시 비밀번호를 회전해야 합니다.
