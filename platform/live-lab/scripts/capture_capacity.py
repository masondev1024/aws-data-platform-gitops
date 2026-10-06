#!/usr/bin/env python3
"""Read-only, retrospective A/B or soak evidence; never starts a load test.

Run after the interval ends. Example (epochs/context supplied by the operator):
  python3 platform/live-lab/scripts/capture_capacity.py --start START --end END \
    --profile develope-test --account ACCOUNT --region REGION --session SESSION --approval APPROVAL \
    --context arn:aws:eks:REGION:ACCOUNT:cluster/kyobo-SESSION \
    --service canary --output DIRECTORY

Owns a temporary loopback-only port-forward to the validated cluster's fixed
Prometheus service. An occupied port is rejected, never reused. Kubernetes CLI
snapshots describe capture time; Prometheus supplies the historical interval.
No CloudWatch, writes to Kubernetes/AWS, retries, or background sampling loop.
"""

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import selectors
import socket
import subprocess
import time
import urllib.parse
import urllib.request

NAMESPACE = 'platform-validation'
POD_SELECTOR = 'app=data-pipeline-app'
PROMETHEUS_SERVICE = 'live-lab-observability-prometheus'
STEP = 60
MAX_RANGE = 3 * 3600
MAX_BYTES = 2 * 1024 * 1024
MAX_SERIES = 32
MAX_COMMANDS = 8  # STS, EKS, config, service, pods, HPA, top, port-forward


def require(condition, message):
    if not condition:
        raise ValueError(message)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('profile', 'account', 'region', 'session', 'approval', 'context', 'output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--start', type=int, required=True)
    parser.add_argument('--end', type=int, required=True)
    parser.add_argument('--service', choices=('stable', 'canary'), default='stable')
    parser.add_argument('--prometheus-port', type=int, default=19091)
    args = parser.parse_args(argv)
    require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', args.profile)), 'invalid profile')
    require(bool(re.fullmatch(r'[0-9]{12}', args.account)), 'invalid account')
    require(bool(re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]', args.region)), 'invalid region')
    require(bool(re.fullmatch(r'[a-z0-9][a-z0-9-]{5,40}', args.session)), 'invalid session')
    require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', args.approval)), 'invalid approval')
    args.cluster = 'kyobo-' + args.session
    require(args.context == f'arn:aws:eks:{args.region}:{args.account}:cluster/{args.cluster}',
            'context must be the exact session EKS ARN')
    require(0 < args.start < args.end <= int(time.time()), 'interval must be completed positive epochs')
    require(args.end - args.start <= MAX_RANGE, 'interval exceeds 3 hours')
    require(1024 <= args.prometheus_port <= 65535, 'invalid loopback port')
    return args


def queries(service='stable'):
    require(service in {'stable', 'canary'}, 'invalid service')
    # Select one service so stable/canary scrapes of a pod are not double counted.
    app = f'namespace="platform-validation",service="data-pipeline-svc-{service}"'
    http = app + ',route!~"/(metrics|healthz|readyz)"'
    def rate(metric, labels=app):
        return f'rate({metric}{{{labels}}}[2m])'
    def quantile(metric, q, labels=app, group=''):
        return f'histogram_quantile({q}, sum by (le{group}) ({rate(metric + "_bucket", labels)}))'
    q = {
        'http_p95_seconds': quantile('raffle_http_request_duration_seconds', 0.95, http),
        'http_p99_seconds': quantile('raffle_http_request_duration_seconds', 0.99, http),
        'http_requests_per_second': f'sum({rate("raffle_http_requests_total", http)})',
        'http_5xx_per_second': f'sum({rate("raffle_http_requests_total", http + ",status=~\"5..\"")})',
        'db_connect_p95_seconds': quantile('raffle_db_connect_duration_seconds', 0.95, group=',role'),
        'db_connect_p99_seconds': quantile('raffle_db_connect_duration_seconds', 0.99, group=',role'),
        'db_connect_count_2m': f'sum by (role) (increase(raffle_db_connect_duration_seconds_count{{{app}}}[2m]))',
        'catalog_load_p95_seconds': quantile('raffle_catalog_load_duration_seconds', 0.95),
        'catalog_load_count_2m': f'sum(increase(raffle_catalog_load_duration_seconds_count{{{app}}}[2m]))',
        'catalog_cache_events_2m': f'sum by (result) (increase(raffle_catalog_cache_events_total{{{app}}}[2m]))',
        'outbox_parity_max': f'max(raffle_apply_outbox_parity_gap{{{app}}})',
        'outbox_parity_min': f'min(raffle_apply_outbox_parity_gap{{{app}}})',
        'scrape_up_min': f'min(up{{{app}}})',
    }
    q['http_5xx_ratio'] = f'({q["http_5xx_per_second"]}) / ({q["http_requests_per_second"]})'
    pod = 'namespace="platform-validation",pod=~"data-pipeline-rollout-.*"'
    q.update({
        'pod_cpu_cores': f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{pod},container!="",container!="POD"}}[2m]))',
        'pod_memory_bytes': f'sum by (pod) (container_memory_working_set_bytes{{{pod},container!="",container!="POD"}})',
        'pod_restarts': f'sum by (pod) (kube_pod_container_status_restarts_total{{{pod}}})',
        'pod_ready': f'min by (pod) (kube_pod_status_ready{{{pod},condition="true"}})',
    })
    for kind in ('current', 'desired'):
        q[f'hpa_{kind}_replicas'] = ('max(kube_horizontalpodautoscaler_status_' + kind +
            '_replicas{namespace="platform-validation",horizontalpodautoscaler="data-pipeline-hpa"})')
    return q


