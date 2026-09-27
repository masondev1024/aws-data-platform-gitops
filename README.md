# AWS Data Platform GitOps

기존 래플 트래픽 응모 시스템에서 구현하지 못했던 운영 인프라와 배포 자동화를 확장한 프로젝트입니다. AWS와 Terraform을 기반으로 EKS, ECR, GitHub Actions OIDC, Argo CD GitOps, Argo Rollouts canary 배포 흐름을 구성하고, 응모 승인과 이벤트 발행의 정합성을 transactional outbox로 보장합니다.

핵심 운영 가설은 `응모 승인 → 동일 DB 트랜잭션의 outbox 기록 → DB 기준 parity SLI → canary gate → 실패 시 stable 복귀`입니다. 애플리케이션 카운터의 단순 차이가 아니라 writer DB에서 최근 응모와 outbox row를 직접 대조하며, 측정 불능은 `-1`로 표시되어 canary를 fail-closed 합니다.

## 플랫폼 운영 검증

`scripts/verify_platform.sh`는 테스트, 매니페스트 렌더링, backendless Terraform 검사, Python 보안 검사를 실행합니다. Jenkins는 신뢰한 로컬 소스에서 승인된 비특권 검증 subset만 수행하며, 이미지 게시와 배포는 GitHub Actions 경로에 남습니다. 검증 통과나 배포 후보 생성만으로 변경 승인 또는 실제 배포를 입증하지 않습니다.

`platform/governance/`는 validation namespace에 한정한 배포·조회 권한 구성을 담습니다. `platform/live-lab/`은 계정·리전·세션 범위를 지정하는 임시 AWS 검증 구성이며, 실행 전 `platform/live-lab/scripts/estimate_session_cost.py`로 승인된 비용 계획을 확인하고 종료·정리 절차를 준비해야 합니다. 비용 추정은 AWS가 강제하는 지출 상한이 아니며, 정적 검사나 로컬 렌더링은 실환경 실행 증거가 아닙니다. 원본 실행 기록, 개인 작업 기록, 자격 증명은 Git 추적에서 제외해 로컬에만 보관합니다.

검증 환경의 GitOps 경로는 `k8s/overlays/validation`입니다. DB 주소·인증 정보·RDS CA·세션별 Ingress는 플랫폼 부트스트랩이 준비하고, 앱·PreSync 마이그레이션·정기 작업은 Argo CD만 배포합니다. `platform/live-lab/scripts/deploy_gitops_validation.py`는 `--execute`를 지정하기 전에는 앱을 변경하지 않으며, 실행 시 계정·클러스터 소유 태그·main SHA·고정 이미지 digest와 `--bundle-dir`의 CI 서명을 재검증합니다. 반환하는 `sync_requested`는 배포 성공이 아닙니다. 이후 `observe_gitops_as_developer.py`로 실제 단기 조회 계정을 발급해 실행 버전을 확인하고 HTTP 응답·DB 정합성·canary 복구를 별도로 검증해야 합니다.

현재 임시 검증 프로필은 생성·정리를 포함해 최대 3시간, USD 1 예비비를 포함한 USD 5.50 이하로 제한합니다. 종료 감시기는 시작 후 2시간 이내에 정리를 시작하고, 검증이 먼저 끝나면 즉시 정리합니다. 새 DB는 표준 지원 중인 MySQL 8.4를 사용하며 유료 연장 지원 자동 가입을 차단합니다. 클라우드 청구 지연이나 정리 실패까지 강제 차단하는 하드캡은 아닙니다.

## CI/CD

`main`에 변경 사항이 들어오면 다음 검증이 실행됩니다.

- Python 애플리케이션 테스트와 Docker 이미지 빌드
- `k8s/overlays/prod` Kustomize 렌더링
- Terraform provider 초기화, 포맷 검사, validate
- Python dependency audit, Bandit, Trivy IaC/secret/image gate, CodeQL

애플리케이션 변경 시 CD workflow는 같은 커밋의 공통 검증을 통과한 뒤 이미지를 빌드합니다. 빌드한 이미지에 Trivy 검사를 수행한 후 `eu-west-1` ECR에 SHA 태그로 발행하고, digest가 고정된 배포 후보와 검증 기록을 artifact로 남깁니다. 기존 SHA 태그가 있으면 해당 digest를 다시 검사하며 덮어쓰지 않습니다. CD가 `main`이나 운영 매니페스트를 직접 수정하지는 않습니다. 후보를 검토해 별도 PR로 반영한 뒤 Argo CD가 `main`의 `k8s/overlays/prod` 변경을 감지하고 Argo Rollouts canary 절차를 수행합니다. 이미지 발행 성공과 실제 배포 성공은 별도로 확인해야 합니다.

