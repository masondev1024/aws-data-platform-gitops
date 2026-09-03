# Troubleshooting Log

> 기준일: 2026-08-24
>
> 이 문서는 `aws-data-platform-gitops`의 CI/CD 및 AWS 테스트 환경을 복구하면서 실제로 관찰한 증상, 원인, 조치, 잔여 위험을 기록한다.

## 1. 초기 CI/CD 실패

### 증상

- 기존 GitHub Actions가 루트의 `Dockerfile`을 참조했지만 애플리케이션 Dockerfile은 `app/Dockerfile`에 있었다.
- `k8s/overlays/prod`가 존재하지 않았고, 기존 workflow가 삭제된 경로를 사용했다.
- AWS role secret/variable이 없어서 AWS credentials 단계에서 실패했다.
- Terraform workflow가 가짜 계정 ARN과 EKS cluster 이름을 사용했다.

### 원인

CI와 CD가 애플리케이션 구조 및 실제 인프라 구성과 동기화되어 있지 않았다. 검증 단계와 배포 단계가 분리되지 않아 AWS 인증 오류가 애플리케이션 검증까지 가렸다.

### 조치

- CI를 Python 테스트, Docker build, Kustomize render, Terraform fmt/validate로 분리했다.
- Docker build context를 `./app`으로 고정했다.
- CD는 GitHub OIDC로 AWS role을 assume하고 ECR에 SHA 태그 이미지를 push하도록 변경했다.
- Argo CD가 `main`의 `k8s/overlays/prod`를 감시하는 GitOps 흐름으로 정리했다.

## 2. Python 테스트가 실행되지 않던 문제

### 증상

테스트 파일이 없어 `pytest`가 exit code 5로 종료될 수 있었다.

### 조치

- `app/requirements-dev.txt`를 추가했다.
- `/healthz`, `/readyz`의 정상/실패 동작을 포함한 애플리케이션 테스트를 추가했다.
- Docker 이미지와 로컬 pytest를 모두 실행해 검증했다.

## 3. Kubernetes manifest 오류

### 증상

- Base kustomization이 삭제된 `deployment.yaml`, `service.yaml`을 참조했다.
- Canary Service가 `apiVersion: v2`였다.
- HPA가 잘못된 API 버전과 Rollout 이름을 참조했다.
- Ingress indentation 및 overlay의 image/리소스 참조가 일관되지 않았다.

### 조치

- Rollout, stable/canary Service, HPA, Ingress, CronJob을 base에 명시했다.
- HPA를 `argoproj.io/v1alpha1` Rollout 대상으로 수정했다.
- prod/v1/v2 overlay의 image placeholder와 이름 suffix 참조를 정리했다.
- `kubectl kustomize`와 YAML parse를 모든 overlay에 대해 실행했다.

## 4. AWS SSO 로그인 문제

### 증상

`https://ask-seoul.kr`을 SSO start URL로 입력했을 때 `Invalid start url provided`가 발생했다.

### 원인

일반 웹사이트 주소는 IAM Identity Center의 start URL이 아니다.

### 조치

IAM Identity Center 설정에서 실제 AWS access portal URL을 확인했다. 이 프로젝트에서 확인된 형식은 다음과 같다.

```text
https://d-xxxxxxxxxx.awsapps.com/start
```

SSO region은 애플리케이션 배포 리전이 아니라 IAM Identity Center 인스턴스의 기본 리전이다. CLI 로그인 후에는 `aws sts get-caller-identity`로 계정과 role을 검증해야 한다.

## 5. Root 세션과 SSO 세션 혼동

### 증상

AWS Console의 Root 세션으로 연 CloudShell은 `arn:aws:iam::<account>:root`로 동작했다. 같은 브라우저에서 AWS access portal의 `AdministratorAccess/mason1024` 세션은 `AWSReservedSSO_AdministratorAccess_...` role로 동작했다.

### 조치

실제 작업은 AWS access portal에서 permission set을 선택한 SSO 세션의 CloudShell에서 수행했다. Root는 초기 계정/Identity Center 설정에만 사용해야 하며 Terraform과 GitHub Actions에 Root credentials를 사용하지 않는다.

