import subprocess
import os
from pathlib import Path

import yaml
import pytest
import importlib.util
import json
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE_DIR = REPO_ROOT / "platform" / "governance"
FIXTURES_DIR = GOVERNANCE_DIR / "fixtures"
VALIDATION_OVERLAY = REPO_ROOT / "k8s" / "overlays" / "validation"
EXPECTED_REPO = "https://github.com/masondev1024/aws-data-platform-gitops"
FORBIDDEN_REPO_FIXTURE = "https://example.invalid/forbidden-governance.git"
EXPECTED_NAMESPACE = "platform-validation"
EXPECTED_SERVER = "https://kubernetes.default.svc"

ALLOWED_APP_RESOURCES = {
    ("", "ConfigMap"),
    ("", "Service"),
    ("argoproj.io", "AnalysisTemplate"),
    ("argoproj.io", "Rollout"),
    ("autoscaling", "HorizontalPodAutoscaler"),
    ("batch", "CronJob"),
    ("batch", "Job"),
    ("monitoring.coreos.com", "PrometheusRule"),
    ("monitoring.coreos.com", "ServiceMonitor"),
    ("networking.k8s.io", "Ingress"),
}

FORBIDDEN_APP_RESOURCES = {
    ("", "LimitRange"),
    ("", "ResourceQuota"),
    ("", "Secret"),
    ("", "ServiceAccount"),
    ("argoproj.io", "Application"),
    ("argoproj.io", "AppProject"),
    ("networking.k8s.io", "NetworkPolicy"),
    ("rbac.authorization.k8s.io", "Role"),
    ("rbac.authorization.k8s.io", "RoleBinding"),
}


