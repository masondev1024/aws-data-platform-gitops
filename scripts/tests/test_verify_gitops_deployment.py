import importlib.util
import json
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

import pytest


SPEC = importlib.util.spec_from_file_location(
    "verify_gitops_deployment", Path(__file__).parents[1] / "verify_gitops_deployment.py"
)
deployment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deployment)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
REVISION = "a" * 40
IMAGE_DIGEST = "b" * 64
IMAGE = f"registry.example.com/platform/data-pipeline-app:release-27@sha256:{IMAGE_DIGEST}"
PRINCIPAL = "system:serviceaccount:platform-validation:kyobo-developer-readonly"
ROLLOUT_UID = "rollout-uid-1"
REPLICASET_UID = "replicaset-uid-1"
POD_UID = "pod-uid-1"
POD_HASH = "rollout-hash-1"


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def snapshot_fixture():
    return {
        "collected_at": (NOW - timedelta(seconds=20)).isoformat(),
        "identity": {"status": {"userInfo": {"username": PRINCIPAL}}},
        "permissions": [{**check, "allowed": check["expected"]} for check in deployment.CHECKS],
        "application": {
            "metadata": {"name": deployment.APPLICATION, "namespace": "argocd"},
            "spec": {
                "source": {
                    "repoURL": deployment.REPOSITORY,
                    "path": "k8s/overlays/validation",
                    "targetRevision": "main",
                },
                "destination": {
                    "server": "https://kubernetes.default.svc",
                    "namespace": deployment.NAMESPACE,
                },
            },
            "status": {
                "reconciledAt": (NOW - timedelta(seconds=45)).isoformat(),
                "sync": {"revision": REVISION, "status": "Synced"},
                "health": {"status": "Healthy"},
                "operationState": {
                    "phase": "Succeeded",
                    "operation": {"sync": {"revision": REVISION}},
                    "syncResult": {"revision": REVISION},
                },
            },
        },
        "rollout": {
            "metadata": {
                "name": deployment.ROLLOUT,
                "namespace": deployment.NAMESPACE,
                "uid": ROLLOUT_UID,
                "generation": 1,
            },
            "spec": {
                "replicas": 1,
                "template": {"spec": {"containers": [{"name": "app-container", "image": IMAGE}]}},
            },
            "status": {
                # Argo Rollouts has emitted this field as a decimal string.
                "observedGeneration": "1",
                "abort": None,
                "phase": "Healthy",
                "readyReplicas": 1,
                "availableReplicas": 1,
                "updatedReplicas": 1,
                "stableRS": POD_HASH,
                "currentPodHash": POD_HASH,
            },
        },
        "replicasets": {
            "metadata": {},
            "items": [
                {
                    "metadata": {
                        "name": "data-pipeline-rollout-" + POD_HASH,
                        "namespace": deployment.NAMESPACE,
                        "uid": REPLICASET_UID,
                        "labels": {"rollouts-pod-template-hash": POD_HASH},
                        "ownerReferences": [
                            {
                                "apiVersion": "argoproj.io/v1alpha1",
                                "kind": "Rollout",
                                "name": deployment.ROLLOUT,
                                "uid": ROLLOUT_UID,
                                "controller": True,
                            }
                        ],
                    },
                    "spec": {
                        "template": {"spec": {"containers": [{"name": "app-container", "image": IMAGE}]}}
                    },
                }
            ],
        },
        "pods": {
            "metadata": {},
            "items": [
                {
                    "metadata": {
                        "name": "data-pipeline-pod-1",
                        "namespace": deployment.NAMESPACE,
                        "uid": POD_UID,
                        "ownerReferences": [
                            {
                                "apiVersion": "apps/v1",
                                "kind": "ReplicaSet",
                                "name": "data-pipeline-rollout-" + POD_HASH,
                                "uid": REPLICASET_UID,
                                "controller": True,
                            }
                        ],
                    },
                    "spec": {"containers": [{"name": "app-container", "image": IMAGE}]},
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "containerStatuses": [
                            {
                                "name": "app-container",
                                "ready": True,
                                "state": {"running": {"startedAt": NOW.isoformat()}},
                                "imageID": f"docker-pullable://registry.example.com/platform/data-pipeline-app@sha256:{IMAGE_DIGEST}",
                            }
                        ],
                    },
                }
            ],
        },
    }


