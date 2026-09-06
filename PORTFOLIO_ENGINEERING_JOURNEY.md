# D2C Event Platform Engineering Journey

## 이 문서의 목적

이 문서는 단순한 기능 목록이 아니라, D2C 응모 승인 서비스와 Kafka 데이터 플랫폼을 운영 가능한 제품으로 발전시키는 과정에서 실제로 발생한 장애, 실패, 설계 선택, 검증 수치와 남은 리스크를 기록한다.

포트폴리오에서 가장 중요한 메시지는 “Kafka와 Kubernetes를 사용했다”가 아니다.

> 실패가 정상적으로 발생한다는 전제에서 원인을 관측하고, 데이터 정합성을 보장하며, 안전하게 배포하고, 비용을 통제하는 플랫폼을 만들었다.

문서 작성 기준 시점은 2026-09-07 KST다. AWS 계정 번호, 토큰, 비밀번호와 같은 자격 증명은 기록하지 않는다.

## 1. 프로젝트 한 줄 요약

응모 승인 요청을 애플리케이션 메모리의 이벤트가 아니라 DB 트랜잭션과 transactional outbox로 확정하고, DB 기준 parity SLI를 Argo Rollouts canary gate에 연결해 불일치·5xx·지연 발생 시 stable 버전으로 복귀시키는 D2C 이벤트 플랫폼 수직 슬라이스다.

전체 데이터 흐름은 다음과 같다.

~~~text
사용자 응모 승인
  -> raffle row와 outbox event를 동일 DB transaction으로 기록
  -> writer DB 기준 accepted row와 outbox row의 parity 측정
  -> Prometheus SLI 노출
  -> Argo Rollouts canary analysis
  -> 안정적이면 승격, 실패하면 stable 복귀
  -> Kafka relay/consumer가 계약 검증·중복 제거·DLQ 처리
  -> S3 Parquet/Iceberg lakehouse 계층으로 적재
~~~

현재 저장소의 책임은 승인 경계와 배포 플랫폼이다. Kafka relay/consumer, S3 Parquet/Iceberg 데이터 레이크 소비 흐름은 별도 d2c-event-data-platform 저장소와 연결한다.

## 2. 왜 오래 걸렸는가

이번 작업은 코드 몇 줄을 추가하는 작업이 아니라 외부 시스템의 실제 상태를 끝까지 검증하는 작업이었다.

1. GitHub Actions의 green 상태만으로 배포 성공이라고 판단하지 않았다.
2. AWS OIDC role이 실제로 존재하는지, trust policy의 subject가 repository ID와 branch ID까지 일치하는지 확인했다.
3. ECR에 이미지가 존재하는지뿐 아니라 immutable tag, image digest, SBOM, provenance와 native scan을 각각 확인했다.
4. 로컬 Trivy와 AWS ECR Basic Scan의 데이터베이스·정책 차이를 구분했다.
5. distroless 이미지로 바꾼 뒤 애플리케이션이 실제로 기동하는지, health/readiness 계약과 non-root 실행까지 다시 검증했다.
6. 전체 EKS/RDS/NAT/ALB 스택은 포트폴리오 검증에 필요한 범위를 넘는 상시 비용이 발생하므로, CD 검증에 필요한 bootstrap 리소스와 전체 workload 인프라를 분리했다.

즉, 오래 걸린 것은 반복적인 재시도 때문이 아니라 각 실패를 “원인 미확인 상태의 통과”로 덮지 않고 재현·복구·재발 방지까지 확인했기 때문이다.

## 3. 핵심 설계 결정과 선택 근거

### DR-01. 직접 Kafka publish 대신 transactional outbox

**문제**

DB에 응모 승인을 먼저 commit하고 Kafka publish가 실패하면 승인 데이터는 존재하지만 이벤트가 사라진다. 반대로 Kafka를 먼저 publish하면 DB commit 실패 시 유령 이벤트가 남는다.

**선택**

응모 row와 outbox row를 같은 DB transaction으로 기록하고, 별도 relay가 outbox를 Kafka로 전달한다.

