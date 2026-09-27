import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/rds-failover-drill.sh"


FAKE_KUBECTL = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
command = args[4]
with open(os.environ["KUBECTL_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
if command == "get":
    if "--ignore-not-found" in args:
        if os.environ.get("FIXTURE_GET_ERROR") == "true":
            raise SystemExit(7)
        if os.environ.get("FIXTURE_EXISTS") == "true":
            print(os.environ["FIXTURE_RESOURCE"])
    elif "job" in args and "-o" in args and "json" in args:
        print(json.dumps({"status": {"conditions": [{"type": "Complete", "status": "True"}]}}))
elif command == "create":
    with open(os.environ["MANIFESTS_PATH"], "a", encoding="utf-8") as stream:
        stream.write(sys.stdin.read() + "\n")
elif command == "logs":
    print('{"ok":true}')
elif command == "delete":
    pass
else:
    raise SystemExit(f"unexpected fake kubectl command: {args}")
'''


def drill_env(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    kubectl = fake_bin / "kubectl"
    kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
    kubectl.chmod(0o755)
    helper_source = tmp_path / "replica_drill.py"
    helper_source.write_text("# test helper source\n", encoding="utf-8")
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SESSION_ID": "test-session-123",
        "APPROVAL_ID": "SS0-20260927-test-approval",
        "AWS_PROFILE": "no-cloud-test",
        "AWS_REGION": "ap-northeast-2",
        "EXPECTED_AWS_ACCOUNT_ID": "123456789012",
        "KUBE_CONTEXT": "fake-context",
        "BASE_URL": "https://example.invalid",
        "REPLICA_LAG_THRESHOLD_SECONDS": "5",
        "RUN_ID": "rds-recovery-test-001",
        "DRILL_MODE": "execute",
        "OUTPUT_DIR": str(tmp_path / "evidence"),
        "HELPER_SOURCE": str(helper_source),
        "TEST_APP_IMAGE": "example.invalid/app@sha256:" + "a" * 64,
        "FIXTURE_EXISTS": "false",
        "FIXTURE_GET_ERROR": "false",
        "FIXTURE_RESOURCE": "configmap/foreign-resource",
        "KUBECTL_LOG": str(tmp_path / "kubectl.log"),
        "MANIFESTS_PATH": str(tmp_path / "created-manifests.jsonl"),
    }


def invoke_shell(tmp_path: Path, env: dict[str, str], body: str) -> subprocess.CompletedProcess[str]:
    script = textwrap.dedent(
        f'''\
        source {json.dumps(str(SCRIPT))}
        APP_IMAGE="$TEST_APP_IMAGE"
        export APP_IMAGE
        {body}
        '''
    )
    return subprocess.run(["bash", "-c", script], cwd=ROOT, env=env, text=True, capture_output=True, check=False)


def recorded_commands(env: dict[str, str]) -> list[list[str]]:
    log = Path(env["KUBECTL_LOG"])
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def created_manifests(env: dict[str, str]) -> list[dict]:
    path = Path(env["MANIFESTS_PATH"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_namespace_override_is_rejected_before_any_cluster_command(tmp_path: Path):
    env = drill_env(tmp_path)
    env["NAMESPACE"] = "default"
    result = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=env, text=True, capture_output=True, check=False)

    assert result.returncode == 2
    assert "fixed to platform-validation" in result.stderr
    assert recorded_commands(env) == []


def test_new_configmap_is_created_with_session_approval_and_run_labels(tmp_path: Path):
    env = drill_env(tmp_path)
    result = invoke_shell(tmp_path, env, "ensure_helper_configmap")

    assert result.returncode == 0, result.stderr
    [manifest] = created_manifests(env)
    labels = manifest["metadata"]["labels"]
    assert manifest["kind"] == "ConfigMap"
    assert manifest["metadata"]["namespace"] == "platform-validation"
    assert labels["app.kubernetes.io/managed-by"] == "rds-failover-drill"
    assert labels["live-lab-session"] == env["SESSION_ID"]
    assert labels["live-lab-approval"] == env["APPROVAL_ID"]
    assert labels["live-lab-run"]
    assert labels["live-lab-helper-token"]
    assert all(command[4] != "delete" for command in recorded_commands(env))


def test_existing_configmap_is_left_untouched_and_creation_fails_closed(tmp_path: Path):
    env = drill_env(tmp_path)
    env["FIXTURE_EXISTS"] = "true"
    result = invoke_shell(tmp_path, env, "ensure_helper_configmap")

    assert result.returncode != 0
    assert "already exists; refusing to reuse or replace" in result.stderr
    assert not Path(env["MANIFESTS_PATH"]).exists()
    assert all(command[4] != "delete" for command in recorded_commands(env))


@pytest.mark.parametrize(
    ("body", "resource_kind"),
    [
        ("ensure_helper_configmap", "ConfigMap"),
        ('helper_job readiness "$APP_IMAGE" --role writer', "Job"),
    ],
)
def test_cluster_lookup_error_is_not_treated_as_absence(tmp_path: Path, body: str, resource_kind: str):
    env = drill_env(tmp_path)
    env["FIXTURE_GET_ERROR"] = "true"
    result = invoke_shell(tmp_path, env, body)

    assert result.returncode != 0
    assert f"Could not safely inspect helper {resource_kind}" in result.stderr
    assert not Path(env["MANIFESTS_PATH"]).exists()
    assert all(command[4] != "delete" for command in recorded_commands(env))


def test_existing_job_name_collision_fails_without_replacement(tmp_path: Path):
    env = drill_env(tmp_path)
    env["FIXTURE_EXISTS"] = "true"
    env["FIXTURE_RESOURCE"] = "job/foreign-resource"
    result = invoke_shell(tmp_path, env, 'helper_job readiness "$APP_IMAGE" --role writer')

    assert result.returncode != 0
    assert "already exists; refusing to reuse or replace" in result.stderr
    assert not Path(env["MANIFESTS_PATH"]).exists()
    assert all(command[4] != "delete" for command in recorded_commands(env))


def test_jobs_have_unique_bounded_names_ownership_target_and_ttl_without_direct_deletes(tmp_path: Path):
    env = drill_env(tmp_path)
    result = invoke_shell(
        tmp_path,
        env,
        'ensure_helper_configmap\n'
        'job_result_one="$(helper_job readiness "$APP_IMAGE" --role writer)"\n'
        'job_result_two="$(helper_job readiness "$APP_IMAGE" --role reader)"\n'
        'cleanup_helpers',
    )

    assert result.returncode == 0, result.stderr
    manifests = created_manifests(env)
    configmap, *jobs = manifests
    assert len(jobs) == 2
    names = [job["metadata"]["name"] for job in jobs]
    assert len(names) == len(set(names))
    for job in jobs:
        assert len(job["metadata"]["name"]) <= 63
        assert job["metadata"]["namespace"] == "platform-validation"
        assert job["spec"]["activeDeadlineSeconds"] == 180
        assert job["spec"]["ttlSecondsAfterFinished"] == 600
        assert len(job["metadata"]["name"]) <= 63
        labels = job["metadata"]["labels"]
        assert labels["live-lab-session"] == env["SESSION_ID"]
        assert labels["live-lab-approval"] == env["APPROVAL_ID"]
        assert labels["live-lab-run"]
        assert labels["live-lab-helper-token"]
        assert labels["live-lab-helper-job-token"]
        assert job["spec"]["template"]["metadata"]["labels"] == labels
        volumes = job["spec"]["template"]["spec"]["volumes"]
        assert configmap["metadata"]["name"] in [v.get("configMap", {}).get("name") for v in volumes]
    assert all(command[4] != "delete" for command in recorded_commands(env))
