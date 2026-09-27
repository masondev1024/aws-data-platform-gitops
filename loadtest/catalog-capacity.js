import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';

const baseURL = (__ENV.BASE_URL || '').replace(/\/$/, '');
const rate = Number(__ENV.CATALOG_RATE || 100);
const seconds = Number(__ENV.CATALOG_SECONDS || 60);
if (!/^https:\/\/[A-Za-z0-9-]+\.ap-northeast-2\.elb\.amazonaws\.com$/.test(baseURL)) {
  throw new Error('Use the exact Seoul session ALB HTTPS endpoint.');
}
if (!Number.isSafeInteger(rate) || !Number.isSafeInteger(seconds) || rate <= 0 || seconds <= 0 || rate * seconds > 20000) {
  throw new Error('The bounded capacity profile requires 1–20,000 planned requests.');
}

export const options = {
  insecureSkipTLSVerify: __ENV.ALLOW_SELF_SIGNED_TLS === 'true',
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)'],
  scenarios: {
    catalogue: {
      executor: 'constant-arrival-rate', rate, timeUnit: '1s',
      duration: `${seconds}s`, preAllocatedVUs: 100, maxVUs: 300,
      // Finish requests already started; this does not schedule extra traffic.
      gracefulStop: '10s',
    },
  },
  thresholds: {
    checks: ['rate>0.99'], http_req_failed: ['rate<0.01'],
    dropped_iterations: ['count==0'], iterations: [`count>=${rate * seconds}`],
    http_reqs: [`count==${rate * seconds}`],
    http_req_duration: ['p(95)<500', 'p(99)<1000'],
  },
};

export default function () {
  // Arrival-rate scheduling can include the exact end boundary. Cap actual HTTP traffic.
  if (exec.scenario.iterationInTest >= rate * seconds) return;
  const response = http.get(`${baseURL}/`, { redirects: 0, timeout: '10s' });
  check(response, {
    'catalogue returns HTTP 200': r => r.status === 200,
    'catalogue page is rendered': r => !!r.body && r.body.includes('WEEKLY'),
  });
}

export function handleSummary(data) {
  if (!__ENV.SUMMARY_FILE) throw new Error('SUMMARY_FILE is required for measured evidence.');
  return {
    [__ENV.SUMMARY_FILE]: JSON.stringify(data, null, 2),
    stdout: JSON.stringify({mode: 'catalog-capacity', rate, seconds, metrics: data.metrics}, null, 2),
  };
}