**선택 이유**

- 승인 사실과 이벤트 의도의 원자성을 DB가 보장한다.
- relay 재시도는 at-least-once여도 event_id와 unique constraint로 중복을 제어할 수 있다.
- Kafka 장애가 사용자 승인 API의 commit을 직접 막지 않는다.
- outbox backlog, retry count, DLQ를 독립적으로 관측할 수 있다.

**트레이드오프**

동기 Kafka publish보다 end-to-end latency가 늘고, relay와 outbox cleanup 운영이 필요하다. 그러나 비즈니스 승인 정합성과 장애 복구 가능성이 더 중요하다.

### DR-02. 애플리케이션 counter가 아니라 DB 기준 parity SLI

**문제**

애플리케이션 메모리 counter 두 개를 비교하면 프로세스 재시작, multi-worker, scrape 누락, DB commit 실패를 반영하지 못한다.

**선택**

writer DB에서 최근 승인 row와 동일 범위의 outbox row를 직접 대조하고, 그 차이를 raffle_apply_outbox_parity_gap으로 노출한다.

**선택 이유**

- source of truth인 DB에서 측정한다.
- 승인 수와 이벤트 수의 시간 범위를 동일하게 정의할 수 있다.
- canary 승격 조건에 도메인 정합성을 직접 넣을 수 있다.

**실패 처리**

DB 질의 실패 또는 production writer 설정 누락 시 0을 반환하지 않고 -1을 반환한다. 측정 불능을 정상으로 위장하지 않는 fail-closed 원칙이다.

### DR-03. GitOps와 immutable image reference

**선택**

GitHub Actions는 Kubernetes API를 직접 변경하지 않고, ECR에 commit SHA tag 이미지를 push한 뒤 GitOps 저장소의 production kustomization만 갱신한다. Argo CD가 Git을 감시하고 Argo Rollouts가 canary를 실행한다.

**선택 이유**

- 배포 선언 상태와 실제 변경 이력이 Git에 남는다.
- latest tag drift와 재현 불가능한 배포를 막는다.
- rollout 실패 시 stable 버전으로 복귀하는 경로가 명확하다.
- release evidence와 image digest를 연결할 수 있다.

### DR-04. 장기 AWS access key 대신 GitHub OIDC

**선택**

GitHub Actions가 AWS STS AssumeRoleWithWebIdentity를 사용하도록 하고, role trust policy의 subject를 repository owner/repository ID/branch ID까지 제한한다.

**선택 이유**

- 장기 credentials를 GitHub secret에 저장하지 않는다.
- workflow 실행마다 짧은 수명의 session을 사용한다.
- 다른 repository나 branch가 deploy role을 오용하기 어렵다.

**트레이드오프**

repository 이름만 확인하는 단순 trust보다 설정이 복잡하고, repository ID나 branch ID를 바꾸면 trust policy도 함께 갱신해야 한다. 대신 supply-chain 공격 범위를 줄인다.

### DR-05. ECR immutable repository

ECR image tag mutability를 IMMUTABLE로 설정하고 image scan-on-push를 활성화했다. 동일 SHA tag를 덮어쓸 수 없으므로 GitOps의 tag와 digest가 가리키는 artifact가 바뀌지 않는다.

### DR-06. multi-stage distroless non-root runtime

초기 runtime은 Python slim 이미지였다. 실제 ECR Basic Scan에서 OS package 취약점이 확인되어 builder와 runtime을 분리했다.

- builder: Python 3.13 slim trixie, digest pinning
- runtime: distroless Python 3.13 Debian 13, digest pinning
- final image: shell/package manager 미포함
- process user: UID/GID 65532
- gunicorn은 distroless entrypoint에 맞춰 module mode로 실행

이 선택은 공격 표면과 runtime 변경 가능성을 줄인다. 대신 shell 기반의 운영 디버깅이 불가능하므로 로그·metrics·ephemeral debug 절차가 더 중요해진다.