def evaluate(snapshot=None):
    return deployment.evaluate_snapshot(
        snapshot_fixture() if snapshot is None else snapshot,
        expected_revision=REVISION,
        expected_image=IMAGE,
        expected_principal=PRINCIPAL,
        application=deployment.APPLICATION,
        namespace=deployment.NAMESPACE,
        now=NOW,
    )


def cli_args(*extra):
    return [
        "--context", "validation-context",
        "--namespace", deployment.NAMESPACE,
        "--principal", PRINCIPAL,
        "--application", deployment.APPLICATION,
        "--expected-revision", REVISION,
        "--expected-image", IMAGE,
        *extra,
    ]


def kubeconfig_fixture(context="validation-context"):
    return {
        "contexts": [{"name": context, "context": {"cluster": "cluster", "user": "user"}}],
        "clusters": [{"name": "cluster", "cluster": {"server": "https://cluster.test"}}],
        "users": [{"name": "user", "user": {"token": "REDACTED"}}],
    }


def install_fake_subprocess(monkeypatch, *, identity=PRINCIPAL, auth_overrides=None,
                           change_final_identity=None, omit_initial_identity_field=None, kubeconfig=None):
    monkeypatch.setattr(deployment, "datetime", FrozenDateTime)
    snapshot = snapshot_fixture()
    current = NOW
    snapshot["collected_at"] = (current - timedelta(seconds=5)).isoformat()
    snapshot["application"]["status"]["reconciledAt"] = (current - timedelta(seconds=45)).isoformat()
    snapshot["application"]["metadata"].update(uid="application-uid", resourceVersion="app-rv-7")
    snapshot["rollout"]["metadata"].update(uid=ROLLOUT_UID, resourceVersion="rollout-rv-4")
    snapshot["application"]["status"]["conditions"] = []
    if omit_initial_identity_field:
        resource, field = omit_initial_identity_field
        snapshot[resource]["metadata"].pop(field, None)
    calls = []
    app_reads = 0
    rollout_reads = 0
    permission_index = 0
    auth_overrides = auth_overrides or {}

    def fake_run(command, **kwargs):
        nonlocal app_reads, rollout_reads, permission_index
        command = list(command)
        calls.append((command, kwargs))
        if "config" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(kubeconfig or kubeconfig_fixture()), "")
        if "whoami" in command:
            body = {"status": {"userInfo": {"username": identity}}}
            return subprocess.CompletedProcess(command, 0, json.dumps(body), "")
        if "can-i" in command:
            check = deployment.CHECKS[permission_index]
            permission_index += 1
            override = auth_overrides.get(permission_index - 1)
            if override is not None:
                code, stdout, stderr = override
            else:
                code = 0 if check["expected"] else 1
                stdout = "yes\n" if check["expected"] else "no\n"
                stderr = ""
            return subprocess.CompletedProcess(command, code, stdout, stderr)
        resource = command[command.index("get") + 1]
        if resource == "applications.argoproj.io":
            app_reads += 1
            body = copy.deepcopy(snapshot["application"])
            if change_final_identity and change_final_identity[0] == "application" and app_reads == 2:
                body["metadata"][change_final_identity[1]] = f"app-{change_final_identity[1]}-changed"
        elif resource == "rollouts.argoproj.io":
            rollout_reads += 1
            body = copy.deepcopy(snapshot["rollout"])
            if change_final_identity and change_final_identity[0] == "rollout" and rollout_reads == 2:
                body["metadata"][change_final_identity[1]] = f"rollout-{change_final_identity[1]}-changed"
        elif resource == "replicasets.apps":
            body = snapshot["replicasets"]
        elif resource == "pods":
            body = snapshot["pods"]
        else:
            raise AssertionError(f"unexpected kubectl resource: {resource}")
        return subprocess.CompletedProcess(command, 0, json.dumps(body), "")

    monkeypatch.setattr(deployment.subprocess, "run", fake_run)
    return calls


