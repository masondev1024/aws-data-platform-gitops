# 🎟️ High-Concurrency Raffle Service & K8s Automation

## 1. 프로젝트 개요
- **목적**: 대규모 동시 접속자(수만 명)가 발생하는 래플(추첨) 이벤트 상황을 가정하여, 애플리케이션의 성능 한계를 테스트하고 클라우드 네이티브 인프라(Kubernetes)를 통한 유연한 확장성(Auto-scaling) 및 무중단 배포를 검증합니다.
- **핵심 목표**:
  1. **부하 테스트 및 병목 분석**: JMeter / k6를 활용한 대규모 트래픽 발생 및 시스템 한계점 확인.
  2. **동적 오토스케일링**: HPA(Pod 확장) 및 Karpenter(Node 확장)를 연동하여 트래픽 스파이크에 대응.
  3. **안전한 배포 전략**: CI/CD 파이프라인과 트래픽 가중치 기반의 Canary 배포 구현.

## 2. 시스템 아키텍처

- **Application**: Python (Flask)
- **Database**: MySQL (Master-Replica Replication)
- **Infrastructure**: AWS EKS 
- **Auto-scaling**: HPA , Karpenter
- **CI/CD & Deployment**: GitHub Actions, ArgoCD, Argo Rollouts (Canary)
- **Load Testing**: k6
- **Monitoring**: Grafana

## 3. 핵심 검증 포인트 (Test Scenarios)

### ① 부하 테스트 (Load Testing)
- **시나리오**: 특정 시간에 래플 응모 API(`/api/apply`)에 10,000명의 동시 접속 요청.
- **검증 항목**: 
  - Master DB의 INSERT 락(Lock) 및 커넥션 부하.
  - Replica DB의 SELECT 읽기 분산 효율.

### ② 오토스케일링 동작 검증 (HPA & Karpenter)
- **Trigger**: Pod의 CPU/Memory 사용률이 임계치(예: 70%)를 초과할 때.
- **검증 항목**:
  - HPA에 의해 Flask Pod 개수가 정상적으로 스케일 아웃(Scale-out) 되는가?
  - Node 자원이 부족할 때, **Karpenter**가 즉시 개입하여 새로운 EC2 Node를 1분 이내에 프로비저닝 하는가?

### ③ 무중단 Canary 배포 (Canary Deployment)
- **시나리오**: 새로운 UI/기능이 추가된 v2.0 버전을 배포.
- **검증 항목**:
  - 트래픽의 10%만 새로운 버전의 Pod으로 라우팅되는지 확인 (Argo Rollouts 사용).
  - 에러율 모니터링 후, 이상이 없을 시 50% ➡️ 100%로 점진적 트래픽 전환 검증.

## 4. 디렉토리 구조
```text
raffle-project/
├── app/                  # Flask 애플리케이션 소스 코드
│   ├── app.py
│   ├── templates/
│   └── requirements.txt
├── k8s/                  # Kubernetes 매니페스트 파일
│   ├── deployment.yaml
│   ├── hpa.yaml
│   ├── karpenter-provisioner.yaml
│   └── argo-rollout.yaml # Canary 배포 설정
├── load_test/            # 부하 테스트 스크립트
│   └── raffle_test.js    # k6 테스트 스크립트
├── .github/workflows/    # CI/CD 파이프라인 정의
└── README.md