# GitOps · 응모 서비스의 EKS 배포와 복구

팀에서 만든 상품 추첨 서비스를 가지고, EKS에서 새 버전을 배포하고 문제가 생기면 복구하는 작업을 개인 프로젝트로 이어갔습니다. 트래픽을 새 버전으로 옮기는 동안 고객의 응모와 후속 이벤트가 DB에 함께 기록되는지 살폈습니다.

10월 6일 시험은 새 버전으로 트래픽을 절반까지 옮긴 뒤 멈췄습니다. **CPU 할당량을 6코어에서 8코어로 늘리고 수동으로 다시 동기화한 뒤, 추가 응모 10건이 모두 접수됐습니다.** 마지막 단계에서 기존 Pod 4개와 새 Pod 4개가 함께 떠야 했는데, Pod당 CPU 제한값이 1코어라 합계가 기존 할당량을 넘은 게 원인이었습니다. [당시 시험 기록](https://github.com/masondev1024/aws-data-platform-gitops/blob/81e1706a9650249273cb71b99eccf55ac02f71ec/README.md#검증된-운영-증거와-범위) · [할당량 수정 PR #42](https://github.com/masondev1024/aws-data-platform-gitops/pull/42)

같은 실수를 잡기 위해 배포 설정을 검사하는 테스트를 추가했습니다. 두 버전을 함께 띄울 때의 CPU 제한값 합계가 할당량을 넘으면 테스트가 실패합니다. 또 자동 확장기(HPA)가 늘린 Pod 수를 Argo CD가 동기화할 때 덮어쓰지 않도록 설정했습니다. [CPU 검사 코드](https://github.com/masondev1024/aws-data-platform-gitops/blob/da4cd00539bcf5c5fc49f81724e788007567ace6/scripts/tests/test_validation_delivery.py#L135-L149) · [HPA 설정 PR #41](https://github.com/masondev1024/aws-data-platform-gitops/pull/41)

[시험 결과](#확인한-결과) · [응모 기록 확인 방법](#구조-요약) · [로컬 검증](#로컬-검증) · [AWS 검증 환경](#aws-검증-환경)

<a id="검증된-운영-증거와-범위"></a>

## 확인한 결과

2026년 10월 6일, 임시 AWS 환경에서 기존 버전만 먼저 시험한 뒤 새 버전에 트래픽을 일부 보내는 카나리 배포를 시험했습니다. 부하 시험에서는 한 번의 응모에 여러 HTTP 요청이 들어갑니다. 아래 응모 건수는 같은 행의 전체 HTTP 요청에 포함됩니다.

| 시험한 시점 | 성공한 HTTP 요청 | 성공한 응모 | 응모 API p95 |
|---|---:|---:|---:|
| 첫 기준선 시험 | 15,599 / 15,600 | 3,899 / 3,900 | — |
| 설정 수정 후, 기존 버전 13분 | 15,600 / 15,600 | 3,900 / 3,900 | 107.9ms |
| 같은 세션, 카나리 13분 | 15,600 / 15,600 | 3,900 / 3,900 | 113.5ms |

- **카나리를 시작하기 전** 첫 시험에서 로드 밸런서(ALB)가 502를 한 건 반환해 배포를 멈췄습니다. Gunicorn이 연결을 유지하는 시간을 ALB의 유휴 제한보다 길게 설정하고 새 세션에서 재시험했습니다. 첫 502의 정확한 원인을 확인할 요청별 ALB 로그는 없었습니다. [조사와 수정 PR #39](https://github.com/masondev1024/aws-data-platform-gitops/pull/39)
- **카나리 진행 중** ALB가 새 버전에 실제로 10%, 50% 가중치를 적용했고 두 서비스가 서로 다른 엔드포인트를 쓰는지 확인했습니다. 각 단계에서 Prometheus와 CloudWatch 검사를 통과했습니다. 재시험한 두 구간에서 부하 도구가 시작하지 못한 반복은 0회였고, ALB·대상 서버 5xx와 DB에서 후속 이벤트가 빠진 응모도 0건이었습니다.
- **수동 복구 후** 읽기 전용 계정으로 Argo의 Git 버전과 Pod에서 실행된 이미지를 대조했습니다. 외부 준비 상태 요청 150건과 별도로 보낸 응모 10건이 모두 성공했습니다. 마지막 재동기화 중에는 준비된 Pod가 잠시 2개로 줄었습니다. 이 세션의 전체 HTTP 요청 기록은 31,540건이며 HTTPS 시험에는 자체 서명 인증서를 썼습니다.
- **시험 종료 시** Terraform 리소스 36개를 정리했습니다. 종료 직후 Terraform 상태와 세션 태그로 조회한 잔여 리소스, 확인하지 못한 항목이 각각 0개였습니다. 1.77시간 사용량으로 추정한 USD 2.448에 예비비 USD 1을 더한 계획 금액은 USD 3.448입니다. [배포·복구 기록 요약](https://jungseongheon.org/portfolio/evidence/index.html#gitops)

## 구조 요약

```text
배포   GitHub Actions → ECR 이미지와 배포 후보 → PR 병합 → Argo CD → EKS
응모   ALB → 응모 API → RDS에 응모와 후속 이벤트를 한 트랜잭션으로 저장
검사   Prometheus(응모·오류·지연) + CloudWatch(ALB) → Argo Rollouts가 다음 단계 결정
```

응모가 접수되면 API는 `raffle_entries`에 응모를, `raffle_outbox_events`에 나중에 전달할 이벤트 `raffle.entry.accepted.v1`을 함께 저장합니다. 둘 중 하나를 쓰지 못하면 트랜잭션을 되돌립니다. 점검할 때는 쓰기 DB에서 최근 10분의 응모를 이벤트와 조인해 짝이 없는 응모를 셉니다. DB 조회에 실패하면 이 지표에 `-1`을 기록합니다. 카나리가 다음 단계로 가려면 값이 `0`이어야 합니다. Kafka로 이벤트를 보내고 S3에 적재하는 코드는 별도 저장소에서 볼 수 있습니다. [응모 저장과 대조 코드](app/app.py) · [카나리 판정 설정](k8s/base/analysis-template.yaml) · [이벤트 처리 저장소](https://github.com/masondev1024/d2c-event-data-platform)

Prometheus는 서비스 이름으로 카나리 지표를 고릅니다. CloudWatch에서는 이 세션 전용 ALB의 오류율과 응답 시간을 봅니다. Pod를 내릴 때는 진행 중인 요청에 시간을 주기 위해 종료 전 30초를 기다리고 전체 종료 유예 시간은 60초로 설정했습니다. 검증 환경에서 ALB가 기존 연결을 정리하는 시간은 30초입니다. [ALB 분석 설정](k8s/components/alb-canary-gate) · [Pod 종료 설정](k8s/overlays/validation/patch-rollout.yaml)

## 로컬 검증

저장소 루트에서 Python 테스트와 Kubernetes 배포 설정 검사를 실행합니다. `verify_platform.sh`는 Python 환경과 `kubectl`, Terraform이 필요합니다. `all`은 운영·검증용 매니페스트 렌더링, 백엔드 연결 없는 Terraform 검사, Python 의존성·정적 보안 검사까지 실행합니다. Docker 빌드는 별도입니다. [검증 스크립트](scripts/verify_platform.sh)

```bash
python3 -m pip install -r app/requirements-dev.txt
PYTHON_BIN=python3 bash scripts/verify_platform.sh all
docker build --pull --tag data-pipeline-app-ci ./app
```

특정 단계만 반복하려면 다음 명령을 사용합니다.

```bash
python3 -m pytest -q app/tests scripts/tests
kubectl kustomize k8s/overlays/prod >/tmp/rendered-production.yaml
terraform -chdir=terraform init -backend=false -input=false
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform validate
```

## CI/CD

관련 코드가 바뀌면 GitHub Actions가 `dev`·`main`의 PR과 push에서 앱 테스트, Docker 빌드, Kustomize 렌더링, Terraform 검사를 실행합니다. 보안 검사에는 Python 의존성 감사, Bandit, Trivy의 인프라·비밀값·이미지 검사와 CodeQL을 씁니다. 저장소에서 검사를 병합 조건으로 강제하려면 브랜치 규칙도 설정합니다. 로컬 Jenkins는 신뢰한 소스에 대해 권한이 필요 없는 검사만 실행합니다. [워크플로](.github/workflows) · [Jenkins 설정](Jenkinsfile)

1. `main`에서 앱 관련 변경 검사를 통과하고 `CD_PUBLISH_ENABLED=true`이면 이미지를 검사해 `eu-west-1` ECR에 커밋 SHA로 발행합니다. 같은 SHA 태그가 이미 있으면 기존 digest를 다시 검사합니다.
2. CI는 이미지 digest를 고정한 배포 후보와 소프트웨어 구성 명세(SBOM), 검증 기록을 artifact로 남깁니다. 후보를 검토해 별도 PR로 병합합니다.
3. Argo CD는 `main`의 `k8s/overlays/prod`를 읽고 Argo Rollouts가 새 버전에 트래픽을 나누어 보냅니다. 실제 실행 버전과 응모 상태는 배포 후에 확인합니다.

이미지는 Python 3.13 slim에서 빌드하고, digest를 고정한 distroless Python 3.13에서 UID 65532로 실행합니다. 실행 이미지에는 셸이나 패키지 관리자가 없습니다. Trivy는 수정 가능한 HIGH·CRITICAL 취약점을 차단하고 미수정 항목은 ECR 스캔 결과와 구분해 추적합니다. 10월 19일에는 만료된 OpenSSL VEX 예외를 제거하고 `libexpat1` 수정이 포함된 이미지로 바꾼 뒤 빌드·보안 검사를 다시 통과했습니다. 이 이미지의 AWS 성능은 재측정하지 않았습니다. [Dockerfile](app/Dockerfile) · [보안 검사](.github/workflows/security.yaml)

## GitHub 설정

검증을 마친 뒤 임시 ECR 저장소도 정리했습니다. 다시 발행하려면 해당 계정·리전에 ECR 저장소를 만들고 태그 불변성과 OIDC 역할 권한을 확인합니다. 리소스를 정리해 둔 동안에는 `CD_PUBLISH_ENABLED`를 비워 둡니다. 공통 검사와 보안 검사는 발행 설정과 별도로 실행됩니다.

| 저장할 위치 | 이름 | 용도 |
|---|---|---|
| Actions → Variables | `AWS_ROLE_TO_ASSUME` | Terraform의 `github_actions_role_arn` 출력값. 이미지 발행에 사용 |
| Actions → Variables | `CD_PUBLISH_ENABLED` | 이미지 발행을 허용할 때 `true` |
| Actions → Variables | `AWS_TERRAFORM_ROLE_TO_ASSUME` | CD 역할과 분리한 인프라 plan용 역할 ARN |
| Actions → Variables | `CLUSTER_API_ALLOWED_CIDRS` | EKS API 접속을 허용할 CIDR 목록. 예시 `["<your-public-ip>/32"]` |
| Actions → Secrets | `TF_VAR_DB_PASSWORD` | RDS에 사용할 16자 이상의 새 비밀번호 |

AWS에 연결하는 Terraform plan은 관련 push이고 plan 역할과 CIDR 변수를 모두 설정했을 때 실행합니다. PR에서는 정적 검사만 합니다. 이미지 발행을 켰는데 CD 역할이 비어 있으면 발행 작업이 중단됩니다.

## 배포 전 조건

DB와 통신하는 워크로드는 RDS 인증서를 검증합니다. 배포할 네임스페이스에 다음 항목을 먼저 준비합니다.

| 종류와 이름 | 필요한 키와 사용처 |
|---|---|
| ConfigMap `raffle-config` | `DB_WRITER_HOST`, `DB_READER_HOST`, `DB_NAME`, `TRUSTED_HOSTS`. 실제 서비스 호스트를 지정합니다. |
| ConfigMap `rds-ca-bundle` | AWS 공식 RDS CA를 `global-bundle.pem` 키로 저장해 읽기 전용으로 마운트합니다. |
| Secret `raffle-secret` | `DB_APP_USER`, `DB_APP_PASSWORD`, `SECRET_KEY`. 앱과 추첨 작업은 필요한 DML 권한만 씁니다. |
| Secret `raffle-migration-secret` | `DB_ADMIN_USER`, `DB_ADMIN_PASSWORD`, `DB_APP_USER`, `DB_APP_PASSWORD`, `SECRET_KEY`. 관리 계정은 마이그레이션 Job에서만 씁니다. 공통 앱 모듈 초기화에 `SECRET_KEY`가 필요합니다. |

새 배포에는 `DB_APP_*` 키를 사용합니다. `DB_USER`·`DB_PASSWORD`는 이전 설정의 호환 키입니다. 운영 환경에서는 샘플 데이터와 장애 주입을 끄고, 과거에 비밀번호를 평문으로 저장한 로그인 계정은 재설정해야 합니다. Git 이력에 남은 과거 RDS 비밀번호를 쓰는 환경에서는 해당 비밀번호를 교체합니다. 운영자는 기본적으로 private subnet의 SSM을 통해 접속합니다. SSH가 꼭 필요하면 `allowed_ssh_location`에 허용할 CIDR을 지정합니다.

<a id="비용-안전-게이트"></a>

## AWS 검증 환경

기본 Terraform 설정은 `allow_full_stack_apply=false`입니다. EKS·EC2·NAT·ALB·RDS를 사용하는 전체 인프라를 계획할 때는 예산과 종료 담당자를 정합니다. 기본 규모는 `t3.medium` worker 1대, NAT Gateway 1개, Single-AZ RDS primary 1대입니다. 다중 AZ 시험에는 필요한 항목만 선택합니다. NAT는 `enable_multi_az_nat`, RDS 읽기 복제본은 `enable_rds_replica`, 동기식 대기 인스턴스를 두는 장애 전환 시험은 `enable_rds_multi_az`로 설정합니다.

EKS API는 기본적으로 private endpoint를 사용합니다. VPC 안의 SSM runner라면 그대로 접속합니다. 로컬에서 Terraform·Helm을 실행할 때는 public endpoint와 접근할 CIDR을 함께 지정합니다. 다음은 전체 인프라의 **plan** 예시입니다.

```bash
terraform -chdir=terraform plan \
  -var='allow_full_stack_apply=true' \
  -var='cluster_endpoint_public_access=true' \
  -var='cluster_api_allowed_cidrs=["<your-public-ip>/32"]'
```

임시 AWS 검증에는 별도의 `platform/live-lab` 설정을 씁니다. [비용 추정 스크립트](platform/live-lab/scripts/estimate_session_cost.py)로 승인된 비용 계획을 확인하고 종료·정리 절차를 준비해야 합니다. 비용 추정은 AWS가 강제하는 지출 상한이 아니며, 청구 반영이 늦거나 정리가 지연될 수 있습니다. 현재 설정은 생성·정리를 포함해 최대 3시간, 예비비 USD 1을 포함해 USD 5.50까지입니다. 시험이 끝나면 바로 정리하고 늦어도 시작 2시간 후에는 정리를 시작합니다. 새 DB는 MySQL 8.4를 사용하며 유료 연장 지원 자동 가입은 비활성화합니다. [세션 조건](platform/live-lab/acceptance.yaml)

1. `prepare_session.sh`로 입력을 준비하고 세션 Secret을 승인·생성합니다. `apply_session_plan.sh`는 비용, 계정, 리전, 생성할 리소스를 확인한 뒤 검사한 plan 파일을 그대로 적용합니다.
2. plan과 JSON 결과는 권한 `0600`으로 로컬 `platform/live-lab/evidence/`에 보관합니다. 원본 실행 기록, 개인 작업 기록, 자격 증명은 Git 추적에서 제외해 로컬에만 보관합니다. 정적 검사와 로컬 렌더링 결과는 실환경 실행 증거가 아닙니다.
3. 재시도할 때는 `LIVE_LAB_RUN_ID`로 감시·정리 기록을 나눕니다. 요청 수는 Terraform `SESSION_ID` 기준으로 누적해 재시도를 포함한 전체 세션에서 최대 200,000건까지 허용합니다.
4. 이 경로에서는 `terraform apply`를 직접 실행하지 말고 `apply_session_plan.sh`를 사용합니다. 종료할 때는 `teardown_live_lab.sh`를 사용합니다. 정리 plan은 시도마다 따로 남기고 감시 프로세스가 중단 신호를 받으면 세션 소유 리소스부터 정리합니다. [세션 스크립트](platform/live-lab/scripts)

## 배포 후보와 실제 실행 버전 확인

배포 후보를 내려받으면 신뢰하는 CI 실행에서 source SHA와 이미지 저장소를 먼저 확인합니다. 아래 대문자 예시 값은 해당 실행의 값으로 바꿉니다. 현재 배포 설정이 후보를 만든 뒤 바뀌었다면 최신 설정으로 후보를 다시 만듭니다. [후보 검증 도구](scripts/verify_release_bundle.py)

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

기본 검사는 source SHA, image digest, SBOM·후보 파일의 해시와 현재 배포 설정을 대조합니다. `--verify-attestation`은 GitHub CLI로 서명과 저장소·워크플로·커밋 출처까지 확인합니다. 레지스트리의 OCI 이미지 digest와 SPDX 서명을 대조하려면 `--verify-image-attestation`을 함께 지정합니다.

검증용 환경에서는 플랫폼 초기 설정에서 DB 주소, 인증 정보, RDS CA, 세션 Ingress를 먼저 준비합니다. Argo CD가 `k8s/overlays/validation`의 앱, PreSync 마이그레이션, 정기 작업을 배포합니다. `deploy_gitops_validation.py`는 기본적으로 계획을 보여 줍니다. `--execute`를 지정하면 계정, 클러스터 소유 태그, main SHA, 고정 이미지 digest와 `--bundle-dir`의 CI 서명을 확인한 뒤 동기화를 요청합니다. `sync_requested`를 받으면 다음 순서로 확인합니다. [검증용 배포 스크립트](platform/live-lab/scripts/deploy_gitops_validation.py)

1. 첫 동기화에서 만든 ALB에 세션 WAF가 연결됐는지 확인합니다. `bind_gitops_alb_identity.py`로 확인한 식별값을 Rollout 라벨에 기록합니다. Argo CD는 동기화 중 이 라벨을 보존합니다. 다음 이미지 변경 때 `--alb-binding-evidence`로 같은 세션의 확인 기록을 전달합니다. 식별값이나 WAF 연결이 맞지 않으면 동기화 요청을 중단합니다.
2. `observe_gitops_as_developer.py`로 단기 읽기 전용 계정을 발급받아 Argo Git 버전, Rollout 상태와 실행 중인 Pod의 image digest를 대조합니다. 해당 계정은 `argocd`에서 지정한 Application 하나만 조회합니다. Secret과 동기화 권한은 주지 않습니다. [접근 권한 설정](platform/governance)
3. 배포 후에는 HTTP 응답과 지연, DB의 응모·이벤트, 중단 후 트래픽이 어느 버전으로 돌아왔는지를 확인합니다. Rollout을 중단해도 Git의 배포 버전은 그대로이므로 수정 PR을 적용했다면 그 Git 버전과 실행 이미지까지 다시 확인합니다.

아래 명령은 조회 계획을 출력합니다. 실제 읽기 전용 검사를 하려면 해당 계정의 kube context를 준비해 `--execute`를 추가합니다. `SOURCE_FULL_SHA`는 이미지 빌드 커밋, `GITOPS_MERGE_FULL_SHA`는 배포 PR을 병합한 커밋입니다. [실행 버전 검사](scripts/verify_gitops_deployment.py)

```sh
python scripts/verify_gitops_deployment.py \
  --context DEVELOPER_CONTEXT \
  --namespace platform-validation \
  --principal system:serviceaccount:platform-validation:kyobo-developer-readonly \
  --application data-pipeline-validation \
  --expected-revision GITOPS_MERGE_FULL_SHA \
  --expected-image REGISTRY/data-pipeline-app:SOURCE_FULL_SHA@sha256:IMAGE_DIGEST
```

## 다른 서비스에서 배포 설정 재사용하기

`scaffold_service.py`는 서비스 소유자와 Backstage 카탈로그 정보, CI, 비루트 컨테이너, 상태 검사, Kustomize 및 카나리 배포 설정을 생성합니다. 새 서비스의 기본 오류율·p95 분석 템플릿도 포함합니다. 아래 예시는 `/tmp/catalog-api`에 파일을 만들고 테스트와 렌더링을 실행합니다. [서비스 템플릿](platform/golden-path)

```bash
python3 scripts/scaffold_service.py \
  --name catalog-api \
  --owner team-d2c-platform \
  --output-dir /tmp/catalog-api
python3 -m pytest -q /tmp/catalog-api/app/tests
kubectl kustomize /tmp/catalog-api/k8s/base >/tmp/rendered-golden-path.yaml
```

## 장애 진단 도구

`agentic_ops/`는 Prometheus와 Kubernetes에서 읽은 값으로 응모 오류, DB 기록, Argo Rollout 상태를 조사합니다. 진단에 쓴 관측값의 ID를 남기고 필요한 지표가 빠졌으면 정상으로 판정할 수 없게 했습니다. 호출 횟수와 관측 시각도 검사합니다. 도구는 조회 권한만 사용하고 배포와 복구는 Argo Rollouts와 운영자가 맡습니다. 합성 장애를 재생해 진단 규칙을 테스트했습니다. 9월 시험에서는 클러스터에서 관측값을 수집했고 외부 모델은 호출하지 않았습니다. [도구 코드](agentic_ops) · [당시 수집 기록](platform/live-lab/acceptance.yaml)