def test_healthy_gitops_snapshot_verifies_merge_revision_and_decimal_generation():
    report = evaluate()

    assert report["status"] == "verified"
    assert report["scope"] == "control_plane_only"
    assert report["gitops_revision"] == REVISION
    assert report["image_digest"] == f"sha256:{IMAGE_DIGEST}"
    assert report["ready_pods"] == 1
    assert report["pr_approval_verified"] is False
    assert report["traffic_verified"] is False
    assert report["data_parity_verified"] is False
    # targetRevision is the moving branch; the observed merge SHA is independently pinned.
    assert snapshot_fixture()["application"]["spec"]["source"]["targetRevision"] == "main"


def test_dry_run_cli_makes_no_external_calls_and_uses_no_impersonation(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run attempted an external command")

    monkeypatch.setattr(deployment.subprocess, "run", forbidden)
    assert deployment.main(cli_args()) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["mode"] == "dry_run"
    assert report["status"] == "not_executed"
    assert report["scope"] == "control_plane_only"
    assert report["commands"]
    assert all("--as" not in command for command in report["commands"])
    assert report["commands"][0] == [
        "kubectl", "--context", "validation-context", "config", "view", "--minify", "-o", "json"
    ]
    assert all("--raw" not in command for command in report["commands"])


def test_live_cli_uses_actual_identity_read_only_commands_and_final_resource_reads(monkeypatch, capsys):
    calls = install_fake_subprocess(monkeypatch)
    captured_snapshots = []
    evaluate_snapshot = deployment.evaluate_snapshot

    def capture_snapshot(snapshot, **kwargs):
        captured_snapshots.append(snapshot)
        return evaluate_snapshot(snapshot, **kwargs)

    monkeypatch.setattr(deployment, "evaluate_snapshot", capture_snapshot)

    assert deployment.main(cli_args("--execute")) == 0
    output = capsys.readouterr().out
    report = json.loads(output)

    assert report["status"] == "verified"
    assert report["mode"] == "live_read_only"
    assert report["scope"] == "control_plane_only"
    assert calls[0][0] == ["kubectl", "--context", "validation-context", "config", "view", "--minify", "-o", "json"]
    assert "whoami" in calls[1][0]
    assert sum("applications.argoproj.io" in command and "get" in command and "auth" not in command
               for command, _ in calls) == 2
    assert sum("rollouts.argoproj.io" in command and "get" in command and "auth" not in command
               for command, _ in calls) == 2
    assert len(calls) == 2 + len(deployment.CHECKS) + 6
    permission_commands = [command for command, _ in calls if "can-i" in command]
    assert len(permission_commands) == len(deployment.CHECKS)
    for check, command in zip(deployment.CHECKS, permission_commands):
        assert command[command.index("--namespace") + 1] == check.get("namespace", deployment.NAMESPACE)
        assert command[command.index("auth") + 2:command.index("auth") + 4] == [check["verb"], check["resource"]]
        if "subresource" in check:
            assert command[command.index("--subresource") + 1] == check["subresource"]
        else:
            assert "--subresource" not in command
    for command, kwargs in calls:
        assert command[0] == "kubectl"
        assert "--as" not in command
        assert command[command.index("--context") + 1] == "validation-context"
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "check": False,
            "shell": False,
            "timeout": 15,
        }
        if "config" in command:
            assert "--namespace" not in command
            assert "--raw" not in command
        else:
            assert "--namespace" in command
            if "can-i" in command:
                continue  # Namespace is checked against each dynamic CHECKS entry above.
            namespace = command[command.index("--namespace") + 1]
            assert namespace == (
                "argocd" if "get" in command and "applications.argoproj.io" in command
                else deployment.NAMESPACE
            )
    assert "env" not in report and "containerStatuses" not in report
    assert "REDACTED" not in output
    assert len(captured_snapshots) == 1
    assert "config" not in captured_snapshots[0]
    assert "REDACTED" not in json.dumps(captured_snapshots[0])
    assert captured_snapshots[0]["permissions"] == [
        {**check, "allowed": check["expected"]} for check in deployment.CHECKS
    ]


