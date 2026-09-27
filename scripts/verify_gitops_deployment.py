#!/usr/bin/env python3
"""Read-only, fail-closed verification of a validation deployment's control plane.

Snapshots contain collected_at, identity (SelfSubjectReview), permissions (CHECKS
plus allowed booleans), application, rollout, replicasets (List), and pods (List).
An offline snapshot is evidence supplied by its caller, not proof of a live run.
This module does not verify PR approval, HTTP traffic, migrations, or DB parity.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import subprocess
import sys


NAMESPACE = "platform-validation"
APPLICATION = "data-pipeline-validation"
ROLLOUT = "data-pipeline-rollout"
REPOSITORY = "https://github.com/masondev1024/aws-data-platform-gitops"
SCOPE = "control_plane_only"
CHECKS = [
    {"verb": "get", "resource": "pods", "expected": True},
    {"verb": "list", "resource": "pods", "expected": True},
    {"verb": "list", "resource": "replicasets.apps", "expected": True},
    {"verb": "get", "resource": "rollouts.argoproj.io", "expected": True},
    {"verb": "get", "resource": "secrets", "expected": False},
    {"verb": "get", "resource": "secrets/raffle-secret", "expected": False},
    {"verb": "create", "resource": "pods", "expected": False},
    {"verb": "create", "resource": "pods", "subresource": "exec", "expected": False},
    {"verb": "patch", "resource": "rollouts.argoproj.io", "expected": False},
    {"verb": "patch", "resource": f"rollouts.argoproj.io/{ROLLOUT}", "expected": False},
    {"verb": "create", "resource": "rolebindings.rbac.authorization.k8s.io", "expected": False},
    {"namespace": "argocd", "verb": "get", "resource": f"applications.argoproj.io/{APPLICATION}", "expected": True},
    {"namespace": "argocd", "verb": "list", "resource": "applications.argoproj.io", "expected": False},
    {"namespace": "argocd", "verb": "watch", "resource": "applications.argoproj.io", "expected": False},
    {"namespace": "argocd", "verb": "get", "resource": "applications.argoproj.io/another-application", "expected": False},
    {"namespace": "argocd", "verb": "patch", "resource": f"applications.argoproj.io/{APPLICATION}", "expected": False},
    {"namespace": "argocd", "verb": "update", "resource": f"applications.argoproj.io/{APPLICATION}", "expected": False},
    {"namespace": "argocd", "verb": "create", "resource": "applications.argoproj.io", "expected": False},
    {"namespace": "argocd", "verb": "get", "resource": "secrets", "expected": False},
]
IMAGE_PATTERN = re.compile(
    r"[a-z0-9][a-z0-9.:-]*/[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}"
)
RUNTIME_IMAGE_PATTERN = re.compile(
    r"(?:[a-z0-9+.-]+://)?(?:[^@\s]+@)?sha256:([0-9a-f]{64})"
)


class ObservationError(Exception):
    """Only constant, non-secret diagnostic messages may be supplied."""

    def __init__(self, reason: str, status: str = "unknown"):
        super().__init__(reason)
        self.status = status


def require(condition: bool, reason: str, *, mismatch: bool = False) -> None:
    if not condition:
        raise ObservationError(reason, "not_ready" if mismatch else "unknown")


def mapping(value, name: str) -> dict:
    require(isinstance(value, dict), f"missing_or_malformed_{name}")
    return value


def integer(value, name: str) -> int:
    require(type(value) is int and value >= 0, f"missing_or_malformed_{name}")
    return value


def recent(value, now: datetime, name: str) -> None:
    require(isinstance(value, str), f"missing_or_malformed_{name}")
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(observed.tzinfo is not None and now.tzinfo is not None, f"missing_timezone_{name}")
        age = (now - observed).total_seconds()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ObservationError(f"missing_or_malformed_{name}") from exc
    require(-30 <= age <= 300, f"stale_or_future_{name}")


def validate_inputs(*, expected_revision, expected_image, expected_principal, application, namespace):
    require(isinstance(expected_revision, str) and
            re.fullmatch(r"[0-9a-f]{40}", expected_revision) is not None, "invalid_expected_revision")
    require(isinstance(expected_image, str) and IMAGE_PATTERN.fullmatch(expected_image) is not None,
            "invalid_expected_image")
    require(isinstance(expected_principal, str) and
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}", expected_principal) is not None,
            "invalid_expected_principal")
    require(application == APPLICATION and namespace == NAMESPACE, "unsupported_target")


def verify_identity_and_permissions(snapshot: dict, expected_principal: str) -> None:
    identity = mapping(snapshot.get("identity"), "identity")
    status = mapping(identity.get("status"), "identity_status")
    info = mapping(status.get("userInfo"), "identity_user")
    require(isinstance(info.get("username"), str), "missing_identity_username")
    require(info["username"] == expected_principal, "identity_mismatch", mismatch=True)
    permissions = snapshot.get("permissions")
    require(isinstance(permissions, list) and len(permissions) == len(CHECKS), "incomplete_permission_matrix")
    for expected, observed in zip(CHECKS, permissions):
        observed = mapping(observed, "permission")
        require(set(observed) == set(expected) | {"allowed"}, "malformed_permission")
        require(all(type(observed[key]) is type(value) and observed[key] == value
                    for key, value in expected.items()), "permission_query_mismatch")
        require(type(observed["allowed"]) is bool, "unknown_permission_result")
        require(observed["allowed"] == expected["expected"], "permission_policy_mismatch", mismatch=True)


def verify_application(app: dict, revision: str, namespace: str, now: datetime) -> None:
    metadata = mapping(app.get("metadata"), "application_metadata")
    require(metadata.get("name") == APPLICATION and metadata.get("namespace") == "argocd",
            "application_identity_mismatch", mismatch=True)
    require(not metadata.get("deletionTimestamp"), "application_terminating", mismatch=True)
    spec = mapping(app.get("spec"), "application_spec")
    require("sources" not in spec, "unsupported_multisource_application")
    source = mapping(spec.get("source"), "application_source")
    require(source.get("repoURL") in (REPOSITORY, REPOSITORY + ".git")
            and source.get("path") == "k8s/overlays/validation"
            and source.get("targetRevision") in ("main", revision), "application_source_mismatch", mismatch=True)
    # Kustomize/Helm/parameter overrides are not the approved Git tree.
    require(set(source) <= {"repoURL", "path", "targetRevision"}, "application_source_overrides", mismatch=True)
    destination = mapping(spec.get("destination"), "application_destination")
    require(destination.get("namespace") == namespace and
            destination.get("server") == "https://kubernetes.default.svc",
            "application_destination_mismatch", mismatch=True)
    status = mapping(app.get("status"), "application_status")
    recent(status.get("reconciledAt"), now, "argo_reconcile")
    conditions = status.get("conditions", [])
    require(isinstance(conditions, list) and all(isinstance(item, dict) for item in conditions),
            "malformed_application_conditions")
    require(not any(str(item.get("type", "")).endswith("Error") for item in conditions),
            "application_reconciliation_error", mismatch=True)
    sync = mapping(status.get("sync"), "application_sync")
    health = mapping(status.get("health"), "application_health")
    require(sync.get("revision") == revision, "gitops_revision_mismatch", mismatch=True)
    require(sync.get("status") == "Synced" and health.get("status") == "Healthy",
            "application_not_synced_healthy", mismatch=True)
    require(not app.get("operation"), "application_operation_in_progress", mismatch=True)
    operation = mapping(status.get("operationState"), "application_operation_state")
    require(operation.get("phase") == "Succeeded", "latest_sync_not_succeeded", mismatch=True)
    requested = mapping(operation.get("operation"), "application_operation")
    requested_sync = mapping(requested.get("sync"), "application_operation_sync")
    require(not requested_sync.get("manifests"), "local_manifest_override", mismatch=True)
    require(not requested_sync.get("sources") and not requested_sync.get("source"),
            "operation_source_override", mismatch=True)
    require(requested_sync.get("revision") in (None, "main", revision),
            "operation_revision_mismatch", mismatch=True)
    result = mapping(operation.get("syncResult"), "application_sync_result")
    require(result.get("revision") == revision, "operation_result_revision_mismatch", mismatch=True)


def named_image(spec: dict) -> str:
    containers = spec.get("containers")
    require(isinstance(containers, list) and containers, "missing_containers")
    require(all(isinstance(item, dict) and isinstance(item.get("name"), str) for item in containers),
            "malformed_containers")
    require(len({item["name"] for item in containers}) == len(containers), "duplicate_container_names")
    images = [item.get("image") for item in containers if item["name"] == "app-container"]
    require(len(images) == 1 and isinstance(images[0], str), "missing_app_container")
    return images[0]


def verify_rollout(rollout: dict, expected_image: str, namespace: str) -> tuple[str, str, int]:
    metadata = mapping(rollout.get("metadata"), "rollout_metadata")
    require(metadata.get("name") == ROLLOUT and metadata.get("namespace") == namespace,
            "rollout_identity_mismatch", mismatch=True)
    uid = metadata.get("uid")
    require(isinstance(uid, str) and bool(uid), "missing_rollout_uid")
    require(not metadata.get("deletionTimestamp"), "rollout_terminating", mismatch=True)
    generation = integer(metadata.get("generation"), "rollout_generation")
    spec = mapping(rollout.get("spec"), "rollout_spec")
    status = mapping(rollout.get("status"), "rollout_status")
    observed = status.get("observedGeneration")
    if isinstance(observed, str) and re.fullmatch(r"[0-9]+", observed):
        observed = int(observed)
    require(integer(observed, "observed_generation") == generation and generation > 0,
            "rollout_generation_not_observed", mismatch=True)
    require(status.get("abort") in (None, False) and type(status.get("abort")) in (bool, type(None)),
            "rollout_aborted_git_recovery_required", mismatch=True)
    require(status.get("phase") == "Healthy" and not status.get("pauseConditions") and not spec.get("paused"),
            "rollout_not_healthy", mismatch=True)
    desired = integer(spec.get("replicas"), "desired_replicas")
    require(desired > 0, "rollout_scaled_to_zero", mismatch=True)
    for key in ("readyReplicas", "availableReplicas", "updatedReplicas"):
        require(integer(status.get(key), key) >= desired, "rollout_replicas_not_ready", mismatch=True)
    pod_hash = status.get("currentPodHash")
    require(isinstance(pod_hash, str) and bool(pod_hash), "missing_current_pod_hash")
    require(status.get("stableRS") == pod_hash, "stable_and_current_revision_differ", mismatch=True)
    template = mapping(spec.get("template"), "rollout_template")
    require(named_image(mapping(template.get("spec"), "rollout_pod_spec")) == expected_image,
            "rollout_image_mismatch", mismatch=True)
    return uid, pod_hash, desired


def list_items(resource: dict, name: str) -> list[dict]:
    metadata = mapping(resource.get("metadata"), f"{name}_metadata")
    require(metadata.get("continue", "") == "", f"incomplete_{name}_list")
    items = resource.get("items")
    require(isinstance(items, list) and all(isinstance(item, dict) for item in items), f"malformed_{name}_list")
    return items


def controlled_by(metadata: dict, kind: str, name: str, uid: str) -> bool:
    owners = metadata.get("ownerReferences", [])
    require(isinstance(owners, list) and all(isinstance(owner, dict) for owner in owners), "malformed_owners")
    controllers = [owner for owner in owners if owner.get("controller") is True]
    require(len(controllers) <= 1, "ambiguous_controller_owner")
    return len(controllers) == 1 and all(controllers[0].get(key) == value
                                      for key, value in {"kind": kind, "name": name, "uid": uid}.items())


def verify_pods(snapshot: dict, rollout_uid: str, pod_hash: str, desired: int,
                expected_image: str, namespace: str) -> int:
    replicasets = list_items(mapping(snapshot.get("replicasets"), "replicasets"), "replicasets")
    owned = {}
    for rs in replicasets:
        metadata = mapping(rs.get("metadata"), "replicaset_metadata")
        if metadata.get("namespace") != namespace or not controlled_by(metadata, "Rollout", ROLLOUT, rollout_uid):
            continue
        labels = mapping(metadata.get("labels"), "replicaset_labels")
        if labels.get("rollouts-pod-template-hash") != pod_hash:
            continue
        require(not metadata.get("deletionTimestamp"), "current_replicaset_terminating", mismatch=True)
        uid, name = metadata.get("uid"), metadata.get("name")
        require(isinstance(uid, str) and uid and isinstance(name, str) and name, "malformed_replicaset_identity")
        require(uid not in owned, "duplicate_replicaset_uid")
        template = mapping(mapping(rs.get("spec"), "replicaset_spec").get("template"), "replicaset_template")
        require(named_image(mapping(template.get("spec"), "replicaset_pod_spec")) == expected_image,
                "replicaset_image_mismatch", mismatch=True)
        owned[uid] = name
    require(len(owned) == 1, "missing_or_ambiguous_current_replicaset")
    pods = list_items(mapping(snapshot.get("pods"), "pods"), "pods")
    verified = set()
    expected_digest = expected_image.rsplit("@", 1)[1]
    for pod in pods:
        metadata = mapping(pod.get("metadata"), "pod_metadata")
        if metadata.get("namespace") != namespace:
            continue
        if not any(controlled_by(metadata, "ReplicaSet", name, uid) for uid, name in owned.items()):
            continue
        if metadata.get("deletionTimestamp"):
            continue
        uid = metadata.get("uid")
        require(isinstance(uid, str) and uid and uid not in verified, "missing_or_duplicate_pod_uid")
        spec = mapping(pod.get("spec"), "pod_spec")
        require(named_image(spec) == expected_image, "pod_spec_image_mismatch", mismatch=True)
        status = mapping(pod.get("status"), "pod_status")
        conditions = status.get("conditions")
        require(isinstance(conditions, list) and all(isinstance(item, dict) for item in conditions),
                "missing_pod_conditions")
        ready = [item.get("status") for item in conditions if item.get("type") == "Ready"]
        require(status.get("phase") == "Running" and ready == ["True"], "pod_not_ready", mismatch=True)
        containers = status.get("containerStatuses")
        require(isinstance(containers, list) and all(isinstance(item, dict) for item in containers),
                "missing_container_statuses")
        names = [item.get("name") for item in containers]
        require(all(isinstance(name, str) for name in names) and len(set(names)) == len(names)
                and set(names) == {item["name"] for item in spec["containers"]}, "container_status_mismatch")
        require(all(item.get("ready") is True and isinstance(item.get("state"), dict)
                    and isinstance(item["state"].get("running"), dict) for item in containers),
                "container_not_running_ready", mismatch=True)
        app_status = next(item for item in containers if item["name"] == "app-container")
        image_id = app_status.get("imageID")
        matched = RUNTIME_IMAGE_PATTERN.fullmatch(image_id) if isinstance(image_id, str) else None
        require(matched is not None, "missing_or_malformed_runtime_image_id")
        require("sha256:" + matched.group(1) == expected_digest, "runtime_image_digest_mismatch", mismatch=True)
        verified.add(uid)
    require(len(verified) >= desired, "insufficient_owned_ready_pods", mismatch=True)
    return len(verified)


def evaluate_snapshot(snapshot: dict, *, expected_revision: str, expected_image: str,
                      expected_principal: str, application: str, namespace: str, now: datetime) -> dict:
    """Evaluate only the supplied control-plane observations, not their authenticity."""
    result = {"scope": SCOPE, "status": "unknown", "checks": [],
              "pr_approval_verified": False, "traffic_verified": False, "data_parity_verified": False}
    try:
        validate_inputs(expected_revision=expected_revision, expected_image=expected_image,
                        expected_principal=expected_principal, application=application, namespace=namespace)
        snapshot = mapping(snapshot, "snapshot")
        recent(snapshot.get("collected_at"), now, "snapshot")
        verify_identity_and_permissions(snapshot, expected_principal)
        result["checks"].append("identity_and_permission_matrix")
        verify_application(mapping(snapshot.get("application"), "application"), expected_revision, namespace, now)
        result["checks"].append("argo_git_revision")
        uid, pod_hash, desired = verify_rollout(mapping(snapshot.get("rollout"), "rollout"), expected_image, namespace)
        count = verify_pods(snapshot, uid, pod_hash, desired, expected_image, namespace)
        result.update(status="verified", gitops_revision=expected_revision,
                      image_digest=expected_image.rsplit("@", 1)[1], ready_pods=count,
                      permission_scope="declared_matrix_only")
        result["checks"].append("rollout_and_owned_pod_digests")
    except ObservationError as exc:
        result.update(status=exc.status, reason=str(exc))
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        result.update(status="unknown", reason="malformed_observation")
    return result


def query(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False, shell=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ObservationError("query_failed_or_timed_out") from exc


def parse_json(output: str) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "duplicate_json_key")
            value[key] = item
        return value

    try:
        value = json.loads(output, object_pairs_hook=unique)
    except (TypeError, ValueError) as exc:
        raise ObservationError("invalid_query_json") from exc
    return mapping(value, "query_result")


def command_plan(args) -> list[list[str]]:
    base = ["kubectl", "--context", args.context, "--namespace", args.namespace, "--request-timeout=10s"]
    commands = [["kubectl", "--context", args.context, "config", "view", "--minify", "-o", "json"],
                base + ["auth", "whoami", "-o", "json"]]
    for check in CHECKS:
        command = ["kubectl", "--context", args.context, "--namespace", check.get("namespace", args.namespace),
                   "--request-timeout=10s", "auth", "can-i", check["verb"], check["resource"]]
        if "subresource" in check:
            command += ["--subresource", check["subresource"]]
        commands.append(command)
    commands += [
        ["kubectl", "--context", args.context, "--namespace", "argocd", "--request-timeout=10s",
         "get", "applications.argoproj.io", args.application, "-o", "json"],
        base + ["get", "rollouts.argoproj.io", ROLLOUT, "-o", "json"],
        base + ["get", "replicasets.apps", "-o", "json"],
        base + ["get", "pods", "-o", "json"],
    ]
    return commands


def verify_context(config: dict, context: str) -> None:
    """Inspect redacted config in memory only; never store credentials or exec env."""
    contexts, clusters, users = (config.get(key) for key in ("contexts", "clusters", "users"))
    require(all(isinstance(items, list) and len(items) == 1 for items in (contexts, clusters, users)),
            "ambiguous_kube_context")
    selected = mapping(contexts[0], "kube_context")
    require(selected.get("name") == context, "kube_context_mismatch")
    selection = mapping(selected.get("context"), "kube_context_selection")
    cluster = mapping(clusters[0], "kube_cluster")
    user = mapping(users[0], "kube_user")
    require(selection.get("cluster") == cluster.get("name") and selection.get("user") == user.get("name"),
            "kube_context_binding_mismatch")
    connection = mapping(cluster.get("cluster"), "kube_connection")
    server = connection.get("server")
    require(isinstance(server, str) and server.startswith("https://")
            and connection.get("insecure-skip-tls-verify", False) is False, "insecure_kube_connection")
    auth = mapping(user.get("user"), "kube_auth")
    require(not any(key in auth for key in ("as", "as-uid", "as-groups", "as-user-extra")),
            "kubeconfig_impersonation_not_allowed")


def collect_snapshot(args) -> dict:
    commands = command_plan(args)

    def read_json(command):
        completed = query(command)
        require(completed.returncode == 0 and not completed.stderr.strip(), "resource_query_failed")
        return parse_json(completed.stdout)

    verify_context(read_json(commands[0]), args.context)
    identity = read_json(commands[1])
    info = mapping(mapping(identity.get("status"), "identity_status").get("userInfo"), "identity_user")
    require(info.get("username") == args.principal, "identity_mismatch", mismatch=True)
    permissions = []
    for check, command in zip(CHECKS, commands[2:2 + len(CHECKS)]):
        completed = query(command)
        answer = completed.stdout.strip()
        require(not completed.stderr.strip() and (completed.returncode, answer) in ((0, "yes"), (1, "no")),
                "permission_query_failed")
        allowed = answer == "yes"
        require(allowed == check["expected"], "permission_policy_mismatch", mismatch=True)
        permissions.append({**check, "allowed": allowed})
    snapshot = {"identity": identity, "permissions": permissions}
    for key, command in zip(("application", "rollout", "replicasets", "pods"), commands[-4:]):
        snapshot[key] = read_json(command)
    # Reads span several API requests, so detect a concurrent rollout/reconcile.
    # This is still a bounded observation, not a transactional cluster snapshot.
    for key, command in zip(("application", "rollout"), commands[-4:-2]):
        initial = mapping(snapshot[key].get("metadata"), f"{key}_metadata")
        final = mapping(read_json(command).get("metadata"), f"{key}_metadata")
        for field in ("uid", "resourceVersion"):
            require(isinstance(initial.get(field), str) and bool(initial[field]), "missing_resource_identity")
            require(initial[field] == final.get(field), "resources_changed_during_collection")
    snapshot["collected_at"] = datetime.now(timezone.utc).isoformat()
    return snapshot


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    for name in ("context", "namespace", "principal", "application", "expected-revision", "expected-image"):
        result.add_argument("--" + name, required=True)
    result.add_argument("--execute", action="store_true")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        validate_inputs(expected_revision=args.expected_revision, expected_image=args.expected_image,
                        expected_principal=args.principal, application=args.application, namespace=args.namespace)
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,511}", args.context) is not None, "invalid_context")
        if not args.execute:
            print(json.dumps({"scope": SCOPE, "mode": "dry_run", "status": "not_executed",
                              "commands": command_plan(args)}, sort_keys=True))
            return 0
        snapshot = collect_snapshot(args)
        result = evaluate_snapshot(snapshot, expected_revision=args.expected_revision,
                                   expected_image=args.expected_image, expected_principal=args.principal,
                                   application=args.application, namespace=args.namespace, now=datetime.now(timezone.utc))
        result.update(mode="live_read_only", collected_at=snapshot["collected_at"])
    except ObservationError as exc:
        result = {"scope": SCOPE, "status": exc.status, "reason": str(exc)}
    print(json.dumps(result, sort_keys=True))
    return {"verified": 0, "not_ready": 1, "unknown": 2}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
