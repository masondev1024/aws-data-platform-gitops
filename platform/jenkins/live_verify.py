#!/usr/bin/env python3
"""Opt-in local Jenkins validation. Never pushes or commits repository state."""

import argparse
import base64
import hashlib
import http.cookiejar
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'platform/live-lab/evidence/jenkins-followup'
URL = 'http://127.0.0.1:8080/'
COMPOSE = ['docker', 'compose', '-p', 'develope-jenkins', '-f', str(ROOT / 'platform/jenkins/docker-compose.yml')]


def command(*args, **kwargs):
    return subprocess.check_output(args, cwd=ROOT, **kwargs).decode().strip()


def save(name, value):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(value, indent=2) + '\n')


def snapshot():
    """Capture actual working bytes (including untracked code), without .git/secrets."""
    command('git', 'check-ignore', str(EVIDENCE / 'probe'))
    paths = command('git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')
    roots = {'.github', '.config', 'app', 'agentic_ops', 'scripts', 'k8s', 'terraform', 'platform', 'loadtest'}
    files = {}
    for name in sorted(set(paths)):
        path = ROOT / name
        if not name or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError(f'Refusing source symlink: {name}')
        if '/' in name and name.split('/')[0] not in roots:
            continue
        if any(part in {'evidence', 'secrets', '.terraform', '__pycache__'} for part in path.relative_to(ROOT).parts):
            continue
        if path.name.startswith('.env') or path.suffix in {'.pem', '.key', '.tfstate', '.tfvars'}:
            raise ValueError(f'Refusing possible secret source: {name}')
        files[name] = (path.read_bytes(), path.stat().st_mode & 0o777)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, (data, _) in files.items()}
    manifest = ''.join(f'{digest}  {name}\n' for name, digest in hashes.items()).encode()
    files['.jenkins-source-files.sha256'] = (manifest, 0o644)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            archive.addfile(info, io.BytesIO(data))
    for name, digest in hashes.items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Source changed while snapshotting: {name}; retry after edit completes')
    content = buffer.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    archive_path = EVIDENCE / f'jenkins-source-{digest}.tar.gz'
    archive_path.write_bytes(content)
    save('source.json', {'sha256': digest, 'head_reference_only': command('git', 'rev-parse', 'HEAD'),
                         'files': hashes, 'git_status': command('git', 'status', '--short'),
                         'captured_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    agent = command(*COMPOSE, 'ps', '-q', 'platform-agent')
    subprocess.run(['docker', 'cp', str(archive_path), f'{agent}:/tmp/{archive_path.name}'], check=True)
    return digest, files['Jenkinsfile'][0].decode()


class Jenkins:
    def __init__(self):
        secret_dir = Path(os.environ['JENKINS_SECRETS_DIR'])
        user = (secret_dir / 'jenkins_admin_user').read_text().strip()
        password = (secret_dir / 'jenkins_admin_password').read_text().strip()
        self.authorization = 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.crumb = {}

    def request(self, path, data=None, content_type='application/x-www-form-urlencoded'):
        headers = {'Authorization': self.authorization, 'Content-Type': content_type, **self.crumb}
        request = urllib.request.Request(URL + path, data=data, headers=headers)
        with self.opener.open(request, timeout=30) as response:
            return response.read()

    def ready(self):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                crumb = json.loads(self.request('crumbIssuer/api/json'))
                self.crumb = {crumb['crumbRequestField']: crumb['crumb']}
                return
            except urllib.error.HTTPError as error:
                if error.code not in {502, 503}:
                    raise
                time.sleep(3)
            except OSError:
                time.sleep(3)
        raise TimeoutError('Jenkins did not become ready within 180 seconds')

    def connect_agent(self):
        # Response contains a secret: pass straight to the existing protected setter.
        xml = ET.fromstring(self.request('computer/platform-agent/jenkins-agent.jnlp'))
        secret = xml.find('.//argument').text
        subprocess.run(['bash', str(ROOT / 'platform/jenkins/set-agent-secret.sh')],
                       input=(secret + '\n').encode(), stdout=subprocess.DEVNULL, check=True)
        subprocess.run([*COMPOSE, 'up', '-d', 'platform-agent'], check=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            state = json.loads(self.request('computer/platform-agent/api/json'))
            if not state['offline']:
                return
            time.sleep(3)
        raise TimeoutError('platform-agent remained offline')

    def configure(self, name, script, digest, fail):
        root = ET.Element('flow-definition', plugin='workflow-job')
        ET.SubElement(root, 'description').text = 'Local working-tree verification; managed by platform/jenkins/live_verify.py'
        properties = ET.SubElement(root, 'properties')
        definitions = ET.SubElement(ET.SubElement(properties, 'hudson.model.ParametersDefinitionProperty'), 'parameterDefinitions')
        for kind, key, value in [('String', 'LOCAL_SOURCE_SHA256', digest), ('Boolean', 'FAILURE_DRILL', str(fail).lower())]:
            definition = ET.SubElement(definitions, f'hudson.model.{kind}ParameterDefinition')
            ET.SubElement(definition, 'name').text = key
            ET.SubElement(definition, 'defaultValue').text = value
        definition = ET.SubElement(root, 'definition', {'class': 'org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition', 'plugin': 'workflow-cps'})
        ET.SubElement(definition, 'script').text = script
        ET.SubElement(definition, 'sandbox').text = 'true'
        ET.SubElement(root, 'disabled').text = 'false'
        content = ET.tostring(root)
        try:
            self.request(f'job/{name}/config.xml', content, 'application/xml')
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            self.request('createItem?name=' + name, content, 'application/xml')
        (EVIDENCE / f'{name}-config.xml').write_bytes(content)

    def run(self, name, digest, fail):
        before = json.loads(self.request(f'job/{name}/api/json'))['nextBuildNumber']
        payload = urllib.parse.urlencode({'LOCAL_SOURCE_SHA256': digest, 'FAILURE_DRILL': str(fail).lower()}).encode()
        self.request(f'job/{name}/buildWithParameters', payload)
        path = f'job/{name}/{before}/'
        deadline = time.monotonic() + 1900
        next_sample = 0
        while time.monotonic() < deadline:
            if time.monotonic() > next_sample:
                sample_resources(f'{name}-{before}')
                next_sample = time.monotonic() + 30
            try:
                build = json.loads(self.request(path + 'api/json'))
                if not build['building']:
                    break
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
            time.sleep(5)
        else:
            raise TimeoutError(f'Build did not finish: {URL}{path}')
        log = self.request(path + 'consoleText')
        (EVIDENCE / f'{name}-{before}.log').write_bytes(log)
        for artifact in build.get('artifacts', []):
            relative = artifact['relativePath']
            target = EVIDENCE / f'{name}-{before}-artifacts' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.request(path + 'artifact/' + urllib.parse.quote(relative)))
        save(f'{name}-{before}.json', build)
        result = {'url': URL + path, 'result': build['result'], 'number': before, 'sha256': digest}
        if fail:
            result['expected_failure_verified'] = (build['result'] == 'FAILURE' and
                b'JENKINS_INTENTIONAL_FAILURE' in log and b'script returned exit code 1' in log and
                b'1 failed' in log)
        print(json.dumps(result), flush=True)
        return result


def sample_resources(label):
    ids = command(*COMPOSE, 'ps', '-q').splitlines()
    sample = {'label': label, 'time': time.time(),
              'host_pressure': command('memory_pressure', '-Q'),
              'swap': command('sysctl', 'vm.swapusage'),
              'containers': command('docker', 'stats', '--no-stream', '--format', '{{json .}}', *ids)}
    with (EVIDENCE / 'resources.jsonl').open('a') as stream:
        stream.write(json.dumps(sample) + '\n')


def verify_boundaries(jenkins):
    """Assert the live security/resource contract, recording no environment secrets."""
    nodes = json.loads(jenkins.request('computer/api/json?tree=computer[displayName,numExecutors,offline]'))['computer']
    controller = next(node for node in nodes if node['displayName'] != 'platform-agent')
    agent = next(node for node in nodes if node['displayName'] == 'platform-agent')
    if controller['numExecutors'] != 0 or agent['numExecutors'] != 1 or agent['offline']:
        raise RuntimeError('Live executor isolation contract failed')
    try:
        urllib.request.urlopen(URL + 'api/json', timeout=10)
    except urllib.error.HTTPError as error:
        anonymous_status = error.code
    else:
        raise RuntimeError('Anonymous Jenkins API access must be denied')
    if anonymous_status != 403:
        raise RuntimeError(f'Unexpected anonymous API status: {anonymous_status}')
    records = []
    for service in ['controller', 'platform-agent']:
        container = command(*COMPOSE, 'ps', '-q', service)
        info = json.loads(command('docker', 'inspect', container))[0]
        uid = command('docker', 'exec', container, 'id', '-u')
        config = info['HostConfig']
        mounts = [{'type': m['Type'], 'source': m['Source'], 'destination': m['Destination'], 'rw': m['RW']} for m in info['Mounts']]
        allowed = ({'/var/jenkins_home', '/run/secrets/jenkins_admin_user', '/run/secrets/jenkins_admin_password'}
                   if service == 'controller' else {'/run/secrets/jenkins_agent_secret', '/home/jenkins/.jenkins', '/home/jenkins/agent'})
        if uid == '0' or config['Privileged'] or any(m['destination'] not in allowed for m in mounts):
            raise RuntimeError(f'Unsafe runtime configuration: {service}')
        for mount in mounts:
            if mount['destination'].startswith('/run/secrets/'):
                if mount['rw']:
                    raise RuntimeError('Secret mounts must be read-only')
            elif mount['type'] != 'volume':
                raise RuntimeError('Jenkins data directories must use Docker volumes')
        forbidden_env = {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'KUBECONFIG'}
        if any(entry.split('=', 1)[0] in forbidden_env for entry in info['Config']['Env']):
            raise RuntimeError('Unexpected cloud credentials in container configuration')
        if 'no-new-privileges:true' not in config['SecurityOpt'] or 'ALL' not in config['CapDrop']:
            raise RuntimeError(f'Missing runtime hardening: {service}')
        ports = config['PortBindings'] or {}
        if service == 'controller' and ports != {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8080'}]}:
            raise RuntimeError('Controller must publish only loopback port 8080')
        if service == 'platform-agent' and ports:
            raise RuntimeError('Agent must not publish any ports')
        records.append({'service': service, 'id': container, 'image': info['Image'], 'uid': uid,
                        'memory_limit': config['Memory'], 'nano_cpus': config['NanoCpus'],
                        'pids_limit': config['PidsLimit'], 'ports': ports, 'mounts': mounts,
                        'oom_killed': info['State']['OOMKilled'], 'restart_count': info['RestartCount']})
    if any(r['memory_limit'] <= 0 for r in records) or sum(r['memory_limit'] for r in records) > 5 * 1024 ** 3:
        raise RuntimeError('Jenkins combined memory limits must be positive and at most 5 GiB')
    save('runtime.json', {'nodes': nodes, 'anonymous_api_status': anonymous_status, 'containers': records})
    save('plugins.json', json.loads(jenkins.request('pluginManager/api/json?tree=plugins[shortName,version,active]')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['connect-agent', 'verify'])
    args = parser.parse_args()
    os.environ.setdefault('JENKINS_SECRETS_DIR', '/tmp/develope-project-jenkins-secrets')
    jenkins = Jenkins()
    jenkins.ready()
    if args.action == 'connect-agent':
        jenkins.connect_agent()
        print('platform-agent is connected; secret value was not printed')
        return
    verify_boundaries(jenkins)
    digest, script = snapshot()
    print('Captured working-tree archive SHA-256: ' + digest, flush=True)
    results = []
    for name, fail in [('platform-verify-success', False), ('platform-verify-intentional-failure', True)]:
        jenkins.configure(name, script, digest, fail)
        results.append(jenkins.run(name, digest, fail))
    save('results.json', results)
    if results[0]['result'] != 'SUCCESS' or not results[1]['expected_failure_verified']:
        raise SystemExit('Live verification did not meet success/failure acceptance criteria; inspect evidence')


if __name__ == '__main__':
    main()