def test_live_identity_mismatch_stops_before_authorization_or_resource_queries(monkeypatch, capsys):
    calls = install_fake_subprocess(monkeypatch, identity="system:serviceaccount:other:admin")

    assert deployment.main(cli_args("--execute")) == 1
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == "not_ready"
    assert report["reason"] == "identity_mismatch"
    assert len(calls) == 2  # redacted context preflight, then actual-user identity


@pytest.mark.parametrize(
    ("response", "status", "reason"),
    [
        ((0, "no\n", ""), "unknown", "permission_query_failed"),
        ((1, "no\n", "Forbidden"), "unknown", "permission_query_failed"),
        ((1, "maybe\n", ""), "unknown", "permission_query_failed"),
        ((0, "yes\n", ""), "not_ready", "permission_policy_mismatch"),
    ],
)
def test_expected_denial_is_distinct_from_query_error(monkeypatch, capsys, response, status, reason):
    denied_check_index = next(
        index for index, check in enumerate(deployment.CHECKS) if not check["expected"]
    )
    calls = install_fake_subprocess(monkeypatch, auth_overrides={denied_check_index: response})

    assert deployment.main(cli_args("--execute")) == {"verified": 0, "not_ready": 1, "unknown": 2}[status]
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == status
    assert report["reason"] == reason
    assert not any("get" in command and "auth" not in command for command, _ in calls)


ARGO_DENIED_CHECKS = [
    (index, check)
    for index, check in enumerate(deployment.CHECKS)
    if check.get("namespace") == "argocd" and not check["expected"]
]


@pytest.mark.parametrize(
    ("check_index", "check"),
    ARGO_DENIED_CHECKS,
    ids=[
        f"{check['verb']}-{check['resource']}"
        for _, check in ARGO_DENIED_CHECKS
    ],
)
def test_live_collector_fails_if_any_broad_argocd_permission_is_allowed(
    monkeypatch, capsys, check_index, check
):
    calls = install_fake_subprocess(
        monkeypatch,
        auth_overrides={check_index: (0, "yes\n", "")},
    )

    assert deployment.main(cli_args("--execute")) == 1
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == "not_ready"
    assert report["reason"] == "permission_policy_mismatch"
    permission_commands = [command for command, _ in calls if "can-i" in command]
    assert len(permission_commands) == check_index + 1
    command = permission_commands[-1]
    assert command[command.index("--namespace") + 1] == check.get("namespace", deployment.NAMESPACE)
    assert command[command.index("auth") + 2:command.index("auth") + 4] == [
        check["verb"], check["resource"]
    ]
    assert not any("get" in command and "auth" not in command for command, _ in calls)


@pytest.mark.parametrize("resource", ["application", "rollout"])
@pytest.mark.parametrize("field", ["uid", "resourceVersion"])
def test_live_collector_rejects_resource_identity_change_between_reads(monkeypatch, capsys, resource, field):
    calls = install_fake_subprocess(monkeypatch, change_final_identity=(resource, field))

    assert deployment.main(cli_args("--execute")) == 2
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == "unknown"
    assert report["reason"] == "resources_changed_during_collection"
    token = "applications.argoproj.io" if resource == "application" else "rollouts.argoproj.io"
    assert sum("get" in command and "auth" not in command and token in command for command, _ in calls) == 2