런타임 이미지는 Python 3.13 slim builder와 digest로 고정한 distroless Python 3.13 non-root runtime을 멀티스테이지로 분리합니다. 최종 이미지에는 shell과 package manager를 포함하지 않고 UID 65532로 실행하며, CD의 Trivy 정책은 수정 가능한 HIGH/CRITICAL만 차단하고 vendor fix가 없는 기본 이미지 CVE는 ECR 네이티브 스캔 결과와 분리해 추적합니다.

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

python3 scripts/scaffold_service.py \
  --name catalog-api \
  --owner team-d2c-platform \
  --output-dir /tmp/catalog-api
python3 -m pytest -q /tmp/catalog-api/app/tests
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

테스트 비용을 줄이기 위해 기본값은 EKS worker 1대(`t3.medium`), NAT Gateway 1개, RDS primary-only·Single-AZ로 조정했습니다. 고가용성 검증이 필요한 경우에만 `enable_multi_az_nat=true`, `enable_rds_replica=true`, `enable_rds_multi_az=true`, node 수 증가를 별도로 선택합니다. RDS Multi-AZ는 동기 standby와 failover drill을 위한 명시적 비용 선택이며, 전체 선택 근거와 실제 측정값은 로컬 전용 engineering notes에 기록합니다.

## 배포 전 조건

배포 namespace에는 다음 ConfigMap과 Secret을 먼저 준비합니다. 운영 overlay도 RDS 인증서를 검증하며, 관리 계정은 마이그레이션 Job에만 전달합니다.

- `raffle-config` ConfigMap: `DB_WRITER_HOST`, `DB_READER_HOST`, `DB_NAME`, `TRUSTED_HOSTS`(허용할 실제 서비스 호스트)
- `rds-ca-bundle` ConfigMap: AWS 공식 RDS CA bundle을 담은 `global-bundle.pem` 키. 모든 DB 접근 워크로드에 읽기 전용으로 마운트합니다.
- `raffle-secret` Secret: `DB_APP_USER`, `DB_APP_PASSWORD`, `SECRET_KEY`. 앱과 추첨 작업에 필요한 DML 계정만 사용합니다.
- `raffle-migration-secret` Secret: `DB_ADMIN_USER`, `DB_ADMIN_PASSWORD`, `DB_APP_USER`, `DB_APP_PASSWORD`, `SECRET_KEY`. 스키마 및 최소 권한 계정 생성용이며 앱 Pod에는 전달하지 않습니다. `SECRET_KEY`는 공통 앱 모듈을 읽는 현재 마이그레이션 진입점의 초기화에 사용합니다.

앱의 `DB_USER`/`DB_PASSWORD`는 이전 설정을 위한 호환 키입니다. 새 배포는 `DB_APP_*` 키를 사용합니다. 운영에서는 샘플 데이터와 장애 주입을 활성화하지 않습니다. 평문 비밀번호로 저장된 기존 로그인 계정은 더 이상 인증하지 않으므로 별도로 비밀번호를 재설정해야 합니다.

기존 Terraform 코드에 평문으로 있던 RDS 비밀번호는 제거했습니다. 이전 값이 Git 이력에 남아 있으므로 실제 AWS 환경에서 즉시 비밀번호를 회전해야 합니다. 운영자 접속은 기본적으로 public SSH가 아니라 private subnet의 SSM 경로를 사용하며, SSH가 필요할 때만 `allowed_ssh_location`에 제한된 CIDR을 명시합니다.

## 배포 후보와 실제 실행 버전 확인

배포 전후 검사는 서로 다른 질문에 답합니다. 아래 도구는 배포·Git 변경·권한 부여를 수행하지 않습니다.

| 확인 단계 | 도구 | 확인하는 범위 |
|---|---|---|
| 배포 후보 | `scripts/verify_release_bundle.py` | source SHA, image digest, SBOM·후보 파일의 해시, 현재 manifest와의 일치 |
| 서명 출처 | 같은 도구의 `--verify-attestation` | GitHub CLI로 release-evidence 서명과 저장소·workflow·source SHA/ref 확인 |
| 실행 이미지 서명 | `--verify-image-attestation` | 레지스트리의 SPDX attestation과 정확한 OCI image digest·빌드 출처 확인 |
| 배포 상태 | `scripts/verify_gitops_deployment.py` | 실제 조회 계정의 권한, Argo Git revision, Rollout 상태, 소유 Pod의 실행 digest |

CI는 후보 일관성 검사를 서명·artifact 업로드 전에 실행합니다. 다운로드한 번들을 확인할 때는 신뢰할 수 있는 CI 실행에서 source SHA와 이미지 저장소를 별도로 확인한 뒤 다음 명령을 사용합니다. 대문자 값은 실제 값으로 바꿉니다.

```sh
python scripts/verify_release_bundle.py \
  --candidate-dir release/candidate \
  --release-evidence release/release-evidence.json \
  --sbom release/data-pipeline-app.sbom.spdx.json \
  --current-manifest k8s/overlays/prod/kustomization.yaml \
  --source-revision SOURCE_FULL_SHA \
  --image-name REGISTRY/data-pipeline-app \
  --verify-attestation
```

