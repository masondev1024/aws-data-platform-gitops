from pathlib import Path
import re
import os
import stat
import subprocess
import hashlib
import importlib.util
import json
import tarfile
import pytest


ROOT = Path(__file__).resolve().parents[2]


def load_live_verifier():
    spec = importlib.util.spec_from_file_location('jenkins_live_verify', ROOT / 'platform/jenkins/live_verify.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_snapshot_captures_dirty_untracked_bytes_and_excludes_secrets(tmp_path, monkeypatch):
    live = load_live_verifier()
    files = {
        'Jenkinsfile': 'pipeline { /* actual working copy */ }',
        'app/new.py': 'print("uncommitted source")\n',
        'platform/live-lab/secrets/token': 'private',
        'platform/live-lab/evidence/old.json': 'private evidence',
        '.omc/notes.md': 'private notes',
    }
    for name, value in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    monkeypatch.setattr(live, 'ROOT', tmp_path)
    monkeypatch.setattr(live, 'EVIDENCE', tmp_path / 'platform/live-lab/evidence/jenkins-followup')
    def fake_command(*args):
        if args[:2] == ('git', 'ls-files'):
            return '\0'.join(files)
        return 'reference'
    monkeypatch.setattr(live, 'command', fake_command)
    monkeypatch.setattr(live.subprocess, 'run', lambda *args, **kwargs: None)
    digest, pipeline = live.snapshot()
    archive = live.EVIDENCE / f'jenkins-source-{digest}.tar.gz'
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
    assert pipeline == files['Jenkinsfile']
    with tarfile.open(archive) as captured:
        assert set(captured.getnames()) == {'Jenkinsfile', 'app/new.py', '.jenkins-source-files.sha256'}
        assert captured.extractfile('app/new.py').read().decode() == files['app/new.py']
    metadata = json.loads((live.EVIDENCE / 'source.json').read_text())
    assert metadata['files']['app/new.py'] == hashlib.sha256(files['app/new.py'].encode()).hexdigest()


def test_local_snapshot_rejects_source_symlinks(tmp_path, monkeypatch):
    live = load_live_verifier()
    (tmp_path / 'Jenkinsfile').symlink_to(ROOT / 'Jenkinsfile')
    monkeypatch.setattr(live, 'ROOT', tmp_path)
    monkeypatch.setattr(live, 'command', lambda *args: 'Jenkinsfile')
    with pytest.raises(ValueError, match='Refusing source symlink'):
        live.snapshot()


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


def test_jenkinsfile_runs_only_the_approved_shared_verification_subset():
    jenkinsfile = read("Jenkinsfile")

    assert "PLATFORM_VERIFY_PHASES = 'tests manifests terraform python-security'" in jenkinsfile
    assert 'bash scripts/verify_platform.sh "${phase}"' in jenkinsfile

    forbidden = [
        "docker build",
        "docker run",
        "docker push",
        "k6",
        "trivy image",
        "aws ecr",
        "git push",
        "kubectl apply",
    ]
    for token in forbidden:
        assert token not in jenkinsfile


def test_controller_is_loopback_authenticated_and_has_no_executors():
    compose = read("platform/jenkins/docker-compose.yml")
    casc = read("platform/jenkins/casc.yaml")

    assert '"127.0.0.1:8080:8080"' in compose
    assert "jenkins_admin_password" in compose
    assert "numExecutors: 0" in casc
    assert "allowsSignup: false" in casc
    assert "Overall/Administer" in casc
    assert "mode: EXCLUSIVE" in casc
    # Keep Jenkins' sandboxed default CSP; an unquoted override splits JAVA_OPTS.
    assert 'DirectoryBrowserSupport.CSP=' not in compose


def test_agent_is_separate_non_root_and_dockerless():
    compose = read("platform/jenkins/docker-compose.yml")
    agent_dockerfile = read("platform/jenkins/Dockerfile.agent")
    casc = read("platform/jenkins/casc.yaml")

    assert "platform-agent:" in compose
    assert 'user: "1000:1000"' in compose
    assert "USER jenkins" in agent_dockerfile
    assert 'labelString: "platform-agent dockerless verifier"' in casc

    forbidden_mounts = [
        "/var/run/docker.sock",
        ".aws",
        "kubeconfig",
        ".kube",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ]
    combined = "\n".join([compose, agent_dockerfile, casc])
    for token in forbidden_mounts:
        assert token not in combined


def test_jenkins_runtime_is_pinned_and_bounded():
    compose = read("platform/jenkins/docker-compose.yml")
    controller_dockerfile = read("platform/jenkins/Dockerfile.controller")
    agent_dockerfile = read("platform/jenkins/Dockerfile.agent")
    plugins = read("platform/jenkins/plugins.txt")

    assert "FROM jenkins/jenkins:2.568.3-jdk21" in controller_dockerfile
    assert "FROM jenkins/inbound-agent:3391.va_37fa_a_305d6d-3-jdk21" in agent_dockerfile
    assert "ARG TERRAFORM_VERSION=1.16.4" in agent_dockerfile
    assert "ARG KUBECTL_VERSION=v1.37.0" in agent_dockerfile
    assert re.search(r"^  nodejs \\\s*$", agent_dockerfile, flags=re.MULTILINE)
    assert 'ENTRYPOINT ["/usr/local/bin/jenkins-agent-with-local-secret.sh"]' in agent_dockerfile
    assert "jenkins_agent_secret is empty" in agent_dockerfile

    assert len(re.findall(r"mem_limit:", compose)) == 2
    assert len(re.findall(r"pids_limit:", compose)) == 2
    assert len(re.findall(r"cpus:", compose)) == 2
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose

    expected_plugins = {
        "configuration-as-code:2121.v86fe99d4b_b_a_b_",
        "credentials-binding:728.v902a_273b_8947",
        "git:5.10.1",
        "matrix-auth:3.3",
        "pipeline-model-definition:2.2293.v6e7193cec599",
        "timestamper:1.30",
        "workflow-aggregator:608.v67378e9d3db_1",
        "ws-cleanup:0.49",
    }
    assert set(plugins.splitlines()) == expected_plugins


def test_local_secret_bootstrap_does_not_embed_source_secrets():
    bootstrap = read("platform/jenkins/bootstrap-local-secrets.sh")
    compose = read("platform/jenkins/docker-compose.yml")

    assert "/tmp/develope-project-jenkins-secrets" in bootstrap
    assert "secrets.token_urlsafe(32)" in bootstrap
    assert "tr -dc" not in bootstrap
    assert "/dev/urandom" not in bootstrap
    assert "jenkins_agent_secret" in bootstrap
    assert "JENKINS_SECRETS_DIR:?" in compose
    assert "JENKINS_ADMIN_PASSWORD=" not in compose
    assert "changeme" not in bootstrap.lower()
    assert "0o444" in bootstrap


def test_jenkins_sh_steps_use_bash_shebang_before_pipefail():
    jenkinsfile = read("Jenkinsfile")

    shell_bodies = re.findall(r"sh '''(.*?)'''", jenkinsfile, flags=re.DOTALL)
    assert shell_bodies
    for body in shell_bodies:
        if "pipefail" in body:
            assert body.startswith("#!/usr/bin/env bash\n")


def test_local_secret_bootstrap_runs_without_leaking_password(tmp_path):
    script = ROOT / "platform/jenkins/bootstrap-local-secrets.sh"
    secret_dir = tmp_path / "jenkins-secrets"
    env = os.environ.copy()
    env["JENKINS_SECRETS_DIR"] = str(secret_dir)

    result = subprocess.run(
        [str(script)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    admin_user = secret_dir / "jenkins_admin_user"
    admin_password = secret_dir / "jenkins_admin_password"
    agent_secret = secret_dir / "jenkins_agent_secret"

    assert admin_user.read_text().strip() == "admin"
    password_value = admin_password.read_text().strip()
    assert len(password_value) >= 32
    assert agent_secret.read_text() == ""
    assert password_value not in result.stdout
    assert str(admin_password) in result.stdout
    assert "Admin password:" not in result.stdout

    assert stat.S_IMODE(secret_dir.stat().st_mode) == 0o700
    for path in (admin_user, admin_password, agent_secret):
        assert stat.S_IMODE(path.stat().st_mode) == 0o444


def test_local_secret_bootstrap_rejects_symlink_secret_dir(tmp_path):
    script = ROOT / "platform/jenkins/bootstrap-local-secrets.sh"
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    secret_link = tmp_path / "link"
    secret_link.symlink_to(real_dir, target_is_directory=True)
    env = os.environ.copy()
    env["JENKINS_SECRETS_DIR"] = str(secret_link)

    result = subprocess.run(
        [str(script)],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode != 0
    assert "Refusing symlink secret directory" in result.stderr


def test_local_secret_bootstrap_rejects_dangling_symlink_secret_file(tmp_path):
    script = ROOT / "platform/jenkins/bootstrap-local-secrets.sh"
    secret_dir = tmp_path / "jenkins-secrets"
    secret_dir.mkdir(mode=0o700)
    (secret_dir / "jenkins_admin_password").symlink_to(secret_dir / "missing-password")
    env = os.environ.copy()
    env["JENKINS_SECRETS_DIR"] = str(secret_dir)

    result = subprocess.run(
        [str(script)],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode != 0
    assert "Refusing symlink secret file" in result.stderr


def test_local_secret_bootstrap_does_not_overwrite_existing_files(tmp_path):
    script = ROOT / "platform/jenkins/bootstrap-local-secrets.sh"
    secret_dir = tmp_path / "jenkins-secrets"
    secret_dir.mkdir(mode=0o700)
    existing_password = "already-created-secret\n"
    password_path = secret_dir / "jenkins_admin_password"
    password_path.write_text(existing_password)
    password_path.chmod(0o444)
    env = os.environ.copy()
    env["JENKINS_SECRETS_DIR"] = str(secret_dir)

    subprocess.run(
        [str(script)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    assert password_path.read_text() == existing_password
    assert stat.S_IMODE(password_path.stat().st_mode) == 0o444


def test_agent_secret_setter_updates_secret_without_echoing_value(tmp_path):
    bootstrap_script = ROOT / "platform/jenkins/bootstrap-local-secrets.sh"
    setter_script = ROOT / "platform/jenkins/set-agent-secret.sh"
    secret_dir = tmp_path / "jenkins-secrets"
    env = os.environ.copy()
    env["JENKINS_SECRETS_DIR"] = str(secret_dir)

    subprocess.run(
        [str(bootstrap_script)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    agent_secret = "abc123-agent-secret"
    result = subprocess.run(
        [str(setter_script)],
        check=True,
        text=True,
        input=f"{agent_secret}\n",
        capture_output=True,
        env=env,
    )

    agent_secret_path = secret_dir / "jenkins_agent_secret"
    assert agent_secret_path.read_text().strip() == agent_secret
    assert stat.S_IMODE(agent_secret_path.stat().st_mode) == 0o444
    assert agent_secret not in result.stdout
    assert agent_secret not in result.stderr
    assert str(agent_secret_path) in result.stdout
    assert not list(secret_dir.glob(".jenkins_agent_secret.*"))


def test_agent_secret_setter_rejects_empty_value(tmp_path):
    bootstrap_script = ROOT / "platform/jenkins/bootstrap-local-secrets.sh"
    setter_script = ROOT / "platform/jenkins/set-agent-secret.sh"
    secret_dir = tmp_path / "jenkins-secrets"
    env = os.environ.copy()
    env["JENKINS_SECRETS_DIR"] = str(secret_dir)

    subprocess.run(
        [str(bootstrap_script)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    result = subprocess.run(
        [str(setter_script)],
        text=True,
        input="\n",
        capture_output=True,
        env=env,
    )

    assert result.returncode != 0
    assert "Agent secret must not be empty." in result.stderr


def test_agent_secret_setter_rejects_insecure_secret_directory(tmp_path):
    setter_script = ROOT / "platform/jenkins/set-agent-secret.sh"
    secret_dir = tmp_path / "jenkins-secrets"
    secret_dir.mkdir(mode=0o700)
    secret_dir.chmod(0o777)
    env = os.environ.copy()
    env["JENKINS_SECRETS_DIR"] = str(secret_dir)

    result = subprocess.run(
        [str(setter_script)],
        text=True,
        input="abc123-agent-secret\n",
        capture_output=True,
        env=env,
    )

    assert result.returncode != 0
    assert "Secret directory must be private mode 0700" in result.stderr