@pytest.mark.parametrize("resource", ["application", "rollout"])
@pytest.mark.parametrize("field", ["uid", "resourceVersion"])
def test_live_collector_requires_initial_resource_identity_fields(monkeypatch, capsys, resource, field):
    install_fake_subprocess(monkeypatch, omit_initial_identity_field=(resource, field))

    assert deployment.main(cli_args("--execute")) == 2
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == "unknown"
    assert report["reason"] == "missing_resource_identity"


def test_verify_context_accepts_only_the_selected_redacted_https_context():
    assert deployment.verify_context(kubeconfig_fixture(), "validation-context") is None


def test_permission_matrix_declares_the_actual_list_queries():
    for resource in ("pods", "replicasets.apps"):
        assert {"verb": "list", "resource": resource, "expected": True} in deployment.CHECKS


@pytest.mark.parametrize("field", ["as", "as-uid", "as-groups", "as-user-extra"])
def test_verify_context_rejects_kubeconfig_impersonation(field):
    config = kubeconfig_fixture()
    config["users"][0]["user"][field] = "untrusted-identity"

    with pytest.raises(deployment.ObservationError) as error:
        deployment.verify_context(config, "validation-context")

    assert error.value.status == "unknown"
    assert str(error.value) == "kubeconfig_impersonation_not_allowed"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda config: config["clusters"][0]["cluster"].update(server="http://cluster.test"), "insecure_kube_connection"),
        (lambda config: config["clusters"][0]["cluster"].update(**{"insecure-skip-tls-verify": True}), "insecure_kube_connection"),
    ],
)
def test_verify_context_rejects_insecure_transport(change, reason):
    config = kubeconfig_fixture()
    change(config)

    with pytest.raises(deployment.ObservationError) as error:
        deployment.verify_context(config, "validation-context")

    assert error.value.status == "unknown"
    assert str(error.value) == reason


def test_live_cli_rejects_kubeconfig_impersonation_before_whoami(monkeypatch, capsys):
    config = kubeconfig_fixture()
    config["users"][0]["user"]["as-groups"] = ["system:masters"]
    calls = install_fake_subprocess(monkeypatch, kubeconfig=config)

    assert deployment.main(cli_args("--execute")) == 2
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == "unknown"
    assert report["reason"] == "kubeconfig_impersonation_not_allowed"
    assert len(calls) == 1


def test_missing_application_error_condition_is_not_hidden_by_cached_healthy_status():
    snapshot = snapshot_fixture()
    snapshot["application"]["status"]["conditions"] = [{"type": "ComparisonError", "message": "private-error"}]

    report = evaluate(snapshot)

    assert report["status"] == "not_ready"
    assert report["reason"] == "application_reconciliation_error"
    assert "private-error" not in json.dumps(report)


@pytest.mark.parametrize(
    ("seconds", "status", "reason"),
    [
        (-301, "unknown", "stale_or_future_argo_reconcile"),
        (31, "unknown", "stale_or_future_argo_reconcile"),
    ],
)
def test_argo_reconcile_must_be_fresh_and_not_too_far_in_the_future(seconds, status, reason):
    snapshot = snapshot_fixture()
    snapshot["application"]["status"]["reconciledAt"] = (NOW + timedelta(seconds=seconds)).isoformat()

    report = evaluate(snapshot)

    assert report["status"] == status
    assert report["reason"] == reason


