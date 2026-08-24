# Repository About Draft

## 추천 About 한 줄

기존 래플 트래픽 응모 시스템의 미완성 운영 인프라를 확장해 AWS·Terraform·EKS·GitOps 기반 CI/CD와 Canary 배포를 구현한 데이터 플랫폼 프로젝트

## 조금 더 기술적인 대안

Terraform으로 AWS 데이터 플랫폼 인프라를 관리하고 GitHub Actions OIDC, ECR, EKS, Argo CD/Rollouts로 보안성 있는 GitOps CI/CD를 구현한 프로젝트

## README/포트폴리오용 설명

기존 래플 트래픽 응모 시스템에서 구현하지 못했던 운영 단계의 인프라와 배포 자동화를 후속으로 확장한 프로젝트입니다. Terraform으로 AWS 네트워크와 데이터 계층을 코드화하고, GitHub Actions OIDC를 통해 장기 access key 없이 ECR에 이미지를 배포합니다. Argo CD가 Git의 Kubernetes manifest를 동기화하고 Argo Rollouts canary 전략으로 점진 배포를 수행하도록 구성했습니다. Prometheus 기반 SLO/SLI, 장애 대응 runbook, 비용 승인 게이트와 short-lived validation profile까지 추가해 운영 가능성과 비용 통제를 함께 검증했습니다.

애플리케이션에는 `/healthz`와 DB 연결을 검증하는 `/readyz`를 분리해 장애 격리와 트래픽 제어를 고려했고, 이미지에는 commit SHA 태그를 사용해 배포 재현성을 높였습니다. CI에서는 애플리케이션 테스트, Docker build, Kustomize render, Terraform validation을 자동 검증합니다.

추가 검증에서는 전체 104개 리소스 구성을 14개 streaming validation profile로 축소해 Kinesis → Firehose → S3 Parquet 경로를 7,200건 전송으로 확인했습니다. 별도 short-lived EKS/RDS profile에서는 readiness 20 req/s를 30초 동안 601/601 성공(p95 354.5ms, p99 406.5ms), one-shot 응모 30 VU 90/90 성공, HPA·Argo rollback·Pod/DB 복구를 실제로 검증했습니다. 3-region/2-CDN 장애 전환 정책은 AWS CDN 비용 없이 로컬에서 구현해 2초 failover RTO와 전환 후 추가 실패 0건을 재현했습니다. 실제 멀티리전 CDN·라이브 미디어 운영과 RDS replica lag은 별도 미검증으로 명확히 구분합니다.

## GitHub Topics 초안

```text
aws, data-platform, data-engineering, terraform, kubernetes, eks,
gitops, argocd, argo-rollouts, github-actions, oidc, ecr, cicd,
infrastructure-as-code, canary-deployment
```