def render_kustomize(path: Path):
    result = subprocess.run(
        ["kubectl", "kustomize", str(path)],
        check=True,
        text=True,
        capture_output=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def load_yaml_documents(path: Path):
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def find_kind(docs, kind, name=None):
    matches = [
        doc
        for doc in docs
        if doc.get("kind") == kind
        and (name is None or doc.get("metadata", {}).get("name") == name)
    ]
    assert matches, f"missing {kind}/{name or '*'}"
    assert len(matches) == 1, f"expected one {kind}/{name or '*'}, got {len(matches)}"
    return matches[0]


def api_group(api_version: str) -> str:
    return "" if "/" not in api_version else api_version.split("/", 1)[0]


def resource_set(entries):
    return {(entry.get("group", ""), entry["kind"]) for entry in entries}


def test_governance_bootstrap_renders_without_prod_default_or_terraform_changes():
    docs = render_kustomize(GOVERNANCE_DIR)
    names = {(doc.get("kind"), doc.get("metadata", {}).get("name")) for doc in docs}

    assert ("Namespace", EXPECTED_NAMESPACE) in names
    assert ("ResourceQuota", "platform-validation-quota") in names
    assert ("LimitRange", "platform-validation-defaults") in names
    assert ("AppProject", "kyobo-platform-validation") in names
    assert ("Application", "data-pipeline-validation") in names

    namespaces = {
        doc.get("metadata", {}).get("namespace")
        for doc in docs
        if doc.get("metadata", {}).get("namespace")
    }
    assert namespaces <= {EXPECTED_NAMESPACE, "argocd"}


def test_argocd_project_has_exact_source_destination_and_app_resource_boundary():
    docs = render_kustomize(GOVERNANCE_DIR)
    project = find_kind(docs, "AppProject", "kyobo-platform-validation")
    spec = project["spec"]

    assert spec["sourceRepos"] == [EXPECTED_REPO]
    assert spec["destinations"] == [
        {"server": EXPECTED_SERVER, "namespace": EXPECTED_NAMESPACE}
    ]
    assert spec["clusterResourceWhitelist"] == []
    assert resource_set(spec["namespaceResourceWhitelist"]) == ALLOWED_APP_RESOURCES
    assert FORBIDDEN_APP_RESOURCES <= resource_set(spec["namespaceResourceBlacklist"])

    role_names = {role["name"] for role in spec["roles"]}
    assert role_names == {"governance-readonly", "governance-sync"}
    readonly = next(role for role in spec["roles"] if role["name"] == "governance-readonly")
    assert all(", sync," not in policy for policy in readonly["policies"])


def test_validation_application_is_manual_and_pinned_to_validation_overlay():
    docs = render_kustomize(GOVERNANCE_DIR)
    app = find_kind(docs, "Application", "data-pipeline-validation")
    spec = app["spec"]

    assert spec["project"] == "kyobo-platform-validation"
    assert spec["source"] == {
        "repoURL": EXPECTED_REPO,
        "targetRevision": "main",
        "path": "k8s/overlays/validation",
    }
    assert spec["destination"] == {
        "server": EXPECTED_SERVER,
        "namespace": EXPECTED_NAMESPACE,
    }
    assert "automated" not in spec.get("syncPolicy", {})
    assert "CreateNamespace=false" in spec["syncPolicy"]["syncOptions"]


def test_developer_rbac_is_readonly_and_cannot_touch_secrets_exec_pods_or_rbac():
    docs = render_kustomize(GOVERNANCE_DIR)
    role = find_kind(docs, "Role", "kyobo-developer-readonly")
    rules = role["rules"]
    verbs = {verb for rule in rules for verb in rule["verbs"]}
    resources = {resource for rule in rules for resource in rule["resources"]}

    assert verbs <= {"get", "list", "watch"}
    assert "secrets" not in resources
    assert "pods/exec" not in resources
    assert "serviceaccounts" not in resources
    assert "analysisruns" in resources
    assert not ({"create", "update", "patch", "delete", "bind", "escalate"} & verbs)
    assert find_kind(docs, "ServiceAccount", "kyobo-developer-readonly")[
        "automountServiceAccountToken"
    ] is False


def test_rendered_validation_overlay_stays_inside_app_project_whitelist():
    docs = render_kustomize(VALIDATION_OVERLAY)
    rendered_resources = {
        (api_group(doc["apiVersion"]), doc["kind"])
        for doc in docs
        if doc.get("kind") != "List"
    }

    assert rendered_resources <= ALLOWED_APP_RESOURCES
    assert not (rendered_resources & FORBIDDEN_APP_RESOURCES)
    assert all(
        doc.get("metadata", {}).get("namespace") in (None, EXPECTED_NAMESPACE)
        for doc in docs
    )


def test_allowed_and_forbidden_fixtures_document_app_lane_boundary():
    allowed = {
        (api_group(doc["apiVersion"]), doc["kind"])
        for doc in load_yaml_documents(FIXTURES_DIR / "allowed-app.yaml")
    }
    forbidden = {
        (api_group(doc["apiVersion"]), doc["kind"])
        for doc in load_yaml_documents(FIXTURES_DIR / "forbidden-app.yaml")
    }

    assert allowed <= ALLOWED_APP_RESOURCES
    assert forbidden <= FORBIDDEN_APP_RESOURCES
    assert not (allowed & forbidden)


def test_runtime_script_is_fixed_matrix_dry_run_by_default_and_has_positive_control():
    script = GOVERNANCE_DIR / "scripts" / "validate_governance_auth.sh"
    content = script.read_text()

    assert "--context, --namespace, and --principal are required" in content
    assert 'namespace" != "platform-validation"' in content
    assert "auth whoami" in content
    assert "positive control failed" in content
    assert "impersonation/setup failure is not an expected deny" in content
    assert 'auth can-i "$verb" "$resource"' in content
    assert "eval " not in content
    assert "$*" not in content
    assert "$@" not in content

    dry_run = subprocess.run(
        [
            str(script),
            "--context",
            "kind-validation",
            "--namespace",
            EXPECTED_NAMESPACE,
            "--principal",
            "system:serviceaccount:platform-validation:kyobo-developer-readonly",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "mode=dry-run" in dry_run.stdout
    assert "allow,get,pods," in dry_run.stdout
    assert "deny,create,pods,exec" in dry_run.stdout
    assert "deny,bind,clusterrole/admin," in dry_run.stdout


def invoke_auth(tmp_path, mode):
    fake = tmp_path / "kubectl"
    fake.write_text('''#!/usr/bin/env bash
case "$*" in
  *"auth can-i bind clusterrole/admin"*)
    if [[ "$*" != *"--all-namespaces"* ]]; then echo no; echo 'Warning: resource is not namespace scoped' >&2; exit 1; fi ;;
  *"auth whoami"*)
    if [[ "$FAKE_AUTH_MODE" == wrong_identity ]]; then printf '{"status":{"userInfo":{"username":"another-user"}}}'; exit 0; fi
    printf '{"status":{"userInfo":{"username":"system:serviceaccount:platform-validation:kyobo-developer-readonly"}}}'; exit 0 ;;
  *"auth can-i get pods"*|*"auth can-i get events"*|*"auth can-i get rollouts.argoproj.io"*|*"auth can-i get analysisruns.argoproj.io"*|*"auth can-i get servicemonitors.monitoring.coreos.com"*)
    if [[ "$FAKE_AUTH_MODE" == positive_denied ]]; then echo no; exit 1; fi
    echo yes; exit 0 ;;
esac
case "${FAKE_AUTH_MODE}" in
  deny) echo no; exit 1 ;;
  error) echo no; echo 'Forbidden impersonation failed' >&2; exit 1 ;;
  unexpected) echo nonsense; exit 0 ;;
  allow) echo yes; exit 0 ;;
esac
''')
    fake.chmod(0o755)
    return subprocess.run(["bash", str(GOVERNANCE_DIR / "scripts/validate_governance_auth.sh"),
                           "--context", "lab", "--namespace", EXPECTED_NAMESPACE,
                           "--principal", "system:serviceaccount:platform-validation:kyobo-developer-readonly", "--execute"],
                          env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "FAKE_AUTH_MODE": mode},
                          text=True, capture_output=True, timeout=10)


def test_expected_deny_exit_one_is_a_successful_negative_assertion(tmp_path):
    result = invoke_auth(tmp_path, "deny")
    assert result.returncode == 0, result.stderr
    assert "deny,deny,create,pods,exec" in result.stdout


@pytest.mark.parametrize("mode", ["error", "unexpected", "allow", "wrong_identity", "positive_denied"])
def test_api_error_or_unexpected_privilege_never_passes_negative_assertion(tmp_path, mode):
    result = invoke_auth(tmp_path, mode)
    assert result.returncode != 0


@pytest.fixture
def live_governance():
    path = REPO_ROOT / "platform/live-lab/scripts/verify_governance_live.py"
    spec = importlib.util.spec_from_file_location("verify_governance_live", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def live_args(tmp_path):
    return ["--account", "123456789012", "--region", "ap-northeast-2",
            "--session", "test-session", "--approval", "SS0-20260923-test",
            "--cluster", "kyobo-test-session", "--context", "test-context",
            "--evidence", str(tmp_path / "evidence.json")]


def response(code=0, stdout="{}", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def test_live_governance_offline_plan_never_contacts_external_systems(live_governance, tmp_path, monkeypatch):
    monkeypatch.setattr(live_governance, "run", lambda *a, **kw: pytest.fail("external command in plan mode"))
    assert live_governance.main(live_args(tmp_path)) == 0
    assert not (tmp_path / "evidence.json").exists()


@pytest.mark.parametrize("flag,value", [("--account", "wrong"), ("--region", "global"),
    ("--session", "../other"), ("--approval", "missing"), ("--cluster", "production")])
def test_live_governance_rejects_scope_before_external_commands(live_governance, tmp_path, monkeypatch, flag, value):
    args = live_args(tmp_path)
    args[args.index(flag) + 1] = value
    monkeypatch.setattr(live_governance, "run", lambda *a, **kw: pytest.fail("scope not checked"))
    assert live_governance.main(args + ["--execute"]) == 1


@pytest.mark.parametrize("code,text", [(0, "resource rbac.authorization.k8s.io:RoleBinding is not permitted in project kyobo-platform-validation"),
    (1, "rpc error: Unauthenticated"), (1, "Forbidden: cannot impersonate"),
    (1, "context deadline exceeded"), (1, "permission denied: applications, override")])
def test_live_governance_generic_errors_are_never_policy_evidence(live_governance, code, text):
    with pytest.raises(live_governance.CheckFailed):
        live_governance.require_denial(response(code, stderr=text), r"resource rbac\.authorization\.k8s\.io:RoleBinding is not permitted in project kyobo-platform-validation")


def test_live_governance_fixtures_are_valid_and_cannot_escalate_if_policy_breaks():
    cm = load_yaml_documents(FIXTURES_DIR / "live-positive/configmap.yaml")[0]
    rb = load_yaml_documents(FIXTURES_DIR / "live-forbidden/rolebinding.yaml")[0]
    assert cm["kind"] == "ConfigMap" and cm["data"]
    assert rb["kind"] == "RoleBinding"
    assert rb["roleRef"]["name"] == "kyobo-developer-readonly"
    assert rb["subjects"] == [{"kind": "ServiceAccount", "name": "kyobo-developer-readonly", "namespace": EXPECTED_NAMESPACE}]


def test_live_quota_probe_has_positive_control_and_no_workload_creation(live_governance, tmp_path, monkeypatch):
    calls = []
    def fake_run(argv, payload=None, **kw):
        calls.append((argv, json.loads(payload)))
        if len(calls) == 1:
            return response()
        return response(1, stderr="Error from server (Forbidden): exceeded quota: platform-validation-quota, requested: requests.cpu=5, used: requests.cpu=0, limited: requests.cpu=2")
    monkeypatch.setattr(live_governance, "run", fake_run)
    v = live_governance.Governance(live_governance.parser().parse_args(live_args(tmp_path)), {"checks": []})
    v.quota()
    assert all("--dry-run=server" in cmd for cmd, pod in calls)
    assert [len(pod["spec"]["containers"]) for cmd, pod in calls] == [1, 5]
    assert all(c["resources"]["requests"]["cpu"] == "1" for c in calls[1][1]["spec"]["containers"])


def test_live_governance_argo_transport_cannot_use_unrelated_server(live_governance, tmp_path):
    v = live_governance.Governance(live_governance.parser().parse_args(live_args(tmp_path)), {"checks": []})
    assert "--port-forward" in v.argo and "--core" not in v.argo
    assert v.argo[v.argo.index("--kube-context") + 1] == "test-context"
    config = Path(v.argo[v.argo.index("--config") + 1])
    assert not config.exists()  # Existing empty files trigger Argo current-context resolution.
    assert config.parent.stat().st_mode & 0o777 == 0o700
    assert config != Path(os.devnull)
    assert v.app_manifest(v.run_id)["spec"]["project"] == "kyobo-platform-validation"


@pytest.mark.parametrize("failure", ["sync", "cleanup", "unexpected", None])
def test_live_governance_main_always_cleans_up_and_sanitizes_errors(live_governance, tmp_path, monkeypatch, failure):
    calls = []
    monkeypatch.setattr(live_governance.signal, "signal", lambda *a: None)
    monkeypatch.setattr(live_governance.Governance, "preflight", lambda self: None)
    monkeypatch.setattr(live_governance.Governance, "rbac", lambda self: None)
    monkeypatch.setattr(live_governance.Governance, "quota", lambda self: None)
    def sync(self, directory):
        if failure == "sync":
            raise live_governance.CheckFailed("expected policy denial not proven")
        if failure == "unexpected":
            raise RuntimeError("secret-token-do-not-record")
    def cleanup(self):
        calls.append("cleanup")
        if failure == "cleanup":
            raise live_governance.CheckFailed("cleanup incomplete")
    monkeypatch.setattr(live_governance.Governance, "positive_and_denials", sync)
    monkeypatch.setattr(live_governance.Governance, "cleanup", cleanup)
    assert live_governance.main(live_args(tmp_path) + ["--execute"]) == (1 if failure else 0)
    assert calls == ["cleanup"]
    content = (tmp_path / "evidence.json").read_text()
    assert "secret-token" not in content
    assert json.loads(content)["status"] == ("failed" if failure else "passed")
    assert (tmp_path / "evidence.json").stat().st_mode & 0o777 == 0o600


def test_live_governance_evidence_never_overwrites_existing_file(live_governance, tmp_path, monkeypatch):
    (tmp_path / "evidence.json").write_text("previous")
    monkeypatch.setattr(live_governance, "run", lambda *a, **kw: pytest.fail("evidence preflight missing"))
    assert live_governance.main(live_args(tmp_path) + ["--execute"]) == 1
    assert (tmp_path / "evidence.json").read_text() == "previous"


@pytest.mark.parametrize("drift", [None, "account", "tags", "endpoint", "ca", "insecure", "project", "argo_uid"])
def test_live_governance_scope_matches_eks_and_real_argo(live_governance, tmp_path, monkeypatch, drift):
    a = live_governance.parser().parse_args(live_args(tmp_path))
    v = live_governance.Governance(a, {"checks": []})
    project = load_yaml_documents(GOVERNANCE_DIR / "argocd/app-project.yaml")[0]
    project["metadata"]["uid"] = "project-uid"
    endpoint = "https://expected.eks.amazonaws.com"
    cluster = {"arn": "arn:aws:eks:ap-northeast-2:123456789012:cluster/kyobo-test-session", "status": "ACTIVE",
               "endpoint": endpoint, "certificateAuthority": {"data": "ca-base64"},
               "tags": {"Project": "kyobo-platform-live-lab", "Session": "test-session", "Approval": a.approval}}
    if drift == "tags":
        cluster["tags"]["Session"] = "another-session"
    def aws(*args):
        if args[0] == "sts":
            return {"Account": "000000000000" if drift == "account" else a.account}
        return {"cluster": cluster}
    def get(kind, namespace, name):
        if kind == "appproject":
            p = json.loads(json.dumps(project))
            if drift == "project":
                p["spec"]["sourceRepos"] = ["*"]
            return p
        if kind == "resourcequota":
            return {"spec": {"hard": {"requests.cpu": "2"}}}
        return {"roleRef": {}, "subjects": []}
    def fake_run(argv, **kw):
        if argv[0] == "kubectl":
            assert "--raw" in argv
            conn = {"server": "https://wrong.example" if drift == "endpoint" else endpoint,
                    "certificate-authority-data": "wrong" if drift == "ca" else "ca-base64"}
            if drift == "insecure":
                conn["insecure-skip-tls-verify"] = True
            return response(stdout=json.dumps({"clusters": [{"cluster": conn}]}))
        p = json.loads(json.dumps(project))
        if drift == "argo_uid":
            p["metadata"]["uid"] = "wrong-uid"
        return response(stdout=json.dumps(p))
    monkeypatch.setenv("ARGOCD_AUTH_TOKEN", "dummy-not-logged")
    monkeypatch.setattr(v, "aws", aws)
    monkeypatch.setattr(v, "get", get)
    monkeypatch.setattr(live_governance, "run", fake_run)
    if drift:
        with pytest.raises(live_governance.CheckFailed):
            v.preflight()
    else:
        v.preflight()


def test_live_governance_positive_and_all_argo_denials_use_server(live_governance, tmp_path, monkeypatch):
    v = live_governance.Governance(live_governance.parser().parse_args(live_args(tmp_path)), {"checks": []})
    v.binding = {"subjects": [], "roleRef": {"name": "readonly"}}
    def get(kind, ns, name):
        if kind == "configmap":
            return {"metadata": {"labels": v.labels}, "data": {"purpose": "disposable-argocd-governance-positive-control"}}
        return v.binding
    commands = []
    created_repo_urls = []
    def fake_run(argv, **kw):
        commands.append(argv)
        if argv[0] == "kubectl":
            assert "get" in argv
            return response(stdout='{"items": []}')
        if "create" in argv:
            app = json.loads(Path(argv[argv.index("--file") + 1]).read_text())
            spec = app["spec"]
            repo_url = spec["source"]["repoURL"]
            created_repo_urls.append(repo_url)
            if repo_url == FORBIDDEN_REPO_FIXTURE:
                return response(1, stderr="rpc error: code = InvalidArgument desc = application repo https://example.invalid/forbidden-governance.git is not permitted in project 'kyobo-platform-validation'")
            if spec["destination"]["namespace"] == "default":
                return response(1, stderr="rpc error: code = InvalidArgument desc = application destination server 'https://kubernetes.default.svc' and namespace 'default' do not match any of the allowed destinations in project 'kyobo-platform-validation'")
            return response()
        if "get" in argv:
            return response(stdout='{"status":{"operationState":{"phase":"Succeeded"}}}')
        if "sync" in argv and "forbidden" in argv[argv.index("--local") + 1]:
            return response(1, stdout="resource rbac.authorization.k8s.io:RoleBinding is not permitted in project kyobo-platform-validation")
        return response()
    monkeypatch.setattr(v, "get", get)
    monkeypatch.setattr(live_governance, "run", fake_run)
    v.positive_and_denials(tmp_path)
    assert created_repo_urls == [EXPECTED_REPO, FORBIDDEN_REPO_FIXTURE, EXPECTED_REPO]
    assert len(v.report["checks"]) == 4
    assert sum("sync" in cmd for cmd in commands) == 2
    assert all(cmd[0] == "argocd" for cmd in commands if "create" in cmd or "sync" in cmd)


def test_live_governance_cleanup_refuses_mismatched_ownership(live_governance, tmp_path, monkeypatch):
    v = live_governance.Governance(live_governance.parser().parse_args(live_args(tmp_path)), {"checks": []})
    def fake_run(argv, **kw):
        assert "get" in argv  # No deletion after an ownership mismatch.
        return response(stdout=json.dumps({"items": [{"metadata": {"name": v.run_id, "labels": {}}}]}))
    monkeypatch.setattr(live_governance, "run", fake_run)
    with pytest.raises(live_governance.CheckFailed, match="cleanup incomplete"):
        v.cleanup()
    assert v.report["cleanup"]["status"] == "failed"


def test_pinned_argocd_install_has_bounded_replicas_resources_and_internal_service():
    path = GOVERNANCE_DIR / "scripts/install_argocd_live.py"
    spec = importlib.util.spec_from_file_location("install_argocd_live", path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    docs = [{"kind": "Deployment", "metadata": {"name": name},
             "spec": {"template": {"metadata": {}, "spec": {"containers": [{"name": "main"}]}}}}
            for name in ("argocd-server", "argocd-application-controller", "argocd-dex-server")]
    docs.append({"kind": "Service", "metadata": {"name": "argocd-server"}, "spec": {"type": "ClusterIP"}})
    result = installer.bounds(docs, "test-session", "SS0-20260923-test")
    assert installer.VERSION == "v3.5.3" and len(installer.SHA256) == 64
    assert [d["spec"]["replicas"] for d in result[:3]] == [1, 1, 0]
    assert all(d["metadata"]["namespace"] == "argocd" for d in result)
    for d in result[:3]:
        resources = d["spec"]["template"]["spec"]["containers"][0]["resources"]
        assert {"cpu", "memory", "ephemeral-storage"} <= set(resources["limits"])
    with pytest.raises(installer.g.CheckFailed):
        installer.bounds([{"kind": "Service", "metadata": {"name": "external"}, "spec": {"type": "LoadBalancer"}}],
                         "test-session", "SS0-20260923-test")


def test_presync_fixture_is_bounded_secretless_and_uses_image_python_entrypoint():
    job = load_yaml_documents(FIXTURES_DIR / "live-presync-failure/job.yaml")[0]
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PreSync"
    assert job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == 60
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False and pod["enableServiceLinks"] is False
    assert "volumes" not in pod
    c = pod["containers"][0]
    assert c["args"] == ["migrate.py"] and "env" not in c and "envFrom" not in c and "volumeMounts" not in c
    assert c["securityContext"]["readOnlyRootFilesystem"] is True


@pytest.mark.parametrize("scenario", ["expected", "applied_configmap", "wrong_log", "wrong_operation", "wrong_image"])
def test_presync_failure_requires_hook_log_and_absent_normal_resource(live_governance, tmp_path, monkeypatch, scenario):
    args = live_governance.parser().parse_args(live_args(tmp_path))
    image = tmp_path / "image-ref.txt"
    image.write_text("123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/kyobo-test-session/data-pipeline-app@sha256:" + "a" * 64)
    if scenario == "wrong_image":
        image.write_text("production/app:latest")
    args.presync_image_file = str(image)
    v = live_governance.Governance(args, {"checks": []})
    name = v.run_id + "-gate"
    def fake_run(argv, **kw):
        if scenario == "wrong_image":
            pytest.fail("invalid image reached external API")
        if argv[0] == "argocd":
            if "create" in argv:
                return response()
            if "sync" in argv:
                return response(1)
            phase = "Error" if scenario == "wrong_operation" else "Failed"
            return response(stdout=json.dumps({"status": {"operationState": {"phase": phase, "syncResult": {
                "resources": [{"kind": "Job", "name": name, "hookType": "PreSync", "hookPhase": "Failed"}]}}}}))
        if "configmaps" in argv:
            return response(stdout=json.dumps({"items": [{"metadata": {"name": name}}] if scenario == "applied_configmap" else []}))
        if "pods" in argv:
            return response(stdout=json.dumps({"items": [{"metadata": {"labels": v.labels}, "status": {
                "containerStatuses": [{"state": {"terminated": {"exitCode": 1}}}]}}]}))
        if "logs" in argv:
            return response(stdout="unrelated failure" if scenario == "wrong_log" else
                "RuntimeError: DB_WRITER_HOST, DB_ADMIN_PASSWORD, DB_APP_USER, and DB_APP_PASSWORD must be configured for migrations")
        pytest.fail(str(argv))
    monkeypatch.setattr(live_governance, "run", fake_run)
    monkeypatch.setattr(v, "get", lambda *args: {"metadata": {"labels": v.labels}, "status": {
        "conditions": [{"type": "Failed", "status": "True"}]}})
    if scenario == "expected":
        v.presync_failure_gate(tmp_path)
        assert v.report["checks"][0]["normal_configmap_present"] is False
        assert "rollback" in v.report["checks"][0]["limitation"]
    else:
        with pytest.raises(live_governance.CheckFailed):
            v.presync_failure_gate(tmp_path)