def test_terminating_application_is_not_ready_despite_cached_healthy_status():
    snapshot = snapshot_fixture()
    snapshot["application"]["metadata"]["deletionTimestamp"] = NOW.isoformat()

    report = evaluate(snapshot)

    assert report["status"] == "not_ready"
    assert report["reason"] == "application_terminating"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda app: app["spec"]["source"].update(targetRevision="feature/untrusted"), "application_source_mismatch"),
        (lambda app: app["spec"]["source"].update(helm={"parameters": []}), "application_source_overrides"),
        (lambda app: app["status"]["sync"].update(revision="c" * 40), "gitops_revision_mismatch"),
        (
            lambda app: app["status"]["operationState"]["operation"]["sync"].update(revision="c" * 40),
            "operation_revision_mismatch",
        ),
        (
            lambda app: app["status"]["operationState"]["syncResult"].update(revision="c" * 40),
            "operation_result_revision_mismatch",
        ),
        (
            lambda app: app["status"]["operationState"]["operation"]["sync"].update(manifests=["local.yaml"]),
            "local_manifest_override",
        ),
    ],
)
def test_application_rejects_suspicious_or_mismatched_gitops_evidence(mutation, reason):
    snapshot = snapshot_fixture()
    mutation(snapshot["application"])

    report = evaluate(snapshot)

    assert report["status"] == "not_ready"
    assert report["reason"] == reason


def test_missing_application_operation_evidence_is_unknown():
    snapshot = snapshot_fixture()
    del snapshot["application"]["status"]["operationState"]

    report = evaluate(snapshot)

    assert report["status"] == "unknown"
    assert report["reason"] == "missing_or_malformed_application_operation_state"


@pytest.mark.parametrize(
    ("mutation", "status", "reason"),
    [
        (lambda rollout: rollout["status"].update(observedGeneration="2"), "not_ready", "rollout_generation_not_observed"),
        (lambda rollout: rollout["status"].update(stableRS="old-hash"), "not_ready", "stable_and_current_revision_differ"),
        (lambda rollout: rollout["status"].update(availableReplicas=0), "not_ready", "rollout_replicas_not_ready"),
        (lambda rollout: rollout["status"].update(abort=True), "not_ready", "rollout_aborted_git_recovery_required"),
        (lambda rollout: rollout["spec"]["template"]["spec"]["containers"][0].update(image="registry.example.com/other/app@sha256:" + IMAGE_DIGEST), "not_ready", "rollout_image_mismatch"),
    ],
)
def test_rollout_generation_health_image_and_abort_are_fail_closed(mutation, status, reason):
    snapshot = snapshot_fixture()
    mutation(snapshot["rollout"])

    report = evaluate(snapshot)

    assert report["status"] == status
    assert report["reason"] == reason
    if reason == "rollout_aborted_git_recovery_required":
        assert "recovery" in report["reason"]


def test_wrong_replicaset_owner_cannot_claim_a_rollout_pod():
    snapshot = snapshot_fixture()
    snapshot["replicasets"]["items"][0]["metadata"]["ownerReferences"][0]["uid"] = "another-rollout-uid"

    report = evaluate(snapshot)

    assert report["status"] == "unknown"
    assert report["reason"] == "missing_or_ambiguous_current_replicaset"


def test_wrong_pod_owner_cannot_be_counted_by_labels_or_image():
    snapshot = snapshot_fixture()
    pod = snapshot["pods"]["items"][0]
    pod["metadata"]["ownerReferences"][0]["uid"] = "another-replicaset-uid"
    pod["metadata"]["labels"] = {"rollouts-pod-template-hash": POD_HASH, "app": "data-pipeline-app"}

    report = evaluate(snapshot)

    assert report["status"] == "not_ready"
    assert report["reason"] == "insufficient_owned_ready_pods"


