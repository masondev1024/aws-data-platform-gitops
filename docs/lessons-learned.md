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