## 6. Terraform CloudShell 도구와 저장공간 문제

### 증상

- CloudShell에 Terraform이 기본 설치되어 있지 않았다.
- Terraform 1.9.8 binary를 임시 설치한 뒤 provider cache가 약 820MB를 차지했다.
- CloudShell의 약 1GB 저장공간이 가득 차 Terraform apply가 리소스 생성 이후 state 저장 단계에서 `no space left on device`로 실패했다.
- apply 이후 확인 결과 ECR repository와 GitHub OIDC provider는 생성됐지만 IAM role은 생성되지 않았다.
- state 파일이 저장되지 않아 생성된 리소스를 import해야 하는 상태가 됐다.
- provider cache 일부를 정리한 뒤에는 Terraform lock file이 요구하는 Helm/Kubernetes/Random/TLS provider가 없어 import도 실행되지 않았다.

### 현재 상태

- ECR repository `data-pipeline-app`: 생성됨
- GitHub Actions OIDC provider: 생성됨
- GitHub Actions IAM role `GitHubActionsDeployRole`: 생성됨
- role policy: ECR push/pull 및 EKS read-only 권한으로 생성됨
- 원래 전체 인프라 root module의 remote state: 아직 구성되지 않음
- CloudShell에서 실패한 local state: 복구 대상에서 제외하고 별도 bootstrap state로 재생성함

로컬의 AWS-only bootstrap stack에서 기존 ECR/OIDC를 import한 뒤 lifecycle policy, GitHub role, inline policy를 apply했다. 이후 GitHub Actions CD를 재실행해 OIDC 인증, ECR login, image push, prod manifest 갱신까지 성공했다.

### 복구 원칙

1. 상태 파일이 없는 상태에서 전체 apply를 반복하지 않는다.
2. 실제 AWS 리소스를 먼저 조회하고, 확인된 리소스만 `terraform import`한다.
3. Terraform provider cache를 재설치할 저장공간을 확보한다.
4. 다음 실행부터는 S3 backend와 DynamoDB lock 또는 동등한 원격 state를 먼저 구성한다.
5. `-target`은 이번과 같은 bootstrap 복구에만 사용하고 정상 운영에서는 전체 plan을 사용한다.

## 7. GitHub CD 실패

### 증상

main merge 직후 CD가 다음 이유로 실패했다.

```text
Repository variable AWS_ROLE_TO_ASSUME is not configured.
```

### 원인

GitHub Actions가 assume할 OIDC role ARN이 저장소 변수에 등록되지 않았다.

### 조치 및 결과

- `github_actions_role_arn`에 해당하는 role을 생성/확인했다.
- GitHub repository variable `AWS_ROLE_TO_ASSUME`에 role ARN을 등록했다.
- `TF_VAR_DB_PASSWORD`를 GitHub Actions secret으로 등록했다. 값은 코드와 로그에 출력하지 않았다.
- CD 재실행 결과 OIDC 인증, ECR push, image SHA 갱신, Kustomize validation, manifest commit이 모두 성공했다.

Terraform 전체 plan은 별도 권한이 필요하다. 현재 CD role은 의도적으로 ECR push 중심의 최소 권한이므로, 전체 인프라 plan/apply에는 별도의 Terraform role과 workflow variable을 사용해야 한다.

## 14. Terraform이 AWS SSO 세션을 못 읽는 경우

### 증상

저비용 validation 프로필의 `terraform apply`가 리소스 생성 전에 다음 오류로 종료될 수 있다.

```text
InvalidClientTokenId: The security token included in the request is invalid
```

### 원인

AWS CLI에서 `AWS_PROFILE=develope-test aws sts get-caller-identity`를 실행한 것만으로는 별도 프로세스인 Terraform에 profile이 전달되지 않는다. Terraform 명령에도 `AWS_PROFILE`/`AWS_REGION`을 export하거나 명령 앞에 명시해야 한다. 이번 재현에서는 AWS CLI의 SSO 세션은 유효했지만 Terraform에는 profile 환경 변수가 없어 기본 자격증명이 선택됐다.

### 복구와 예방

