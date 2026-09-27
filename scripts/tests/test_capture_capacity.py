"""Offline tests: these tests never contact AWS, Kubernetes, or Prometheus."""

import importlib.util
import json
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('capture_capacity', ROOT / 'platform/live-lab/scripts/capture_capacity.py')
capacity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capacity)


def args():
    return SimpleNamespace(profile='develope-test', account='123456789012', region='ap-northeast-2', session='session-123',
        approval='approved-123', cluster='kyobo-session-123',
        context='arn:aws:eks:ap-northeast-2:123456789012:cluster/kyobo-session-123',
        start=120, end=240, prometheus_port=19091)


def preflight_documents():
    a = args()
    return [
        {'Account': a.account},
        {'cluster': {'arn': a.context, 'status': 'ACTIVE', 'endpoint': 'https://session.eks.test',
                     'certificateAuthority': {'data': 'EXACT-CA'},
                     'tags': {'Project': 'kyobo-platform-live-lab', 'Session': a.session, 'Approval': a.approval}}},
        {'contexts': [{'name': a.context, 'context': {'cluster': 'expected'}}],
         'clusters': [{'name': 'expected', 'cluster': {'server': 'https://session.eks.test',
                                                    'certificate-authority-data': 'EXACT-CA'}}]},
        {'metadata': {'name': capacity.PROMETHEUS_SERVICE, 'namespace': 'monitoring', 'uid': 'service-uid'},
         'spec': {'selector': {'prometheus': 'live-lab-observability-prometheus'}, 'ports': [{'port': 9090}]}}
    ]


@pytest.mark.parametrize('index,mutate,expected_calls', [
    (0, lambda x: x.update(Account='999999999999'), 1),
    (1, lambda x: x['cluster']['tags'].update(Session='other-session'), 2),
    (1, lambda x: x['cluster'].update(status='CREATING'), 2),
    (2, lambda x: x['clusters'][0]['cluster'].update({'certificate-authority-data': 'OTHER'}), 3),
    (2, lambda x: x['clusters'][0]['cluster'].update({'insecure-skip-tls-verify': True}), 3),
    (2, lambda x: x['clusters'][0]['cluster'].update({'proxy-url': 'http://foreign'}), 3),
])
def test_identity_failure_blocks_queries(monkeypatch, index, mutate, expected_calls):
    documents = preflight_documents()
    mutate(documents[index])
    calls = []
    def run(self, command):
        calls.append(command)
        return json.dumps(documents[len(calls) - 1])
    monkeypatch.setattr(capacity.Reader, 'run', run)
    report = capacity.capture(args())
    assert report['status'] == 'unknown'
    assert len(calls) == expected_calls
    assert 'kubernetes' not in report and 'prometheus' not in report


def test_preflight_uses_explicit_context_and_region_without_mutations(monkeypatch):
    documents = iter(preflight_documents())
    commands = []
    reader = capacity.Reader(args())
    def run(command):
        commands.append(command)
        return json.dumps(next(documents))
    monkeypatch.setattr(reader, 'run', run)
    assert reader.preflight()['status'] == 'verified'
    assert all('--context' in c and args().context in c for c in commands if c[0] == 'kubectl')
    assert all('--region' in c and args().region in c for c in commands if c[0] == 'aws')
    assert all(c[c.index('--profile') + 1] == 'develope-test' for c in commands if c[0] == 'aws')
    assert not any(word in {'apply', 'create', 'delete', 'patch', 'update-kubeconfig'} for c in commands for word in c)


def payload(values=None):
    return {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
        {'metric': {}, 'values': values if values is not None else [[120, '1'], [180, '2'], [240, '3']]}]}}


def test_finite_complete_range_is_observed():
    result = capacity.normalize(payload(), 120, 240, 'http_p95_seconds')
    assert result['status'] == 'observed'
    assert result['series'][0]['values'] == [[120, 1.0], [180, 2.0], [240, 3.0]]


@pytest.mark.parametrize('value', ['NaN', '+Inf', '-Inf', '-1'])
def test_invalid_or_unknown_sentinel_is_not_success_or_nonfinite_json(value):
    result = capacity.normalize(payload([[120, value], [180, '1'], [240, '1']]), 120, 240, 'outbox_parity_min')
    assert result['status'] == 'unknown'
    assert result['series'][0]['values'][0] == [120, None]
    json.dumps(result, allow_nan=False)