@pytest.mark.parametrize(
    ("mutation", "status", "reason"),
    [
        (lambda pod: pod["status"].update(phase="Pending"), "not_ready", "pod_not_ready"),
        (lambda pod: pod["status"]["containerStatuses"][0].update(imageID="containerd://sha256:" + "c" * 64), "not_ready", "runtime_image_digest_mismatch"),
        (lambda pod: pod["status"]["containerStatuses"][0].update(imageID="not-an-image-id"), "unknown", "missing_or_malformed_runtime_image_id"),
        (lambda pod: pod["status"].pop("conditions"), "unknown", "missing_pod_conditions"),
    ],
)
def test_owned_pod_readiness_and_runtime_image_id_are_verified(mutation, status, reason):
    snapshot = snapshot_fixture()
    mutation(snapshot["pods"]["items"][0])

    report = evaluate(snapshot)

    assert report["status"] == status
    assert report["reason"] == reason


def test_duplicate_pod_uid_and_duplicate_container_statuses_do_not_add_ready_replicas():
    snapshot = snapshot_fixture()
    snapshot["rollout"]["spec"]["replicas"] = 2
    for field in ("readyReplicas", "availableReplicas", "updatedReplicas"):
        snapshot["rollout"]["status"][field] = 2
    snapshot["pods"]["items"].append(copy.deepcopy(snapshot["pods"]["items"][0]))

    duplicate_pod = evaluate(snapshot)
    assert duplicate_pod["status"] == "unknown"
    assert duplicate_pod["reason"] == "missing_or_duplicate_pod_uid"

    snapshot = snapshot_fixture()
    snapshot["pods"]["items"][0]["status"]["containerStatuses"].append(
        copy.deepcopy(snapshot["pods"]["items"][0]["status"]["containerStatuses"][0])
    )
    duplicate_status = evaluate(snapshot)
    assert duplicate_status["status"] == "unknown"
    assert duplicate_status["reason"] == "container_status_mismatch"


def test_terminating_pods_are_excluded_from_the_desired_ready_count():
    snapshot = snapshot_fixture()
    snapshot["rollout"]["spec"]["replicas"] = 2
    for field in ("readyReplicas", "availableReplicas", "updatedReplicas"):
        snapshot["rollout"]["status"][field] = 2
    first = snapshot["pods"]["items"][0]
    second = copy.deepcopy(first)
    second["metadata"].update(name="data-pipeline-pod-2", uid="pod-uid-2")
    terminating = copy.deepcopy(first)
    terminating["metadata"].update(name="data-pipeline-pod-terminating", uid="pod-uid-terminating",
                                   deletionTimestamp=NOW.isoformat())
    snapshot["pods"]["items"] = [first, second, terminating]

    report = evaluate(snapshot)

    assert report["status"] == "verified"
    assert report["ready_pods"] == 2


def test_terminating_pod_is_not_counted_when_active_ready_pods_are_insufficient():
    snapshot = snapshot_fixture()
    snapshot["rollout"]["spec"]["replicas"] = 2
    for field in ("readyReplicas", "availableReplicas", "updatedReplicas"):
        snapshot["rollout"]["status"][field] = 2
    terminating = copy.deepcopy(snapshot["pods"]["items"][0])
    terminating["metadata"].update(name="data-pipeline-pod-terminating", uid="pod-uid-terminating",
                                   deletionTimestamp=NOW.isoformat())
    snapshot["pods"]["items"].append(terminating)

    report = evaluate(snapshot)

    assert report["status"] == "not_ready"
    assert report["reason"] == "insufficient_owned_ready_pods"


@pytest.mark.parametrize("resource", ["replicasets", "pods"])
def test_paginated_kubernetes_list_is_unknown(resource):
    snapshot = snapshot_fixture()
    snapshot[resource]["metadata"]["continue"] = "next-page-token"

    report = evaluate(snapshot)

    assert report["status"] == "unknown"
    assert report["reason"] == f"incomplete_{resource}_list"


@pytest.mark.parametrize("missing", ["identity", "permissions", "application", "rollout", "replicasets", "pods", "collected_at"])
def test_missing_snapshot_observation_is_unknown(missing):
    snapshot = snapshot_fixture()
    del snapshot[missing]

    report = evaluate(snapshot)

    assert report["status"] == "unknown"
