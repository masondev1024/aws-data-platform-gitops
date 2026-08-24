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