`--verify-attestation`을 생략한 성공은 **로컬 파일 일관성만** 의미합니다. 서명 확인도 PR 승인, 브랜치 보호, 배포 성공을 대신하지 않습니다. 현재 manifest가 후보 생성 이후 달라졌다면 stale 후보로 거부합니다. 해시를 임의로 고치는 대신 최신 기준으로 후보를 다시 만들고 검토해야 합니다.

아래 명령은 기본적으로 조회 계획만 표시합니다. 실제 검증에는 제한 계정으로 구성한 kube context와 플랫폼 관리자가 적용한 governance RBAC가 필요합니다. `--execute`를 추가했을 때만 읽기 전용 API를 호출합니다.

```sh
python scripts/verify_gitops_deployment.py \
  --context DEVELOPER_CONTEXT \
  --namespace platform-validation \
  --principal system:serviceaccount:platform-validation:kyobo-developer-readonly \
  --application data-pipeline-validation \
  --expected-revision GITOPS_MERGE_FULL_SHA \
  --expected-image REGISTRY/data-pipeline-app:SOURCE_FULL_SHA@sha256:IMAGE_DIGEST
```

이미지를 만든 source SHA와 배포 PR의 GitOps merge SHA는 서로 다를 수 있습니다. 오래된 Argo 상태, 빠진 증거, 다른 이미지, 관리자 수준의 접근 권한은 정상 결과로 처리하지 않습니다. 개발자 계정에는 `argocd` namespace의 지정된 Application 하나를 `get`할 수 있는 권한만 추가하며 다른 Application 조회나 sync·Secret 접근은 허용하지 않습니다.

이 도구들은 **로컬 검증과 제어면 상태 대조**를 위한 기반입니다. prod 배포 후보를 validation overlay의 승인 PR에 연결하는 실환경 검증, 실제 HTTP/DB 상태, canary 트래픽 복귀, 수정 PR 후 복구는 별도 절차입니다. Rollouts 중단은 Git 되돌림이 아닙니다. 승인 PR, 최신 Argo revision, 정상 이미지와 사용자 경로의 회복을 모두 확인하기 전에는 전체 E2E 또는 복구 완료라고 기록하지 않습니다.

`dev`와 `main` 대상 PR·push에 CI, Security, CodeQL을 실행하며 이미지 발행 CD는 수동 실행을 포함해 `main`만 대상으로 합니다. 실제 필수 검사 강제 및 PR 승인은 GitHub 브랜치 보호/규칙을 별도로 설정해야 합니다.

## IDP 골든패스

[`platform/golden-path`](platform/golden-path)는 이 저장소의 배포 패턴을 다른 서비스가 재사용할 수 있게 만든 첫 번째 IDP slice입니다. 스캐폴더는 서비스 소유자, Backstage catalog metadata, CI, 비-root 컨테이너, health probe, Kustomize, Argo Rollouts canary, Prometheus error-rate/p95 latency 분석 템플릿을 한 번에 생성합니다.

이것은 Backstage 전체 설치를 이미 운영한다는 주장이 아니라, 내부 개발자 플랫폼으로 승격할 수 있는 실행 가능한 service template과 계약입니다. 다음 확장 단계는 중앙 reusable workflow와 Argo CD ApplicationSet에 연결하는 것입니다.

## Agentic AI 운영 진단

`agentic_ops/`는 Prometheus와 Kubernetes의 제한된 읽기 전용 관측값에서 조사 도구를 선택하고, 근거 ID가 연결된 장애 진단을 만듭니다. 응용프로그램 API·DB 정합성 지표와 Argo Rollouts/AnalysisRun을 함께 조사합니다. 도구 스키마·호출 예산·관측 신선도·근거 인용을 검증하고, 정상 판정은 필수 SLI가 모두 확인된 경우에만 허용합니다. 에이전트에는 SQL·셸·클러스터 변경 권한이 없으며, canary 판정과 복구는 기존 Argo Rollouts/GitOps 경로가 담당합니다.

현재 자동화 테스트는 합성 incident replay와 정책 계약을 확인한 단계입니다. 실제 클러스터 수집, OpenAI 호출, 모델 진단 품질은 별도 인증·비용 승인 후 검증해야 하며 현재 완료된 실환경 AI 평가로 주장하지 않습니다.

구현 중 발생한 CI/CD·AWS OIDC·SBOM·ECR scan·distroless runtime 실패와 선택 근거는 저장소 외부의 로컬 engineering notes로 관리합니다. 원격 README에는 재현 가능한 실행 방법과 공개 가능한 시스템 경계만 남깁니다.

## 검증된 운영 증거와 범위

- 새 세션의 원본 evidence, 부하 로그, 계정·리소스 식별자와 인증 정보는 로컬에 둡니다. 정적 검증과 실환경 결과를 구분하고 과거 결과를 현재 변경의 증거로 사용하지 않습니다.
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
