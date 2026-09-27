"""Offline contract checks; these do not prove a live Argo deployment."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("delivery", ROOT / "platform/live-lab/scripts/deploy_gitops_validation.py")
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


@pytest.fixture
def workload():
    result = subprocess.run(["kubectl", "kustomize", str(ROOT / "k8s/overlays/validation")],
                            text=True, capture_output=True, check=True, timeout=20)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def test_gitops_owns_workloads_but_not_dynamic_platform_resources(workload):
    kinds = {item["kind"] for item in workload}
    assert {"Job", "Rollout", "CronJob", "Service", "AnalysisTemplate"} <= kinds
    assert not kinds & {"Ingress", "Namespace", "Secret", "Role", "RoleBinding"}
    assert all(item["metadata"]["namespace"] == "platform-validation" for item in workload)
    job = next(item for item in workload if item["kind"] == "Job")
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PreSync"
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"][-1] == {"secretRef": {"name": "raffle-migration-secret"}}
    for item in workload:
        if item["kind"] in {"Rollout", "Job"}:
            pod = item["spec"]["template"]["spec"]
        elif item["kind"] == "CronJob":
            pod = item["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        else:
            continue
        assert not pod["automountServiceAccountToken"]
        assert any(volume.get("configMap", {}).get("name") == "rds-ca-bundle" for volume in pod["volumes"])
        assert all(container.get("image") for container in pod["containers"])
        if item["kind"] == "Rollout":
            app = pod["containers"][0]
            assert app["readinessProbe"]["httpGet"]["path"] == "/readyz"
            assert app["securityContext"]["readOnlyRootFilesystem"] is True
            assert app["envFrom"][-1] == {"secretRef": {"name": "raffle-secret"}}


def test_mismatched_or_unreviewed_image_blocks_bootstrap(workload):
    with pytest.raises(delivery.g.CheckFailed, match="reviewed image"):
        delivery.validate_workload(workload, "registry.invalid/reviewed@sha256:" + "a" * 64)


@pytest.mark.parametrize("key,value", [("argocd.argoproj.io/hook", "Sync"),
                                       ("argocd.argoproj.io/hook-delete-policy", "HookFailed")])
def test_migration_gate_cannot_be_removed_at_deploy_time(workload, key, value):
    job = next(obj for obj in workload if obj["kind"] == "Job")
    job["metadata"]["annotations"][key] = value
    with pytest.raises(delivery.g.CheckFailed, match="PreSync"):
        delivery.validate_workload(workload, "data-pipeline-app:pending-reviewed-release")


def test_ambiguous_sync_request_cannot_be_reported_as_failed_or_retried_automatically():
    source = (ROOT / "platform/live-lab/scripts/deploy_gitops_validation.py").read_text()
    assert 'report["status"] = "sync_request_outcome_unknown"' in source
    assert 'raise SyncRequestUnknown("sync request outcome unknown; inspect Application before retrying")' in source


def test_bootstrap_cannot_apply_application_resources():
    source = (ROOT / "platform/live-lab/scripts/deploy_gitops_validation.py").read_text()
    assert '"sync": {"revision": a.gitops_revision, "prune": True}' in source
    assert '"/metadata/resourceVersion"' in source
    assert "checked_revision(a.gitops_revision)" in source
    assert '"application_resources_directly_applied": False' in source


@pytest.fixture
def outputs():
    return {key: {"value": value} for key, value in {
        "session_id": "delivery-20260927", "approval_id": "SS0-20260927-delivery",
        "cluster_name": "kyobo-delivery-20260927",
        "db_writer_endpoint": "writer.sample.ap-northeast-2.rds.amazonaws.com",
        "db_reader_endpoint": "reader.sample.ap-northeast-2.rds.amazonaws.com",
        "operator_cidr": "203.0.113.4/32",
        "waf_web_acl_arn": "arn:aws:wafv2:ap-northeast-2:123456789012:regional/webacl/kyobo-delivery-20260927-web-acl/abc-123",
    }.items()}


def bootstrap(outputs):
    return delivery.session_bootstrap(outputs, "arn:aws:acm:ap-northeast-2:123456789012:certificate/abc-123",
                                      "delivery-20260927", "SS0-20260927-delivery", "123456789012", "ap-northeast-2")


def test_bootstrap_contains_only_scoped_dynamic_values(outputs):
    docs = bootstrap(outputs)
    assert not {obj["kind"] for obj in docs} & {"Rollout", "Job", "CronJob", "Secret", "Pod", "Deployment"}
    ingress = next(obj for obj in docs if obj["kind"] == "Ingress")
    assert ingress["metadata"]["annotations"]["alb.ingress.kubernetes.io/inbound-cidrs"] == "203.0.113.4/32"
    assert all(obj["metadata"]["labels"]["live-lab-session"] == "delivery-20260927" for obj in docs)
    assert "__" not in yaml.safe_dump_all(docs)


@pytest.mark.parametrize("key,value", [
    ("session_id", "another-session"), ("approval_id", "SS0-20260927-another"),
    ("cluster_name", "unrelated"), ("operator_cidr", "0.0.0.0/0"),
    ("db_writer_endpoint", "outside.example.com"),
    ("waf_web_acl_arn", "arn:aws:wafv2:ap-northeast-2:123456789012:regional/webacl/unrelated/abc-123"),
])
def test_bootstrap_refuses_cross_scope_or_public_ingress(outputs, key, value):
    outputs[key]["value"] = value
    with pytest.raises(delivery.g.CheckFailed):
        bootstrap(outputs)


def test_real_observer_config_does_not_inherit_admin_plugins_or_impersonation():
    spec = importlib.util.spec_from_file_location("observer", ROOT / "platform/live-lab/scripts/observe_gitops_as_developer.py")
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    config = observer.restricted_config({"server": "https://cluster.example.test", "certificate-authority-data": "Y2E=",
                                         "insecure-skip-tls-verify": True, "proxy-url": "https://untrusted.test"}, "fake.test.token")
    assert config["clusters"][0]["cluster"] == {"server": "https://cluster.example.test", "certificate-authority-data": "Y2E="}
    assert config["users"] == [{"name": "developer", "user": {"token": "fake.test.token"}}]
    assert "exec" not in yaml.safe_dump(config)
    assert "as-user" not in yaml.safe_dump(config)


def test_cleanup_stops_owned_gitops_before_namespace_and_controller_cleanup():
    script = (ROOT / "platform/live-lab/scripts/teardown_live_lab.sh").read_text()
    assert script.index("Application from another session") < script.index("delete application data-pipeline-validation")
    assert script.index("delete application data-pipeline-validation") < script.index("delete namespace platform-validation")
    assert script.index("delete namespace platform-validation") < script.index("helm --kube-context")


def test_signed_bundle_is_reverified_and_bound_to_git_candidate(tmp_path, monkeypatch):
    source_sha = "a" * 40
    digest = "sha256:" + "b" * 64
    image = f"123456789012.dkr.ecr.eu-west-1.amazonaws.com/data-pipeline-app:{source_sha}@{digest}"
    manifest = tmp_path / "k8s/overlays/validation/kustomization.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("reviewed candidate\n")
    bundle = tmp_path / "bundle"
    candidate = bundle / "validation-candidate/validation-kustomization.yaml"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(manifest.read_bytes())
    calls = []

    def run(command):
        calls.append(command)
        if "show" in command:
            return subprocess.CompletedProcess(command, 0, "source manifest before image PR\n", "")
        if "--verify-attestation" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps({"attestation": "verified",
                "oci_image_subject_verified": True, "image_digest": digest}), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(delivery, "ROOT", tmp_path)
    monkeypatch.setattr(delivery.g, "run", run)
    result = delivery.verify_signed_delivery(bundle, image)
    assert result["source_revision"] == source_sha
    verify = next(command for command in calls if "--verify-attestation" in command)
    assert "--verify-image-attestation" in verify
    assert verify[verify.index("--candidate-target") + 1] == "validation"
    assert calls[0][-3:] == ["--is-ancestor", source_sha, "HEAD"]
    candidate.write_text("unreviewed change\n")
    with pytest.raises(delivery.g.CheckFailed, match="differs"):
        delivery.verify_signed_delivery(bundle, image)


def test_bootstrap_does_not_mutate_before_provenance_verification():
    source = (ROOT / "platform/live-lab/scripts/deploy_gitops_validation.py").read_text()
    assert source.index('report["supply_chain"] = verify_signed_delivery') < source.index('g.run(kube + ["apply"')