### DR-07. IDP golden path

scripts/scaffold_service.py가 서비스 이름, owner, port를 받아 다음을 생성한다.

- Backstage catalog metadata
- CI와 Docker build
- distroless non-root Dockerfile
- health/readiness probe
- Kustomize와 Argo Rollouts canary
- Prometheus error-rate/p95 analysis template

이는 Backstage 전체를 이미 운영한다는 주장이 아니라, 내부 개발자 플랫폼으로 승격할 수 있는 실행 가능한 service template과 guardrail이다.

### DR-08. 비용 안전 경계

EKS control plane, worker, NAT Gateway, ALB, RDS는 상시 유지하지 않았다. GitHub CD 검증에 필요한 ECR repository, lifecycle policy, GitHubActionsDeployRole과 ECR policy만 별도 bootstrap stack으로 복구했다.

bootstrap apply 결과는 다음과 같다.

~~~text
4 added, 0 changed, 0 destroyed
~~~

전체 workload 인프라를 실제로 검증할 때는 별도 비용 승인, 짧은 검증 시간, destroy와 잔여 리소스 확인을 전제로 한다.

## 4. 실제 실패와 트러블슈팅 기록

### INC-01. GitHub-hosted runner에서 rg 명령이 없음

**증상**

첫 release path CI에서 Kubernetes manifest assertion 단계가 exit 127로 실패했다.

~~~text
rg: command not found
Process completed with exit code 127
~~~

**원인**

로컬 macOS에는 ripgrep이 있었지만 GitHub-hosted runner에 기본 설치된다는 보장이 없었다. CI가 특정 개발자 도구에 암묵적으로 의존했다.

**대응**

고정 문자열 확인은 POSIX에 가까운 grep -F로 바꾸고, CI를 다시 실행했다.

**결과**

