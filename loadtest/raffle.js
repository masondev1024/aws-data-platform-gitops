import http from 'k6/http';
import { check } from 'k6';

const baseURL = (__ENV.BASE_URL || 'http://localhost:8080').replace(/\/$/, '');
const mode = __ENV.MODE || 'readiness';
const rate = Number(__ENV.RATE || 5);
const duration = __ENV.DURATION || '30s';
const applyVUs = Number(__ENV.APPLY_VUS || 10);
const runID = __ENV.RUN_ID || `${Date.now()}`;

if (mode === 'apply' && !__ENV.TEST_PASSWORD) {
  throw new Error('MODE=apply requires TEST_PASSWORD; do not commit credentials to the repository.');
}

export const options = mode === 'apply'
  ? {
      scenarios: {
        apply_once: {
          executor: 'per-vu-iterations',
          vus: applyVUs,
          iterations: 1,
          maxDuration: __ENV.MAX_DURATION || '2m',
        },
      },
      thresholds: {
        checks: ['rate>0.99'],
        http_req_failed: ['rate<0.01'],
        'http_req_duration{endpoint:signup}': ['p(95)<1000', 'p(99)<1500'],
        'http_req_duration{endpoint:login}': ['p(95)<1000', 'p(99)<1500'],
        'http_req_duration{endpoint:apply}': ['p(95)<1000', 'p(99)<1500'],
      },
    }
  : {
      scenarios: {
        readiness: {
          executor: 'constant-arrival-rate',
          rate,
          timeUnit: '1s',
          duration,
          preAllocatedVUs: Number(__ENV.PRE_ALLOCATED_VUS || 10),
          maxVUs: Number(__ENV.MAX_VUS || 50),
        },
      },
      thresholds: {
        checks: ['rate>0.99'],
        http_req_failed: ['rate<0.01'],
        'http_req_duration{endpoint:readiness}': ['p(95)<500', 'p(99)<1000'],
      },
    };

const jsonParams = (endpoint) => ({
  headers: { 'Content-Type': 'application/json' },
  tags: { endpoint },
});

function checkStatus(response, name, expectedStatus) {
  return check(response, {
    [`${name} returned ${expectedStatus}`]: (value) => value.status === expectedStatus,
  });
}

export default function () {
  if (mode === 'health') {
    const response = http.get(`${baseURL}/healthz`, { tags: { endpoint: 'health' } });
    checkStatus(response, 'health', 200);
    return;
  }

  if (mode !== 'apply') {
    const response = http.get(`${baseURL}/readyz`, { tags: { endpoint: 'readiness' } });
    checkStatus(response, 'readiness', 200);
    return;
  }

  const username = `${__ENV.TEST_USERNAME_PREFIX || 'k6_user'}_${runID}_${__VU}`;
  const password = __ENV.TEST_PASSWORD;
  const signup = http.post(
    `${baseURL}/api/signup`,
    JSON.stringify({ username, password }),
    jsonParams('signup'),
  );
  if (!checkStatus(signup, 'signup', 200)) {
    return;
  }

  const login = http.post(
    `${baseURL}/api/login`,
    JSON.stringify({ username, password }),
    jsonParams('login'),
  );
  if (!checkStatus(login, 'login', 200)) {
    return;
  }

  const apply = http.post(
    `${baseURL}/api/apply`,
    JSON.stringify({ item_id: Number(__ENV.ITEM_ID || 1) }),
    jsonParams('apply'),
  );
  checkStatus(apply, 'apply', 200);
}