```bash
export AWS_PROFILE=develope-test
export AWS_REGION=eu-west-1
aws sts get-caller-identity
terraform -chdir=terraform/validation plan -input=false
```

validation runbook에도 같은 export를 포함했다. 적용 전에는 반드시 계정 ID와 ARN을 확인하고, Terraform plan이 읽기 단계까지 성공한 뒤에만 apply한다. 자격증명을 파일에 복사하거나 장기 access key를 만들지 않는다.

## 15. Firehose Parquet 변환과 5MB 버퍼 조합

### 증상

Parquet 변환을 켠 validation profile에서 Firehose 생성이 다음 오류로 거부됐다.

```text
BufferingHints.SizeInMBs must be at least 64 when data format conversion is enabled.
```

### 원인과 결정

Firehose record format conversion은 64MB 미만의 `SizeInMBs`를 허용하지 않는다. 처음에는 freshness를 빠르게 하려고 `5MB/60초`를 선택했지만, 이 조합은 API 계약과 충돌했다. Parquet 데이터 계약 검증을 포기하지 않고 `64MB/60초`로 수정했다. 낮은 validation 처리량에서는 size보다 60초 interval이 flush를 결정하므로 128MB/300초 대비 feedback 지연은 줄어든다.

### 예방

`terraform/validation` Firehose resource에 plan 단계 `precondition`을 추가해 Parquet 변환과 64MB 미만 버퍼 조합을 AWS API 호출 전에 차단했다. raw JSON만 의도적으로 검증하는 별도 경우에만 Parquet 변환을 끄고 5MB를 선택한다. 공식 근거는 [Firehose record format conversion 문서](https://docs.aws.amazon.com/firehose/latest/dev/enable-record-format-conversion.html)다.

## 8. Repository rename 이후 OIDC assume-role 실패

### 증상

저장소명을 `develope-project`에서 `aws-data-platform-gitops`로 변경한 뒤, GitHub Actions의 AWS 인증 단계에서 다음 오류가 발생했다.

```text
Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```

### 원인

이번 실패 run 당시 저장소 OIDC 설정은 `use_default=true`, `use_immutable_subject=false`였다. 이 저장소는 2026-04-06에 생성되어 GitHub의 immutable subject 자동 적용 대상이 아니었는데, AWS IAM role trust policy만 immutable subject 형식을 요구하고 있었다. 따라서 GitHub가 발행한 기본 `sub`와 IAM의 `StringEquals` 조건이 불일치했다.

IAM이 요구한 형식은 다음과 같았다.

```text
repo:masondev1024@269997727/aws-data-platform-gitops@1202584860:ref:refs/heads/main
```

### 조치

1. GitHub repository OIDC를 immutable subject로 명시적으로 opt-in했다.

   ```bash
   gh api --method PUT \
     repos/masondev1024/aws-data-platform-gitops/actions/oidc/customization/sub \
     -F use_default=false \
     -F use_immutable_subject=true
   ```

2. 전체 EKS/NAT/RDS를 복구하지 않고 `bootstrap-terraform`으로 CD에 필요한 ECR repository, lifecycle policy, IAM role, inline policy만 재생성했다. Terraform apply 결과는 `4 added, 0 changed, 0 destroyed`였다.
3. 실패 run `32715848652`의 failed job을 재실행했다. OIDC 인증, ECR 로그인, 이미지 build/push, production manifest validation, Git commit이 모두 성공했다.

검증 job: https://github.com/masondev1024/aws-data-platform-gitops/actions/runs/32715848652/job/97429253921
검증 결과: https://github.com/masondev1024/aws-data-platform-gitops/actions/runs/32715848652

### 운영 교훈

GitHub 저장소명을 변경할 때는 URL, Argo CD `repoURL`, Terraform 변수뿐 아니라 OIDC customization 상태와 AWS trust policy의 subject claim을 함께 확인해야 한다. immutable subject를 사용할 경우 GitHub repository에서 opt-in한 뒤 IAM policy를 적용해야 하며, branch/environment 범위는 `StringEquals`로 최소화한다.

## CD bootstrap 리소스와 workload teardown 분리

### 증상

전체 workload 인프라를 비용 때문에 destroy한 뒤 CD가 ECR login 이전 단계에서 실패했다. EKS, NAT, RDS는 없어도 이미지 publish는 가능하지만, CD가 사용할 ECR repository와 IAM role까지 함께 삭제된 상태였다.

### 조치

CD bootstrap은 다음 리소스만 별도 state로 소유한다.

- GitHub Actions OIDC provider — 계정 공용 리소스
- `data-pipeline-app` ECR repository와 lifecycle policy
- `GitHubActionsDeployRole`과 최소 ECR push/pull inline policy

Workload destroy에서는 이 bootstrap state를 대상으로 `destroy`하지 않는다. 복구 시에는 다음처럼 최소 stack만 먼저 확인·적용한다.

```bash
cd /path/to/bootstrap-terraform
AWS_PROFILE=develope-test AWS_REGION=eu-west-1 terraform plan
AWS_PROFILE=develope-test AWS_REGION=eu-west-1 terraform apply -auto-approve
```

복구 후에는 `terraform plan`이 `No changes`인지 확인하고, EKS/NAT/RDS를 다시 생성하지 않은 상태에서 CD를 재실행한다. 장기적으로는 bootstrap state도 로컬 파일이 아니라 별도 원격 backend에서 관리해 workload state와 소유권을 명확히 분리한다.

## 16. Ingress 필드 위치 오류

### 증상

실제 EKS에 production manifest를 적용할 때 다음 오류로 Ingress 생성이 거절됐다.

```text
strict decoding error: unknown field "metadata.ingressClassName"
```

### 원인과 조치

`ingressClassName`이 `metadata` 아래에 있었고 Kubernetes Ingress schema가 요구하는 `spec` 아래가 아니었다. 필드를 `spec.ingressClassName`으로 이동한 뒤 Kustomize render와 실제 ALB reconciliation을 다시 확인했다. 이후 ALB hostname이 발급되고 `/healthz`와 `/readyz`가 200을 반환했다.

## 17. RDS 보안 그룹과 EKS 노드 보안 그룹 불일치

### 증상

Rollout Pod가 처음에는 RDS 연결 timeout으로 Ready가 되지 않았다. RDS가 private endpoint라서 애플리케이션 문제처럼 보였지만, 애플리케이션 로그를 확인한 뒤 네트워크 문제로 범위를 좁혔다.

### 원인과 조치

RDS SG는 예전 `app-node-sg`만 허용하고 있었고, 현재 EKS managed node ENI는 EKS cluster security group을 사용했다. Terraform RDS ingress에 애플리케이션 SG와 EKS cluster SG를 명시적으로 추가했다. Terraform plan은 `0 add, 1 change, 0 destroy`였고, 적용 후 오류가 `Unknown database 'raffle_db'`로 바뀌어 네트워크 경로가 복구됐음을 확인했다. 이후 `/init-db`를 1회 실행해 schema를 만들고 Rollout이 2/2 Ready가 됐다.

## 18. HPA가 단일 검증 노드의 Pod 수용량을 초과한 문제

### 증상

k6 readiness 50 req/s 단계 후 HPA가 CPU 지표를 읽고 desired replicas를 11까지 계산했다. 단일 t3.medium 노드의 allocatable Pod 상한은 17개였고, 기존 system/observability Pod 때문에 애플리케이션 Pod 4개만 배치되고 7개가 `Pending`이었다. Pending 원인은 CPU가 아니라 `Too many pods`였다.

### 조치

검증 프로필은 노드 자동 확장 실험이 목적이 아니므로 HPA `maxReplicas`를 20에서 4로 낮추고, scale-up/down을 각각 1 Pod/60초로 제한했다. 적용 후 HPA는 4개 이하로 정리됐고 약 60초 안에 2 replicas/2 Ready로 안정화됐다. 운영 환경에서 상한을 높일 때는 Karpenter/node autoscaling 예산과 Pod capacity 테스트를 함께 통과시켜야 한다.

## 19. Argo Rollouts AnalysisRun 실패와 자동 rollback

### 재현

검증 목적으로 canary 5xx 분석 조건을 일시적으로 항상 거짓이 되도록 바꾸고 Pod template annotation만 변경해 revision 2를 만들었다. 10% traffic weight와 2분 pause 뒤 AnalysisRun이 실행됐고, canary 메트릭이 아직 없어 `slice index out of range` 오류가 발생했다.

### 결과와 교훈

AnalysisRun이 `Error`가 되자 Rollout은 `Degraded`로 전환되고 canary ReplicaSet을 0으로 줄여 stable revision으로 자동 복귀했다. 원래 AnalysisTemplate을 복원하고 annotation을 제거하자 `Healthy`, step 6, stable 100% 상태가 됐다. 메트릭이 없는 canary를 성공으로 처리하지 않고 fail-closed한 것은 안전하지만, 실제 배포 runbook에는 synthetic canary traffic과 no-data guard를 추가해야 한다.

## 20. Pod 장애와 DB 장애 복구 훈련

### Pod 장애

Ready Pod 한 개를 삭제했다. ALB `/healthz`는 계속 200이었고, 새 비종료 Pod가 Ready가 되어 2개 Ready로 복구되는 시점은 약 2초로 관측됐다. Rollout은 `Healthy`로 유지됐다.

### DB 장애

한 Pod가 새로 시작할 때만 DB writer/reader endpoint를 `127.0.0.1`로 주입해 DB 연결 실패를 주입했다. 해당 Pod는 Ready에서 제외됐지만 다른 Pod는 ALB 200을 계속 반환했다. 원래 endpoint를 복원하고 실패 Pod를 교체한 뒤 약 9초 내 2개 Ready/Healthy로 회복됐다. readiness에는 DB 의존성을 두되 liveness에는 두지 않은 설계가 실제 장애 격리에 기여했다.

## 21. 부하 테스트 계정명 충돌

### 증상

중복 응모 검증을 재실행했는데 고유 nonce를 붙였다고 생각한 계정이 계속 회원가입 400을 반환했다.

### 원인과 조치

zsh에서 `$USERNAME`은 로그인 사용자명으로 이미 정의된 특수 환경값이라 테스트 스크립트의 대입값이 의도대로 사용되지 않았다. 실제 요청에는 기존 사용자명이 들어갔고, 애플리케이션의 정상적인 unique constraint 400이었다. 테스트 변수명을 `TEST_USER`/`TEST_PASS`로 바꾸고 UUID를 사용한 뒤 회원가입 200, 로그인 200, 최초 응모 200, 중복 응모 400을 재현했다. 테스트 자격증명은 파일이나 문서에 저장하지 않았다.

## 22. stable/canary ServiceMonitor 중복 계측

Rollout 완료 후 stable과 canary Service가 같은 stable Pod hash를 가리키면서 하나의 Pod가 두 ServiceMonitor에 의해 스크랩될 수 있었다. 원시 카운터를 단순 `sum`하면 응모 성공 수가 실제보다 2배로 보였다. Grafana와 PrometheusRule을 `max by (pod, ...)` 후 집계하도록 수정해 duplicate scrape를 제거했고, 현재 정확성 집계는 Pod 기준 dedup을 사용한다. AnalysisTemplate은 `service`를 명시적으로 필터링해 canary 범위를 분리한다.

## 23. Stateful workload teardown 뒤 고아 EBS 확인

단기 EKS destroy 뒤에도 Kubernetes PVC가 만든 EBS volume은 데이터 보존 정책에 따라 남을 수 있다. 이번 감사에서는 테스트 cluster tag와 PVC tag가 일치하는 `available` 1GiB volume을 발견했고 attachment가 없는 것을 확인한 뒤 명시적으로 삭제했다. 이후 EC2 `describe-volumes`에서 `InvalidVolume.NotFound`와 전용 tag 재조회 빈 결과를 확인했다. Resource Groups Tagging API는 삭제 ARN을 잠시 반환했으므로 태그 인덱스보다 실제 EC2 control plane을 teardown의 최종 판정으로 삼는다. 다음 teardown에도 `describe-volumes`에서 cluster/PVC tag를 기준으로 잔여 volume을 확인한다.