def test_missing_samples_and_api_warnings_remain_unknown():
    result = capacity.normalize(payload([[180, '1']]), 120, 240, 'metric')
    assert result['status'] == 'unknown'
    assert result['series'][0]['missing_timestamps'] == [120, 240]
    response = payload()
    response['warnings'] = ['partial response']
    assert capacity.normalize(response, 120, 240, 'metric')['status'] == 'unknown'


@pytest.mark.parametrize('response', [
    {'status': 'error', 'error': 'timeout'},
    {'status': 'success', 'data': {'resultType': 'matrix', 'result': []}},
    payload([[119, '0']]),
    payload([[120, '0'], [120, '1']]),
])
def test_invalid_prometheus_response_rejected(response):
    with pytest.raises(ValueError):
        capacity.normalize(response, 120, 240, 'metric')


def test_fixed_queries_have_histogram_aggregation_and_exact_scope():
    queries = capacity.queries()
    assert len(queries) == 20
    assert all('namespace="platform-validation"' in q for q in queries.values())
    assert 'sum by (le)' in queries['http_p99_seconds']
    assert 'sum by (le,role)' in queries['db_connect_p95_seconds']
    assert 'or vector(0)' not in ' '.join(queries.values())
    assert 'min(' in queries['outbox_parity_min']


def test_command_budget_cannot_grow_unbounded():
    reader = capacity.Reader(args())
    for _ in range(capacity.MAX_COMMANDS):
        reader.reserve()
    with pytest.raises(ValueError, match='budget'):
        reader.reserve()


def test_output_is_private_exclusive_and_finite(tmp_path):
    output = tmp_path / 'capacity'
    target = capacity.write_report(output, {'status': 'unknown', 'value': None})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        capacity.write_report(output, {'status': 'observed'})
    assert json.loads(target.read_text())['status'] == 'unknown'