- 실패 run: [CI 34038645369](https://github.com/masondev1024/aws-data-platform-gitops/actions/runs/34038645369)
- 수정 commit: f179fce
- 성공 run: [CI 34038755932](https://github.com/masondev1024/aws-data-platform-gitops/actions/runs/34038755932)

**재발 방지**

CI command는 runner image에 우연히 존재하는 로컬 도구가 아니라 workflow가 명시적으로 설치한 도구 또는 이식성 높은 기본 도구를 사용한다.

### INC-02. AWS OIDC AssumeRoleWithWebIdentity 실패

**증상**

CD가 ECR login 이전의 AWS credentials 단계에서 다음 오류로 실패했다.

~~~text
Could not assume role with OIDC:
Not authorized to perform sts:AssumeRoleWithWebIdentity
~~~

- 실패 run: [CD 34038645412](https://github.com/masondev1024/aws-data-platform-gitops/actions/runs/34038645412)

**처음 의심한 것**

단순히 AWS credentials를 다시 입력하거나 workflow를 재시도하는 방법은 선택하지 않았다. OIDC 실패는 token audience, trust subject, role 존재 여부 중 하나가 틀렸다는 신호이므로 각각 확인해야 한다.

**확인**

갱신된 AWS profile로 STS caller identity는 정상 확인됐지만, 다음 IAM 조회에서 role 자체가 존재하지 않았다.

~~~text
NoSuchEntity: role GitHubActionsDeployRole
~~~

GitHub repository variable에는 삭제된 role ARN이 남아 있었다. 즉, token 형식 문제가 아니라 “workflow가 가리키는 AWS role이 실제 계정에 없음”이 root cause였다.

**대응**

AWS-only bootstrap Terraform에서 ECR repository, lifecycle policy, GitHubActionsDeployRole, ECR inline policy를 재생성했다. trust policy는 repository ID, repository owner ID, main branch ref와 sts audience를 제한했다.

**결과**

이후 CD에서 OIDC, ECR login, build/push, manifest update가 모두 통과했다.

**배운 점**

GitHub variable에 ARN 문자열이 있다고 infrastructure가 존재하는 것은 아니다. CI/CD 자격 증명은 다음 세 가지를 함께 검증해야 한다.

1. local STS identity
2. target IAM role 존재 여부
3. trust policy subject와 GitHub OIDC claims의 exact match

### INC-03. SBOM output directory가 존재하지 않아 release evidence 실패

**증상**

이미지 build/push와 Trivy gate까지 성공했지만 SPDX SBOM 단계에서 실패했다.

~~~text
ENOENT: no such file or directory,
open 'release/data-pipeline-app.sbom.spdx.json'
~~~

- 실패 run: [CD 34039720000](https://github.com/masondev1024/aws-data-platform-gitops/actions/runs/34039720000)

**원인**

anchore/sbom-action에 release/data-pipeline-app.sbom.spdx.json을 output으로 전달했지만 workflow가 release directory를 먼저 만들지 않았다.

**대응**

SBOM 단계 전에 mkdir -p release를 추가하고 artifact upload와 provenance attestation 경로를 동일하게 맞췄다.

**결과**

- 수정 commit: ab5ba2d
- 성공 run: [CD 34039837372](https://github.com/masondev1024/aws-data-platform-gitops/actions/runs/34039837372)
- release evidence artifact와 SPDX SBOM artifact 생성 성공

**배운 점**

파일을 생성하는 action은 directory까지 보장한다고 가정하면 안 된다. artifact path는 생성·검증·업로드의 lifecycle을 한 workflow 안에서 명시해야 한다.

### INC-04. Debian base image와 AWS ECR scan의 차이

**첫 결과**

초기 bookworm 기반 이미지의 ECR Basic Scan은 다음 결과를 반환했다.

~~~text
총 25건
CRITICAL 4 / HIGH 15 / MEDIUM 6
~~~

app 의존성보다 openssl, perl, util-linux, pcre2, zlib 같은 OS package가 중심이었다. apt update 후에도 후보 버전이 동일하여 “apt를 한 번 더 실행하면 해결된다”는 가설은 성립하지 않았다.

**첫 대응**

Debian trixie digest로 runtime base를 갱신했다.

- 수정 commit: 9ce1bfe
- ECR image digest: sha256:119dd7bc5a7c3881f8e7ca74022d30a81d3920e424c53adafd90248c599d6f90
- ECR Basic Scan: CRITICAL 6 / HIGH 11 / MEDIUM 3 / LOW 1

trixie 변경만으로 AWS native 결과가 0이 되지 않았으므로 성공이라고 포장하지 않았다.

**정책 차이**

GitHub CD의 Trivy gate는 --ignore-unfixed를 사용해 수정 가능한 HIGH/CRITICAL을 차단한다. 반면 ECR Basic Scan은 vendor fix가 아직 없는 finding도 표시할 수 있다. 따라서 다음을 구분했다.

- fixable finding: CD를 차단하고 즉시 수정
- vendor-unfixed finding: 무시하지 않고 native scan에서 별도 기록, base digest 업데이트를 추적
- scanner DB mismatch: “로컬 Trivy 0”을 “AWS ECR 0”으로 해석하지 않음

**추가 대응**

공격 표면을 줄이기 위해 final runtime을 distroless로 바꾸고 local Trivy를 같은 버전 0.72.0으로 비교했다. 현재 local candidate는 수정 가능한 HIGH/CRITICAL 0건이며, vendor-unfixed 결과는 release 후 AWS ECR native scan으로 재확인해야 한다.

### INC-05. distroless 이미지가 첫 기동에서 즉시 종료

**증상**

첫 distroless candidate container가 exit 2로 종료됐다.

~~~text
/usr/bin/python3.13: can't open file '/app/gunicorn'
~~~

**원인**

기존 Dockerfile의 CMD는 gunicorn executable을 직접 호출했다. distroless Python image의 entrypoint는 Python interpreter이므로 gunicorn을 Python script 파일로 해석했다.

**대응**

CMD를 Python module mode로 바꿨다.

~~~text
CMD ["-m", "gunicorn", ...]
~~~

**재검증**

- distroless app image build 성공
- gunicorn worker 기동 성공
- UID/GID 65532:65532 확인
- /healthz HTTP 200
- /metrics HTTP 200
- outbox parity metric 노출 확인

### INC-06. 존재하지 않는 health endpoint를 호출한 검증 오류

**증상**

첫 runtime smoke test에서 /health 호출 결과 404가 발생했다.

**원인**

애플리케이션 contract는 /healthz와 /readyz인데 검증 명령이 /health를 호출했다. 애플리케이션 장애가 아니라 테스트 oracle의 경로 오류였다.

**대응**

Kubernetes probe와 tests의 실제 계약인 /healthz로 재검증했다.

- /healthz: process liveness, DB 불필요, 200
- /readyz: DB 미연결 로컬 환경에서 의도된 503
- /metrics: 200

**배운 점**

smoke test는 “HTTP 200이 나오는가”보다 probe 계약과 의존성 의미를 확인해야 한다. liveness와 readiness를 하나로 합치면 DB 장애가 process restart storm으로 번질 수 있다.

### INC-07. 로컬 도구와 shell 환경의 차이

작업 중 다음은 애플리케이션 실패가 아닌 검증 환경 문제로 분리했다.

| 증상 | 원인 | 대응 |
| --- | --- | --- |
| local shell에서 trivy not found | Trivy가 host에 설치되지 않음 | 동일 버전 Trivy container 실행 |
| local shell에서 python not found | python alias가 없음 | python3 사용 |
| zsh에서 status 변수 선언 실패 | status가 zsh 예약 변수 | scan_state로 변수명 변경 |
| ECR scan status가 잠시 null | push 후 native scan eventual consistency | describe-image-scan-findings로 COMPLETE 확인 |

이런 문제를 앱 장애로 잘못 분류하지 않는 것도 운영 품질의 일부다. 실패한 명령 자체와 시스템의 상태 변화를 분리해서 기록했다.

### INC-08. GitOps bot commit으로 main과 feature가 분기됨

distroless 변경을 feature에 커밋하는 동안 이전 CD가 production kustomization을 갱신하는 bot commit을 main에 추가했다. 그 결과 feature는 기존 main을 기준으로 진행 중이고 main은 GitOps image update를 포함해 앞서가므로 fast-forward merge가 거부됐다.

~~~text
Diverging branches can't be fast-forwarded
~~~

reset이나 force push로 어느 쪽 이력을 지우지 않고, main의 bot manifest commit과 feature의 runtime/docs commit을 모두 보존하는 no-ff merge를 선택한다. 자동화 bot이 main에 commit하는 GitOps 전략에서는 release 직전에 origin/main을 fetch하고 branch divergence를 확인해야 한다.

## 5. 이전 운영 검증에서 누적된 장애 패턴

이번 release path 이전에도 저장소의 CI, Kubernetes, Terraform, AWS streaming, Rollout을 실제로 검증하면서 다음 문제를 겪었다. 이 항목들은 현재 설계의 배경이며, 기존 상세 로그는 docs/troubleshooting.md와 docs/lessons-learned.md에 남겨 두었다.

### 5.1 코드와 workflow의 drift

초기 workflow는 root Dockerfile을 빌드했지만 실제 애플리케이션 Dockerfile은 app/Dockerfile에 있었다. 삭제된 Kubernetes 경로와 실제 존재하지 않는 EKS cluster/계정 ARN도 workflow에 남아 있었다. 이 상태에서는 AWS 인증을 고쳐도 배포가 성공할 수 없었다.

검증과 배포를 다음처럼 분리하고 실제 repository 구조를 source of truth로 삼았다.

- CI: pytest, compileall, Docker build, Kustomize render, Terraform fmt/validate
- Security: dependency audit, Bandit, Trivy IaC/secret/image, CodeQL
- CD: OIDC, ECR immutable image, SBOM, attestation, GitOps manifest update

### 5.2 Terraform state와 CloudShell 저장공간

CloudShell에 Terraform과 provider를 임시 설치하면서 provider cache가 저장공간 대부분을 차지했고, apply 중 state 저장 단계에서 no space left on device가 발생했다. 일부 ECR/OIDC 리소스만 생성되고 state는 남지 않아 import와 소유권 복구가 필요했다.

이후 전체 workload state와 CD bootstrap state를 분리했다. 정상 운영에서는 remote backend와 lock을 사용하고, state가 확인되지 않은 상태에서 전체 apply를 반복하지 않는다. bootstrap 복구 외에는 target apply를 정상 배포 전략으로 사용하지 않는다.

### 5.3 계정 공용 리소스의 중복 소유

전체 stack을 만들 때 GitHub OIDC Provider가 이미 계정에 존재해 EntityAlreadyExists가 발생했다. OIDC Provider는 workload 애플리케이션이 소유할 리소스가 아니라 계정 공용 bootstrap 리소스이므로, workload stack에서는 data source로 읽고 bootstrap stack이 소유하도록 경계를 분리했다. ECR registry와 IAM Identity Center에도 같은 원칙을 적용한다.

### 5.4 AWS 인증 주체와 리전 혼동

Root session, IAM Identity Center permission set, Terraform process가 서로 다른 identity를 사용해 InvalidClientTokenId와 권한 오류가 반복될 수 있었다. 웹사이트 주소를 SSO start URL로 잘못 사용한 문제도 있었다.

모든 AWS 작업 전 다음을 실행하고 결과를 기록한다.

~~~bash
export AWS_PROFILE=develope-test
export AWS_REGION=eu-west-1
aws sts get-caller-identity
~~~

Root credential나 장기 access key를 Terraform/GitHub Actions에 사용하지 않는다.

### 5.5 Kubernetes schema와 네트워크 경계 오류

과거 manifest 검증에서 stale Deployment/Service 참조, 잘못된 Canary Service apiVersion, HPA의 Rollout 참조 오류, Ingress의 ingressClassName 위치 오류가 있었다. 실제 cluster에서는 RDS security group이 현재 EKS node/cluster security group을 허용하지 않아 Pod readiness가 DB timeout으로 실패했다.

대응은 manifest render만으로 끝내지 않았다.

- 모든 overlay를 kubectl kustomize로 렌더링
- schema와 resource name 참조를 검증
- Ingress 필드는 Kubernetes API schema에 맞춰 spec 아래에 배치
- RDS ingress에는 실제 애플리케이션 경계의 SG를 명시
- /healthz와 /readyz를 분리해 네트워크/DB 문제를 process crash와 구분

### 5.6 Firehose Parquet buffer 계약 위반

Parquet format conversion을 켜고 Firehose buffer를 5MB로 설정했을 때 AWS API가 다음 오류를 반환했다.

~~~text
BufferingHints.SizeInMBs must be at least 64 when data format conversion is enabled.
~~~

Parquet 변환을 포기하지 않고 64MB/60초로 조정했다. validation throughput에서는 size보다 60초 flush가 feedback latency를 좌우하므로 128MB/300초보다 검증 피드백이 빠르다. Terraform precondition으로 같은 조합을 plan 단계에서 차단했다.

### 5.7 부하·HPA·Rollout 관측 오류

- 50 req/s에서 HTTP error가 0%여도 k6 dropped iteration이 발생해 지속 가능 용량으로 인정하지 않았다.
- HPA가 CPU metric을 읽어도 단일 검증 노드 용량을 넘으면 Pending Pod가 된다. HPA, node capacity, Pod IP, DB connection budget을 같이 봐야 한다.
- Prometheus query가 no-data인데 AnalysisRun을 성공 처리하면 관측 불능 배포가 통과한다. 이번 설계는 stable 복귀를 우선한다.
- stable/canary Service가 같은 Pod를 scrape하면 metric이 중복 집계된다. query에서 pod 기준 중복 제거를 해야 한다.

운영 지표는 error rate 하나가 아니라 dropped iterations, p95/p99, readiness, DB 연결, analysis result와 함께 해석한다.

### 5.8 ECR image와 teardown의 숨은 의존성

ECR repository가 Terraform state에 있어도 image manifest가 남아 있으면 repository destroy가 실패할 수 있었다. 실험 환경에서는 image cleanup과 force_delete 여부를 teardown runbook에 포함하고, bootstrap ECR을 workload destroy와 분리한다.

실험의 성공 조건은 애플리케이션이 뜨는 것뿐 아니라 다음까지 포함한다.

- Terraform state resource count 0
- EKS/RDS/ALB/Kinesis/Firehose/ECR/S3/Secrets Manager/VPC/NAT/EIP 잔여 확인
- Cost Explorer의 Estimated 상태 확인
- 다음 실험에 재사용할 리소스와 삭제할 리소스의 소유권 확인

## 6. 검증 결과

### 코드와 golden path

| 검증 항목 | 결과 | 의미 |
| --- | --- | --- |
| 애플리케이션 pytest | 21 passed, 1 warning | outbox parity, health/readiness, metrics 계약 포함 |
| golden path generated pytest | 1 passed, 1 warning | 스캐폴더가 생성한 서비스도 기본 계약 보유 |
| app Docker build | 성공 | Python 3.13 builder + distroless runtime |
| generated service Docker build | 성공 | IDP template의 실제 build 가능성 확인 |
| app /healthz | 200 | process liveness |
| app /readyz without DB | 503 | dependency failure를 정상으로 위장하지 않음 |
| app /metrics | 200 | bounded HTTP metrics와 parity metric 노출 |
| runtime user | 65532:65532 | non-root |
| local Trivy fixable HIGH/CRITICAL | 0 | CD gate 기준 통과 |
| Kustomize render | 성공 | Rollout, AnalysisTemplate, probes 렌더링 |

### GitHub Actions evidence

최종 trixie release path에서 다음 workflow가 모두 성공했다.

- [CI 34040327473](https://github.com/masondev1024/aws-data-platform-gitops/actions/runs/34040327473)
- [Security 34040327623](https://github.com/masondev1024/aws-data-platform-gitops/actions/runs/34040327623)
- [CodeQL 34040327462](https://github.com/masondev1024/aws-data-platform-gitops/actions/runs/34040327462)
- [CD 34040327553](https://github.com/masondev1024/aws-data-platform-gitops/actions/runs/34040327553)

CD에서 확인한 단계는 다음과 같다.

1. GitHub OIDC로 AWS role assume
2. ECR login
3. immutable SHA image build/push
4. Trivy fixable HIGH/CRITICAL gate
5. SPDX SBOM 생성
6. production manifest update
7. Kustomize validation
8. release evidence 생성
9. image SBOM과 release evidence provenance attestation
10. GitOps commit push

### AWS 범위와 비용 상태

현재 실제 AWS에 남긴 것은 다음의 CD bootstrap 범위다.

- ECR repository: data-pipeline-app
- ECR lifecycle policy
- GitHubActionsDeployRole
- ECR push/pull inline policy

이번 검증에서 상시 생성하지 않은 리소스는 EKS, EC2 worker, NAT Gateway, ALB, RDS다. 따라서 이번 결과를 “전체 production EKS가 장시간 운영됐다”고 표현하지 않는다. 실제 표현은 “AWS OIDC → ECR → GitOps release path를 실 계정에서 검증했고, full workload stack은 비용 안전 게이트 뒤에 분리했다”가 정확하다.

로컬 validation container는 smoke test 후 제거했다. AWS bootstrap 리소스를 더 이상 사용하지 않을 때는 ECR image, ECR repository와 IAM role을 명시적으로 정리하고, 삭제 전 GitHub Actions variable과 workflow 의존성을 확인한다.

## 7. 현재 남은 리스크와 다음 검증

### 반드시 확인할 것

1. distroless commit 839311d를 feature와 main에 push한다.
2. CD가 새 image SHA tag를 ECR에 push하는지 확인한다.
3. 새 digest의 ECR Basic Scan이 COMPLETE가 될 때까지 기다린다.
4. ECR native CRITICAL/HIGH/MEDIUM/LOW 수치를 기록한다.
5. GitOps bot commit으로 production kustomization이 새 image SHA를 가리키는지 확인한다.

### 남은 설계 과제

- ECR Basic Scan의 vendor-unfixed CVE를 base image release cadence와 연결한다.
- ECR native scan 결과를 release evidence JSON에 자동 포함한다.
- full EKS validation profile을 짧은 시간으로 띄워 Argo CD/Rollouts parity gate를 실제 cluster에서 실행한다.
- Kafka relay의 consumer lag, duplicate rate, DLQ rate를 canary evidence와 연결한다.
- S3 Parquet/Iceberg의 schema evolution, partition pruning, compaction과 backfill runbook을 연결한다.
- secrets를 Kubernetes Secret에서 AWS Secrets Manager/External Secrets로 이동한다.

## 8. 면접에서 사용할 수 있는 문제 해결 스토리

### Situation

D2C 응모 승인 서비스에 Kafka와 GitOps를 연결했지만, 단순 publish 성공만으로는 DB 승인과 이벤트 발행의 정합성을 증명할 수 없었다.

### Task

개발팀이 반복 사용할 수 있는 플랫폼 수직 슬라이스를 만들고, 데이터 정합성·배포 안전성·공급망 보안·비용 통제를 실제 evidence로 증명해야 했다.

### Action

- transactional outbox로 DB 승인과 이벤트 의도를 원자화했다.
- writer DB parity SLI를 만들고 fail-closed canary analysis에 연결했다.
- GitHub OIDC와 immutable ECR image로 장기 key와 mutable tag를 제거했다.
- CI runner 차이, IAM role 부재, SBOM directory 누락을 실제 로그로 재현하고 수정했다.
- ECR native scan과 local Trivy의 정책 차이를 분리했다.
- multi-stage distroless non-root image로 runtime attack surface를 줄였다.
- scaffold script로 같은 guardrail을 새 서비스에도 자동 적용했다.

### Result

CI, Security, CodeQL, CD가 실제 GitHub Actions에서 성공했고, AWS OIDC부터 ECR push, SBOM, attestation, GitOps manifest update까지 연결됐다. 로컬 runtime에서는 health/readiness/metrics와 non-root 실행을 수치로 확인했다.

### 핵심 배운 점

운영 가능한 플랫폼은 “성공한 데모”가 아니라 “실패했을 때 원인을 좁히고, 잘못된 성공 신호를 차단하고, 복구 결과를 증거로 남기는 시스템”이다.

## 9. 주장할 수 있는 것과 주장하면 안 되는 것

### 주장할 수 있는 것

- AWS OIDC 기반 GitHub Actions → ECR → GitOps release path를 실 계정에서 검증했다.
- transactional outbox와 DB parity SLI를 애플리케이션과 rollout 정책에 연결했다.
- immutable image, SBOM, provenance attestation, CodeQL, Trivy gate를 구현했다.
- IDP golden path에서 서비스 생성부터 probe/canary/metrics 계약까지 자동화했다.
- 실제 실패 로그를 기반으로 runner portability, IAM trust, artifact lifecycle 문제를 수정했다.

### 주장하면 안 되는 것

- full EKS/RDS production을 장기간 운영했다고 말하면 안 된다.
- ECR native scan의 모든 finding이 0이라고 말하면 안 된다.
- Kafka exactly-once를 전체 시스템에서 보장한다고 말하면 안 된다. 현재 경계는 DB atomicity + at-least-once relay + idempotent consumer다.
- Backstage 전체 플랫폼이 운영 중이라고 말하면 안 된다. 현재는 golden path slice다.

이 경계를 지키는 것이 오히려 면접에서 신뢰를 만든다. 무엇을 구현했고 무엇을 아직 검증하지 않았는지 구분할 수 있어야 production engineer의 설명이 된다.