class Reader:
    def __init__(self, args):
        self.args = args
        self.calls = 0
        self.kube = ['kubectl', '--context', args.context, '--request-timeout=20s']

    def environment(self):
        # The kubectl exec credential plugin must use the selected profile too.
        env = {k: v for k, v in os.environ.items() if k not in {
            'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN'}}
        env.update(AWS_PROFILE=self.args.profile, AWS_DEFAULT_PROFILE=self.args.profile,
                   AWS_MAX_ATTEMPTS='1', AWS_PAGER='')
        return env

    def reserve(self):
        self.calls += 1
        require(self.calls <= MAX_COMMANDS, 'command budget exhausted')

    def run(self, command):
        self.reserve()
        result = subprocess.run(command, capture_output=True, timeout=30,
            env=self.environment())
        require(result.returncode == 0, f'{command[0]} read failed (exit {result.returncode})')
        require(len(result.stdout) <= MAX_BYTES, 'CLI response exceeds evidence bound')
        return result.stdout.decode()

    def get(self, *args):
        return json.loads(self.run(self.kube + list(args)))

    def preflight(self):
        a = self.args
        aws = ['aws', '--profile', a.profile, '--region', a.region, '--output', 'json', '--no-cli-pager']
        identity = json.loads(self.run(aws + ['sts', 'get-caller-identity']))
        require(identity.get('Account') == a.account, 'caller account mismatch')
        cluster = json.loads(self.run(aws + ['eks', 'describe-cluster', '--name', a.cluster]))['cluster']
        require(cluster.get('arn') == a.context and cluster.get('status') == 'ACTIVE', 'EKS ARN/status mismatch')
        tags = {'Project': 'kyobo-platform-live-lab', 'Session': a.session, 'Approval': a.approval}
        require(all(cluster.get('tags', {}).get(k) == v for k, v in tags.items()), 'EKS session tags mismatch')
        config = self.get('config', 'view', '--minify', '--flatten', '--raw', '-o', 'json')
        for user in config.get('users', []):
            plugin = user.get('user', {}).get('exec', {})
            for entry in plugin.get('env', []) or []:
                if entry.get('name') in {'AWS_PROFILE', 'AWS_DEFAULT_PROFILE'}:
                    require(entry.get('value') == a.profile, 'kube credential plugin profile mismatch')
            plugin_args = plugin.get('args', []) or []
            for index, value in enumerate(plugin_args):
                if value == '--profile':
                    require(index + 1 < len(plugin_args) and plugin_args[index + 1] == a.profile,
                            'kube credential plugin profile mismatch')
                elif value.startswith('--profile='):
                    require(value.split('=', 1)[1] == a.profile, 'kube credential plugin profile mismatch')
        require(len(config.get('contexts', [])) == 1 and config['contexts'][0]['name'] == a.context,
                'kube context mismatch')
        require(len(config.get('clusters', [])) == 1, 'ambiguous kube cluster')
        require(config['contexts'][0].get('context', {}).get('cluster') == config['clusters'][0].get('name'),
                'context points to another cluster')
        conn = config['clusters'][0]['cluster']
        require(conn.get('server') == cluster.get('endpoint') and str(conn.get('server', '')).startswith('https://')
                and conn.get('certificate-authority-data') == cluster.get('certificateAuthority', {}).get('data')
                and bool(conn.get('certificate-authority-data')) and not conn.get('insecure-skip-tls-verify')
                and not conn.get('proxy-url') and not conn.get('tls-server-name'), 'EKS TLS endpoint mismatch')
        service = self.get('-n', 'monitoring', 'get', 'service', PROMETHEUS_SERVICE, '-o', 'json')
        require(service.get('metadata', {}).get('name') == PROMETHEUS_SERVICE
                and service['metadata'].get('namespace') == 'monitoring'
                and service.get('spec', {}).get('selector')
                and any(p.get('port') == 9090 for p in service['spec'].get('ports', [])), 'unexpected Prometheus service')
        # Never persist raw kubeconfig, environment, pod specs, or credential output.
        return {'status': 'verified', 'profile': a.profile, 'account': a.account, 'cluster_arn': a.context,
                'tags': tags, 'prometheus_service_uid': service['metadata'].get('uid')}

    def snapshots(self):
        output = {}
        for name, command in {
            'pods': ['get', 'pods', '-l', POD_SELECTOR, '-o', 'json'],
            'hpa': ['get', 'hpa', 'data-pipeline-hpa', '-o', 'json'],
            'top': ['top', 'pods', '-l', POD_SELECTOR, '--containers', '--no-headers'],
        }.items():
            try:
                raw = self.run(self.kube + ['-n', NAMESPACE] + command)
                if name == 'pods':
                    items = json.loads(raw)['items']
                    require(0 < len(items) <= 100, 'pods missing or excessive')
                    data = [{'name': p['metadata']['name'], 'uid': p['metadata']['uid'],
                             'status': p.get('status', {})} for p in items]
                elif name == 'hpa':
                    data = json.loads(raw).get('status')
                    require(bool(data), 'HPA status absent')
                else:
                    require(bool(raw.strip()) and len(raw.splitlines()) <= 100, 'top missing or excessive')
                    data = []
                    for line in raw.splitlines():
                        columns = line.split()
                        require(len(columns) == 4 and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?[num]?', columns[2])
                                and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:[KMGTPE]i?)?', columns[3]),
                                'top sample is not numeric CPU/memory')
                        data.append(dict(zip(('pod', 'container', 'cpu', 'memory'), columns)))
                output[name] = {'status': 'observed', 'captured_at': int(time.time()), 'data': data}
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                output[name] = {'status': 'unknown', 'reason': str(error)}
        return output


