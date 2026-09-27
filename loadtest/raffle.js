import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';
import exec from 'k6/execution';

const baseURL = (__ENV.BASE_URL || 'http://localhost:8080').replace(/\/$/, '');
const mode = __ENV.MODE || 'readiness';
const readRate = Number(__ENV.READ_RATE || 20);
const readDuration = __ENV.READ_DURATION || '60m';
const burstRate = Number(__ENV.BURST_RATE || 100);
const burstDuration = __ENV.BURST_DURATION || '5m';
const applyRate = Number(__ENV.APPLY_RATE || 5);
const applyDuration = __ENV.APPLY_DURATION || '10m';
const refreshVus = Number(__ENV.REFRESH_VUS || 10000);
const maxPlannedRequests = Number(__ENV.MAX_PLANNED_REQUESTS || 200000);
const runID = (__ENV.RUN_ID || `${Date.now()}`).replace(/[^A-Za-z0-9_-]/g, '').slice(-24);
const usernamePrefix = __ENV.TEST_USERNAME_PREFIX || 'k6';
const allowSelfSignedTLS = __ENV.ALLOW_SELF_SIGNED_TLS === 'true';

const canaryApplyAttempts = new Counter('raffle_canary_apply_attempts');
const canaryApplySuccesses = new Counter('raffle_canary_apply_successes');
const responseFailures = new Counter('raffle_response_failures');

function durationSeconds(value) {
  const match = value.match(/^(\d+)(s|m|h)$/);
  if (!match) throw new Error(`Unsupported duration: ${value}; use Ns, Nm, or Nh`);
  const factor = { s: 1, m: 60, h: 3600 }[match[2]];
  return Number(match[1]) * factor;
}

const plannedRequests = mode === 'soak'
  ? readRate * durationSeconds(readDuration) + burstRate * durationSeconds(burstDuration)
  : mode === 'synchronized-refresh'
    ? refreshVus
  : mode === 'canary-apply'
    ? applyRate * durationSeconds(applyDuration) * 4
    : mode === 'apply'
      ? Number(__ENV.APPLY_VUS || 10) * 4
      : Number(__ENV.RATE || 5) * durationSeconds(__ENV.DURATION || '30s');

if (!Number.isSafeInteger(plannedRequests) || plannedRequests > maxPlannedRequests) {
  throw new Error(
    `Planned HTTP requests (${plannedRequests}) exceed MAX_PLANNED_REQUESTS (${maxPlannedRequests}); ` +
    'the six-hour lab ledger has a 200,000 request ceiling.',
  );
}

if (['soak', 'synchronized-refresh', 'canary-apply'].includes(mode) && !baseURL.startsWith('https://')) {
  throw new Error(`${mode} must target the HTTPS ALB endpoint; HTTP does not exercise the Secure cookie path.`);
}

if (['apply', 'canary-apply'].includes(mode) && !__ENV.TEST_PASSWORD) {
  throw new Error('Write modes require TEST_PASSWORD; do not commit credentials to the repository.');
}
if (['apply', 'canary-apply'].includes(mode) && !/^[A-Za-z0-9_-]{1,8}$/.test(usernamePrefix)) {
  throw new Error('TEST_USERNAME_PREFIX must contain 1-8 letters, numbers, underscores, or hyphens.');
}

const applyScenario = (executor, rate, testDuration, preAllocatedVUs, maxVUs) => ({
  executor,
  rate,
  timeUnit: '1s',
  duration: testDuration,
  preAllocatedVUs,
  maxVUs,
  // Let in-flight requests finish without adding scheduled iterations.
  gracefulStop: '10s',
});

export const options = mode === 'soak'
  ? {
      insecureSkipTLSVerify: allowSelfSignedTLS,
      scenarios: {
        steady_category_browse: applyScenario('constant-arrival-rate', readRate, readDuration, 40, 120),
        synchronized_category_refresh_burst: {
          ...applyScenario('constant-arrival-rate', burstRate, burstDuration, 120, 300),
          startTime: readDuration,
        },
      },
      thresholds: {
        checks: ['rate>0.99'],
        http_req_failed: ['rate<0.01'],
        dropped_iterations: ['count==0'],
        iterations: ['count>=102000'],
        'http_req_duration{endpoint:category}': ['p(95)<500', 'p(99)<1000'],
      },
    }
  : mode === 'synchronized-refresh'
    ? {
        insecureSkipTLSVerify: allowSelfSignedTLS,
        scenarios: {
          one_refresh_per_simultaneous_client: {
            executor: 'per-vu-iterations',
            vus: refreshVus,
            iterations: 1,
            maxDuration: '2m',
            gracefulStop: '0s',
          },
        },
        thresholds: {
          checks: ['rate>0.99'],
          http_req_failed: ['rate<0.01'],
          dropped_iterations: ['count==0'],
          iterations: [`count>=${refreshVus}`],
          'http_req_duration{endpoint:category}': ['p(95)<1000', 'p(99)<3000'],
        },
      }
  : mode === 'canary-apply'
    ? {
        insecureSkipTLSVerify: allowSelfSignedTLS,
        scenarios: {
          unique_csrf_protected_apply: applyScenario(
            'constant-arrival-rate',
            applyRate,
            applyDuration,
            20,
            100,
          ),
        },
        thresholds: {
          checks: ['rate>0.99'],
          http_req_failed: ['rate<0.01'],
          dropped_iterations: ['count==0'],
          iterations: ['count>=3900'],
          raffle_canary_apply_attempts: ['count>=3900'],
          raffle_canary_apply_successes: ['count>=3900'],
          'http_req_duration{endpoint:apply}': ['p(95)<500', 'p(99)<1500'],
        },
      }
    : mode === 'apply'
      ? {
          insecureSkipTLSVerify: allowSelfSignedTLS,
          scenarios: {
            apply_once: {
              executor: 'per-vu-iterations',
              vus: Number(__ENV.APPLY_VUS || 10),
              iterations: 1,
              maxDuration: __ENV.MAX_DURATION || '2m',
              gracefulStop: '0s',
            },
          },
          thresholds: {
            checks: ['rate>0.99'],
            http_req_failed: ['rate<0.01'],
            dropped_iterations: ['count==0'],
            iterations: [`count>=${Number(__ENV.APPLY_VUS || 10)}`],
          },
        }
      : {
          insecureSkipTLSVerify: allowSelfSignedTLS,
          scenarios: {
            readiness: applyScenario(
              'constant-arrival-rate',
              Number(__ENV.RATE || 5),
              __ENV.DURATION || '30s',
              Number(__ENV.PRE_ALLOCATED_VUS || 10),
              Number(__ENV.MAX_VUS || 50),
            ),
          },
          thresholds: {
            checks: ['rate>0.99'],
            http_req_failed: ['rate<0.01'],
            dropped_iterations: ['count==0'],
            iterations: ['count>=150'],
            'http_req_duration{endpoint:readiness}': ['p(95)<500', 'p(99)<1000'],
          },
        };

