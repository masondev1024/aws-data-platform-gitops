# Raffle load tests

이 디렉터리는 래플 응모 API의 용량과 안정성을 재현 가능한 방식으로 측정하기 위한 k6 시나리오를 보관한다. 현재 AWS의 EKS/RDS는 비용 절감을 위해 종료된 상태이므로, 이 파일은 테스트를 정의하지만 실행 결과를 의미하지는 않는다.

## 실행 모드

### 1. Health-only smoke

DB 없이 프로세스와 네트워크 경로만 확인한다.

```bash
BASE_URL=https://example.test MODE=health RATE=1 DURATION=30s \
  docker run --rm -i \
  -e BASE_URL -e MODE -e RATE -e DURATION \
  grafana/k6:latest run - < loadtest/raffle.js
```

### 2. Readiness baseline

읽기 DB까지 포함해 일정한 도착률로 readiness를 호출한다. `RATE`는 초당 요청 수이며, 테스트 대상이 감당할 수 있는 작은 값부터 단계적으로 올린다.

```bash
BASE_URL=https://example.test MODE=readiness RATE=5 DURATION=2m \
  docker run --rm -i \
  -e BASE_URL -e MODE -e RATE -e DURATION \
  grafana/k6:latest run - < loadtest/raffle.js
```

### 3. One-shot application path

각 VU가 고유한 테스트 계정을 만들고 로그인한 뒤 한 번 응모한다. 테스트용 상품과 DB에만 실행해야 한다.

```bash
BASE_URL=https://example.test MODE=apply APPLY_VUS=10 ITEM_ID=1 \
TEST_PASSWORD='use-a-short-lived-test-secret' RUN_ID="$(date +%s)" \
  docker run --rm -i \
  -e BASE_URL -e MODE -e APPLY_VUS -e ITEM_ID \
  -e TEST_PASSWORD -e RUN_ID \
  grafana/k6:latest run - < loadtest/raffle.js
```

`TEST_PASSWORD`는 셸 환경변수나 단기 자격증명으로만 주고 저장소에 기록하지 않는다. RDS read replica를 사용하는 환경에서는 회원가입 직후 로그인 요청이 replica lag에 걸릴 수 있으므로, 이 실패는 단순히 k6 threshold를 낮추기보다 lag와 read-after-write 정책의 문제로 분류해야 한다.

## 기록해야 할 결과

- k6: 요청 수, p50/p95/p99, error rate, checks 통과율
- Kubernetes: Pod 수, HPA desired/current replicas, CPU/memory, restart count
- MySQL: connections, CPU, lock/transaction latency, writer-to-reader replica lag
- 애플리케이션: `/metrics`의 HTTP status, request duration, `raffle_apply_requests_total` 결과별 건수
- 정확성: 성공 응모 수와 `UNIQUE(user_id, item_id)` 중복 방지 결과가 일치하는지

최고 단일 요청값을 용량으로 기록하지 않는다. 연속된 측정 구간에서 SLO를 만족한 가장 높은 부하를 지속 가능 용량의 하한으로 기록한다.
