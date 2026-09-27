"""Exercise the real k6 JS entrypoint with offline HTTP/execution stubs."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node required for offline k6 entrypoint simulation")
@pytest.mark.parametrize("filename,mode,scenario,limit,requests", [
    ("catalog-capacity.js", "catalog", "catalogue", 6000, 1),
    ("raffle.js", "soak", "steady_category_browse", 72000, 1),
    ("raffle.js", "soak", "synchronized_category_refresh_burst", 30000, 1),
    ("raffle.js", "canary-apply", "unique_csrf_protected_apply", 3900, 4),
    ("raffle.js", "readiness", "readiness", 150, 1),
])
def test_final_scheduled_iteration_does_not_send_an_extra_request(filename, mode, scenario, limit, requests):
    script = r"""
const fs = require('fs'), vm = require('vm');
const [file, mode, name, rawLimit] = process.argv.slice(1);
let source = fs.readFileSync(file, 'utf8').replace(/^import .*;$/gm, '')
  .replace('export default function ()', 'function iteration()')
  .replaceAll('export const ', 'const ').replaceAll('export function ', 'function ');
const limit = Number(rawLimit), results=[];
for (const index of [limit-1, limit, limit+1]) {
  const calls=[];
  const response={status:200, body:'WEEKLY <meta name="csrf-token" content="token">',
    headers:{'Set-Cookie':'session=test; Secure; HttpOnly'}, json:()=> 'token'};
  const context={__ENV:{MODE:mode, BASE_URL:'https://fixture.ap-northeast-2.elb.amazonaws.com',
    APPLY_DURATION:'13m', TEST_PASSWORD:'offline-synthetic-only', SUMMARY_FILE:'unused'},
    __VU:1, __ITER:0, exec:{scenario:{name, iterationInTest:index}},
    http:{get:(...a)=>{calls.push(a); return response;}, post:(...a)=>{calls.push(a); return response;}},
    check:()=>true, Counter:class {add(){}}, console};
  vm.runInNewContext(source+';iteration();',context,{timeout:1000});
  results.push(calls.length);
}
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run([NODE, "-e", script, str(ROOT / "loadtest" / filename), mode, scenario, str(limit)],
                            check=True, capture_output=True, text=True, timeout=10)
    assert json.loads(result.stdout) == [requests, 0, 0]