// Check actual requests as well as iterations: a boundary no-op must not hide missing traffic.
options.thresholds.http_reqs = [`count==${plannedRequests}`];

function requestOptions(endpoint, extra = {}) {
  return {
    redirects: 0,
    tags: { endpoint },
    ...extra,
  };
}

function checkStatus(response, name, expectedStatus) {
  if (response.status !== expectedStatus) {
    responseFailures.add(1, { status: String(response.status), error_code: String(response.error_code || 0) });
    // Status only: never record response bodies, cookies, or credentials.
    console.warn(`unexpected_http_status=${response.status} error_code=${response.error_code || 0} operation=${name}`);
  }
  return check(response, {
    [`${name} returned ${expectedStatus}`]: (value) => value.status === expectedStatus,
  });
}

function jsonParams(endpoint, csrfToken) {
  const headers = { 'Content-Type': 'application/json' };
  if (csrfToken) headers['X-CSRFToken'] = csrfToken;
  return requestOptions(endpoint, { headers });
}

function applyOnce() {
  canaryApplyAttempts.add(1);
  const username = `${usernamePrefix}_${runID}_${__VU}_${__ITER}`;
  if (username.length > 50) throw new Error('Generated test username exceeds the database contract.');
  const password = __ENV.TEST_PASSWORD;
  const signupPage = http.get(`${baseURL}/signup`, requestOptions('csrf-bootstrap'));
  const csrfMatch = signupPage.body && signupPage.body.match(/<meta name="csrf-token" content="([^"]+)"\s*\/?\s*>/);
  if (!check(csrfMatch, { 'signup page provides CSRF token': Boolean })) return;
  const csrfToken = csrfMatch[1];

  const signup = http.post(
    `${baseURL}/api/signup`,
    JSON.stringify({ username, password }),
    jsonParams('signup', csrfToken),
  );
  if (!checkStatus(signup, 'signup', 200)) return;

  const login = http.post(
    `${baseURL}/api/login`,
    JSON.stringify({ username, password }),
    jsonParams('login', csrfToken),
  );
  if (!checkStatus(login, 'login', 200)) return;
  const authenticatedCsrfToken = login.json('csrf_token');
  if (!check(authenticatedCsrfToken, { 'login returns a fresh authenticated CSRF token': Boolean })) return;
  check(login, {
    'login session cookie is marked Secure': (response) =>
      (response.headers['Set-Cookie'] || '').split(',').some((cookie) => /(?:^|;\s*)Secure(?:;|$)/i.test(cookie)),
  });

  const apply = http.post(
    `${baseURL}/api/apply`,
    JSON.stringify({ item_id: Number(__ENV.ITEM_ID || 1) }),
    jsonParams('apply', authenticatedCsrfToken),
  );
  if (checkStatus(apply, 'apply', 200)) canaryApplySuccesses.add(1);
}

export default function () {
  const iterationLimit = mode === 'soak'
    ? (exec.scenario.name === 'steady_category_browse'
      ? readRate * durationSeconds(readDuration)
      : burstRate * durationSeconds(burstDuration))
    : ['apply', 'canary-apply'].includes(mode) ? plannedRequests / 4 : plannedRequests;
  if (exec.scenario.iterationInTest >= iterationLimit) return;

  if (mode === 'health') {
    const response = http.get(`${baseURL}/healthz`, requestOptions('health'));
    checkStatus(response, 'health', 200);
    return;
  }

  if (mode === 'canary-apply' || mode === 'apply') {
    applyOnce();
    return;
  }

  if (mode === 'soak' || mode === 'synchronized-refresh') {
    const response = http.get(`${baseURL}/`, requestOptions('category'));
    checkStatus(response, 'category browse', 200);
    return;
  }

  const response = http.get(`${baseURL}/readyz`, requestOptions('readiness'));
  checkStatus(response, 'readiness', 200);
}

export function handleSummary(data) {
  return {
    'stdout': JSON.stringify({
      mode,
      planned_http_requests: plannedRequests,
      metrics: data.metrics,
    }, null, 2),
    [__ENV.SUMMARY_FILE || 'summary.json']: JSON.stringify({
      mode,
      run_id: runID,
      planned_http_requests: plannedRequests,
      started_at: data.state && data.state.testRunDurationMs ? new Date(Date.now() - data.state.testRunDurationMs).toISOString() : null,
      metrics: data.metrics,
    }, null, 2),
  };
}
