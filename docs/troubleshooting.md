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

## 8. Repository rename 이후 OIDC assume-role 실패

### 증상

저장소명을 `develope-project`에서 `aws-data-platform-gitops`로 변경한 뒤, GitHub Actions의 AWS 인증 단계에서 다음 오류가 발생했다.

```text
Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```

### 원인

2026-07-15 이후 GitHub는 새로 생성되거나 이름이 변경된 저장소의 OIDC `sub` claim에 owner/repository ID를 포함하는 immutable subject 형식을 적용한다. 이름만 포함한 기존 trust condition은 새 토큰과 일치하지 않는다.

### 조치

IAM trust policy를 다음 형식으로 변경했다.

```text
repo:masondev1024@269997727/aws-data-platform-gitops@1202584860:ref:refs/heads/main
```

Terraform에도 `github_owner_id`와 `github_repo_id`를 명시해 저장소 이름 변경에 영향을 받지 않는 subject를 사용하도록 반영했다. 이후 새 `main` 커밋에서 CD를 재실행해 OIDC 인증을 검증한다.

### 운영 교훈

GitHub 저장소명을 변경할 때는 URL, Argo CD `repoURL`, Terraform 변수뿐 아니라 OIDC trust policy의 subject claim도 함께 확인해야 한다. 장기적으로는 owner/repository ID 기반 claim을 사용하고, branch/environment 범위는 `StringEquals`로 최소화한다.