@contextmanager
def tunnel(reader):
    port = reader.args.prometheus_port
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))  # Fail closed if somebody else owns the port.
    reader.reserve()
    process = subprocess.Popen(reader.kube + ['-n', 'monitoring', 'port-forward',
        '--address=127.0.0.1', 'service/' + PROMETHEUS_SERVICE, f'{port}:9090'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=reader.environment())
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline, received = time.monotonic() + 20, b''
            while time.monotonic() < deadline:
                require(process.poll() is None, 'owned port-forward exited')
                if selector.select(timeout=0.2):
                    received += os.read(process.stdout.fileno(), 4096)
                    require(len(received) <= 16384, 'port-forward output exceeded bound')
                    if f'Forwarding from 127.0.0.1:{port} ->'.encode() in received:
                        break
            else:
                raise TimeoutError('owned port-forward readiness timeout')
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()


def normalize(payload, start, end, name):
    require(payload.get('status') == 'success', 'Prometheus API error')
    data = payload.get('data', {})
    require(data.get('resultType') == 'matrix', 'expected Prometheus matrix')
    series = data.get('result', [])
    require(0 < len(series) <= MAX_SERIES, 'missing or excessive time series')
    expected = set(range(start, end + 1, STEP))
    result, unknown = [], bool(payload.get('warnings') or payload.get('infos'))
    for item in series:
        points, seen = [], set()
        require(len(item.get('values', [])) <= len(expected), 'too many samples')
        for timestamp, raw in item.get('values', []):
            require(timestamp in expected and timestamp not in seen, 'invalid sample timestamp')
            seen.add(timestamp)
            value = float(raw)
            valid = math.isfinite(value) and value >= 0
            unknown |= not valid
            points.append([timestamp, value if valid else None])
        unknown |= seen != expected
        result.append({'labels': item.get('metric', {}), 'values': points,
                       'missing_timestamps': sorted(expected - seen)})
    return {'status': 'unknown' if unknown else 'observed', 'series': result,
            'reason': 'missing/nonfinite/negative samples or API annotations' if unknown else None,
            'warnings': payload.get('warnings', []), 'infos': payload.get('infos', [])}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Prometheus redirect refused')


def collect_prometheus(args, process):
    output = {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for name, query in queries(args.service).items():
        try:
            require(process.poll() is None, 'owned port-forward exited')
            params = urllib.parse.urlencode({'query': query, 'start': args.start, 'end': args.end,
                'step': STEP, 'timeout': '10s', 'limit': MAX_SERIES + 1})
            with opener.open(f'http://127.0.0.1:{args.prometheus_port}/api/v1/query_range?{params}', timeout=15) as response:
                raw = response.read(MAX_BYTES + 1)
            require(len(raw) <= MAX_BYTES, 'Prometheus response too large')
            output[name] = normalize(json.loads(raw), args.start, args.end, name)
        except (OSError, ValueError, KeyError, TypeError) as error:
            output[name] = {'status': 'unknown', 'reason': str(error)}
        output[name]['query'] = query
    return output


def write_report(directory, report):
    path = Path(directory)
    require(not path.is_symlink(), 'output directory symlink refused')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o077 == 0, 'output directory must be private and owned')
    target = path / 'capacity.json'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    return target


def capture(args):
    reader = Reader(args)
    report = {'status': 'unknown', 'scope': {k: getattr(args, k) for k in
        ('profile', 'account', 'region', 'session', 'approval', 'context', 'start', 'end', 'service')},
        'step_seconds': STEP, 'max_command_invocations': MAX_COMMANDS,
        'max_prometheus_requests': len(queries(args.service)),
        'limitations': ['Completeness is not a health/SLO verdict.',
            'Kubernetes CLI snapshots are capture-time only; historical samples come from Prometheus.',
            'Selected-service application metrics; 2-minute rolling rates/counts include pre-start warmup.',
            'DB histogram includes connection/TLS attempts, including failures; it does not prove TLS negotiation.',
            'No traffic may yield unknown quantiles/ratios; no zero filling.',
            'CLI/API invocation budget excludes kubectl discovery/authentication internals.']}
    try:
        report['identity'] = reader.preflight()
        report['kubernetes'] = reader.snapshots()
        with tunnel(reader) as process:
            report['prometheus'] = collect_prometheus(args, process)
        sections = list(report['kubernetes'].values()) + list(report['prometheus'].values())
        if all(section['status'] == 'observed' for section in sections):
            report['status'] = 'observed'
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        report['error'] = str(error)
    report['command_invocations'] = reader.calls
    report['captured_at'] = int(time.time())
    return report


def main(argv=None):
    args = arguments(argv)
    report = capture(args)
    target = write_report(args.output, report)
    print(json.dumps({'status': report['status'], 'evidence': str(target)}))
    return 0 if report['status'] == 'observed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