def test_output_symlink_is_rejected(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        capacity.write_report(link, {})


@pytest.mark.parametrize('start,end', [(0, 120), (200, 100), (100, 10901), (100, 99999999999)])
def test_interval_bounds(start, end, tmp_path):
    a = args()
    cli = ['--profile', a.profile, '--account', a.account, '--region', a.region, '--session', a.session,
           '--approval', a.approval, '--context', a.context, '--output', str(tmp_path),
           '--start', str(start), '--end', str(end)]
    with pytest.raises(ValueError):
        capacity.arguments(cli)


def test_occupied_loopback_port_never_reuses_an_existing_tunnel(monkeypatch):
    class BusySocket:
        def __enter__(self): return self
        def __exit__(self, *unused): pass
        def bind(self, address): raise OSError('occupied')
    monkeypatch.setattr(capacity.socket, 'socket', BusySocket)
    monkeypatch.setattr(capacity.subprocess, 'Popen', lambda *a, **kw: pytest.fail('must not spawn'))
    with pytest.raises(OSError, match='occupied'):
        with capacity.tunnel(capacity.Reader(args())):
            pytest.fail('must not enter')


def test_prometheus_redirect_refused():
    with pytest.raises(ValueError, match='redirect'):
        capacity.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://foreign.example/')


def test_owned_tunnel_is_terminated_even_when_collection_fails(monkeypatch):
    class Probe:
        def __enter__(self): return self
        def __exit__(self, *unused): pass
        def bind(self, address): assert address == ('127.0.0.1', 19091)
    class Stream:
        closed = False
        def fileno(self): return 91
        def close(self): self.closed = True
    class Process:
        stdout = Stream()
        stopped = False
        waited = False
        def poll(self): return 0 if self.stopped else None
        def terminate(self): self.stopped = True
        def wait(self, timeout): self.waited = True
    class Selector:
        def __enter__(self): return self
        def __exit__(self, *unused): pass
        def register(self, *unused): pass
        def select(self, timeout): return [True]
    process = Process()
    commands = []
    monkeypatch.setattr(capacity.socket, 'socket', Probe)
    monkeypatch.setattr(capacity.selectors, 'DefaultSelector', Selector)
    monkeypatch.setattr(capacity.os, 'read', lambda *unused: b'Forwarding from 127.0.0.1:19091 -> 9090\n')
    def spawn(command, **kwargs):
        commands.append(command)
        assert kwargs['env']['AWS_PROFILE'] == 'develope-test'
        return process
    monkeypatch.setattr(capacity.subprocess, 'Popen', spawn)
    with pytest.raises(ValueError, match='collection failed'):
        with capacity.tunnel(capacity.Reader(args())):
            raise ValueError('collection failed')
    assert process.stopped and process.waited and process.stdout.closed
    assert '--address=127.0.0.1' in commands[0]
    assert 'service/live-lab-observability-prometheus' in commands[0]


def test_missing_top_and_hpa_are_explicit_unknown(monkeypatch):
    reader = capacity.Reader(args())
    monkeypatch.setattr(reader, 'run', lambda command: '{}')
    result = reader.snapshots()
    assert result['pods']['status'] == 'unknown'
    assert result['hpa']['status'] == 'unknown'
    assert result['top']['status'] == 'unknown'


def test_pods_and_top_select_actual_rendered_base_rollout(monkeypatch):
    rendered = subprocess.run(['kubectl', 'kustomize', str(ROOT / 'k8s/base')],
                              capture_output=True, text=True, check=True, timeout=30)
    rollout = next(item for item in yaml.safe_load_all(rendered.stdout)
                   if item and item.get('kind') == 'Rollout')
    match_labels = rollout['spec']['selector']['matchLabels']
    template_labels = rollout['spec']['template']['metadata']['labels']
    commands = []
    reader = capacity.Reader(args())
    def run(command):
        commands.append(command)
        if 'top' in command:
            return 'data-pipeline-rollout-abc app-container 10m 32Mi\n'
        if 'hpa' in command:
            return json.dumps({'status': {'currentReplicas': 2}})
        return json.dumps({'items': [{'metadata': {'name': 'data-pipeline-rollout-abc', 'uid': 'pod-uid'},
                                     'status': {'phase': 'Running'}}]})
    monkeypatch.setattr(reader, 'run', run)
    snapshots = reader.snapshots()
    selected = [c[c.index('-l') + 1] for c in commands if '-l' in c]
    assert len(selected) == 2
    for selector in selected:
        labels = dict(part.split('=', 1) for part in selector.split(','))
        assert labels == match_labels
        assert all(template_labels.get(k) == v for k, v in labels.items())
    assert snapshots['pods']['status'] == snapshots['top']['status'] == 'observed'


def cli_args(tmp_path):
    a = args()
    return ['--account', a.account, '--region', a.region, '--session', a.session,
            '--approval', a.approval, '--context', a.context, '--output', str(tmp_path),
            '--start', '120', '--end', '240']


def test_profile_is_required_even_when_environment_sets_one(tmp_path, monkeypatch):
    monkeypatch.setenv('AWS_PROFILE', 'other-profile')
    with pytest.raises(SystemExit) as error:
        capacity.arguments(cli_args(tmp_path))
    assert error.value.code == 2


def test_explicit_profile_and_collector_port(tmp_path):
    parsed = capacity.arguments(cli_args(tmp_path) + ['--profile', 'develope-test'])
    assert parsed.profile == 'develope-test'
    assert parsed.prometheus_port == 19091


@pytest.mark.parametrize('plugin', [
    {'env': [{'name': 'AWS_PROFILE', 'value': 'other'}]},
    {'args': ['eks', 'get-token', '--profile', 'other']},
    {'args': ['eks', 'get-token', '--profile=other']},
])
def test_conflicting_kube_plugin_profile_blocks_cluster_queries(monkeypatch, plugin):
    documents = preflight_documents()
    documents[2]['users'] = [{'user': {'exec': plugin}}]
    calls = []
    def run(self, command):
        calls.append(command)
        return json.dumps(documents[len(calls) - 1])
    monkeypatch.setattr(capacity.Reader, 'run', run)
    report = capacity.capture(args())
    assert report['status'] == 'unknown'
    assert report['error'] == 'kube credential plugin profile mismatch'
    assert len(calls) == 3
    assert 'kubernetes' not in report


def test_subprocess_profile_overrides_ambient_profile_and_credentials(monkeypatch):
    monkeypatch.setenv('AWS_PROFILE', 'other')
    monkeypatch.setenv('AWS_DEFAULT_PROFILE', 'other')
    for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN'):
        monkeypatch.setenv(key, 'not-a-real-credential')
    def run(command, **kwargs):
        env = kwargs['env']
        assert env['AWS_PROFILE'] == env['AWS_DEFAULT_PROFILE'] == 'develope-test'
        assert 'AWS_ACCESS_KEY_ID' not in env
        assert 'AWS_SECRET_ACCESS_KEY' not in env
        assert 'AWS_SESSION_TOKEN' not in env
        assert 'AWS_SECURITY_TOKEN' not in env
        return SimpleNamespace(returncode=0, stdout=b'{}')
    monkeypatch.setattr(capacity.subprocess, 'run', run)
    assert capacity.Reader(args()).run(['kubectl', 'config', 'view']) == '{}'
