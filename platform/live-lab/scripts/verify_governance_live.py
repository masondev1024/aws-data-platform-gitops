#!/usr/bin/env python3
"""Session-bound Argo CD, developer RBAC and quota checks; offline by default.

No bootstrap, Git push, global policy edits or workload deployment. The positive
control is a disposable ConfigMap synchronized through the real Argo CD server.
Only --execute contacts AWS/Kubernetes. CLI output is never copied to evidence.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[3]
GOV = ROOT / "platform/governance"
NAMESPACE = "platform-validation"
PROJECT = "kyobo-platform-validation"
REPO = "https://github.com/masondev1024/aws-data-platform-gitops"
SERVER = "https://kubernetes.default.svc"
PRINCIPAL = f"system:serviceaccount:{NAMESPACE}:kyobo-developer-readonly"


class CheckFailed(RuntimeError):
    """Only fixed, non-sensitive diagnostics may be included in this exception."""


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


def run(argv, payload=None, check=True):
    # Never echo arguments, stderr or payloads: provider/Argo errors can contain credentials.
    env = dict(os.environ)
    for key in ("ARGOCD_OPTS", "ARGOCD_SERVER", "ARGOCD_CONTEXT", "ARGOCD_CORE"):
        env.pop(key, None)
    env["AWS_PAGER"] = ""
    try:
        result = subprocess.run(argv, input=payload, text=True, capture_output=True,
                                timeout=240, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckFailed("command unavailable or timed out; result is unknown") from exc
    if check:
        require(result.returncode == 0, "command failed; result is not an expected denial")
    return result


def document(result):
    try:
        value = json.loads(result.stdout)
        require(isinstance(value, dict), "invalid JSON response shape")
        return value
    except (ValueError, TypeError) as exc:
        raise CheckFailed("invalid JSON response") from exc


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("account", "region", "session", "approval", "cluster", "context", "evidence"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--presync-image-file", help="Opt in to a disposable PreSync missing-DB failure gate using this session image-ref file")
    return p


def validate_args(a):
    require(bool(re.fullmatch(r"[0-9]{12}", a.account)), "invalid expected account")
    require(bool(re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", a.region)), "invalid region")
    require(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{5,40}", a.session)), "invalid session")
    require(bool(re.fullmatch(r"SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}", a.approval)), "invalid approval")
    require(a.cluster == f"kyobo-{a.session}", "cluster must match the session Terraform name")
    require(bool(a.context) and not a.context.startswith("-"), "invalid kube context")


class Governance:
    def __init__(self, args, report):
        self.a, self.report = args, report
        self.kube = ["kubectl", "--context", args.context, "--request-timeout=20s"]
        self.cli_directory = tempfile.TemporaryDirectory(prefix="governance-argocd-")
        cli_config = Path(self.cli_directory.name) / "config"
        # Nonexistent config in a private directory: /dev/null fails mode checks,
        # while an existing empty YAML requires a current-context in Argo v3.5.
        # Port-forward resolves the actual server through the already-validated EKS context.
        # TLS bypass applies ONLY to this authenticated Kubernetes tunnel, not a remote URL.
        self.argo = ["argocd", "--config", str(cli_config), "--port-forward", "--port-forward-namespace", "argocd",
                     "--kube-context", args.context, "--insecure"]
        self.run_id = f"gov-{args.session}-{uuid.uuid4().hex[:8]}"
        self.names = [self.run_id + suffix for suffix in ("", "-repo", "-dest", "-gate")]
        self.labels = {"live-lab-session": args.session, "live-lab-approval": args.approval,
                       "live-lab-governance-run": self.run_id}
        self.report["run_id"] = self.run_id

    def aws(self, *args):
        return document(run(["aws", "--region", self.a.region, "--output", "json", *args]))

    def get(self, kind, namespace, name):
        return document(run(self.kube + ["-n", namespace, "get", kind, name, "-o", "json"]))

    def preflight(self):
        require(bool(os.environ.get("ARGOCD_AUTH_TOKEN")), "ARGOCD_AUTH_TOKEN is required for the real Argo server")
        require(self.aws("sts", "get-caller-identity").get("Account") == self.a.account,
                "AWS caller account mismatch")
        cluster = self.aws("eks", "describe-cluster", "--name", self.a.cluster)["cluster"]
        expected_arn = f"arn:aws:eks:{self.a.region}:{self.a.account}:cluster/{self.a.cluster}"
        require(cluster.get("arn") == expected_arn and cluster.get("status") == "ACTIVE",
                "EKS ARN/status mismatch")
        tags = cluster.get("tags", {})
        require(all(tags.get(k) == v for k, v in {
            "Project": "kyobo-platform-live-lab", "Session": self.a.session,
            "Approval": self.a.approval}.items()), "EKS ownership mismatch")
        config = document(run(self.kube + ["config", "view", "--minify", "--flatten", "--raw", "-o", "json"]))
        entries = config.get("clusters", [])
        require(len(entries) == 1, "ambiguous kube context")
        connection = entries[0]["cluster"]
        require(connection.get("server") == cluster.get("endpoint") and
                connection.get("certificate-authority-data") == cluster["certificateAuthority"]["data"] and
                not connection.get("insecure-skip-tls-verify") and not connection.get("proxy-url"),
                "kube context does not securely match the expected EKS endpoint")
        expected = yaml.safe_load((GOV / "argocd/app-project.yaml").read_text())["spec"]
        actual = self.get("appproject", "argocd", PROJECT)["spec"]
        for field in ("sourceRepos", "destinations", "namespaceResourceWhitelist", "namespaceResourceBlacklist"):
            require(actual.get(field) == expected[field], "live AppProject boundary drift")
        require(not actual.get("clusterResourceWhitelist"), "cluster resources unexpectedly allowed")
        require(not actual.get("sourceNamespaces"), "unexpected cross-namespace applications")
        # Confirm the real Argo API sees the same project; --core is never used.
        argo_project = document(run(self.argo + ["proj", "get", PROJECT, "-o", "json"]))
        require(argo_project["metadata"]["uid"] == self.get("appproject", "argocd", PROJECT)["metadata"]["uid"],
                "Argo server is not observing the expected cluster project")
        quota = self.get("resourcequota", NAMESPACE, "platform-validation-quota")
        require(quota["spec"]["hard"].get("requests.cpu") == "2", "unexpected quota CPU contract")
        self.binding = self.get("rolebinding", NAMESPACE, "kyobo-developer-readonly")
        self.report["checks"].append({"check": "scope_and_live_project", "status": "passed"})

    def app_manifest(self, name, repo=REPO, namespace=NAMESPACE):
        return {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
                "metadata": {"name": name, "namespace": "argocd", "labels": self.labels},
                "spec": {"project": PROJECT,
                         "source": {"repoURL": repo, "targetRevision": "main",
                                    "path": "platform/governance/fixtures/live-positive"},
                         "destination": {"server": SERVER, "namespace": namespace},
                         "syncPolicy": {"syncOptions": ["CreateNamespace=false"]}}}

    def create_app(self, manifest, directory):
        path = Path(directory) / "application.json"
        path.write_text(json.dumps(manifest))
        # Skip remote manifest generation for an unpushed local validation slice;
        # Argo server still calls ValidatePermissions for repo/destination policies.
        return run(self.argo + ["app", "create", "--file", str(path), "--validate=false"], check=False)

    def positive_and_denials(self, directory):
        result = self.create_app(self.app_manifest(self.run_id), directory)
        require(result.returncode == 0, "positive Application creation failed")
        local = Path(directory) / "positive"
        local.mkdir()
        manifest = yaml.safe_load((GOV / "fixtures/live-positive/configmap.yaml").read_text())
        manifest["metadata"] = {"name": self.run_id, "namespace": NAMESPACE, "labels": self.labels}
        (local / "resource.json").write_text(json.dumps(manifest))
        run(self.argo + ["app", "sync", self.run_id, "--local", str(local), "--timeout", "120"])
        state = document(run(self.argo + ["app", "get", self.run_id, "-o", "json"]))
        operation = state.get("status", {}).get("operationState", {})
        require(operation.get("phase") == "Succeeded", "positive Argo sync did not succeed")
        cm = self.get("configmap", NAMESPACE, self.run_id)
        require(cm.get("data") == manifest["data"] and self.owned(cm), "positive sync not observed in cluster")
        self.report["checks"].append({"check": "argocd_local_configmap_sync", "status": "passed",
                                      "operation_phase": "Succeeded"})
        for suffix, repo, namespace, pattern in (
            ("-repo", "https://example.invalid/forbidden-governance.git", NAMESPACE,
             r"application repo https://example\.invalid/forbidden-governance\.git is not permitted in project '?kyobo-platform-validation'?"),
            ("-dest", REPO, "default",
             r"application destination .*default.*(do not match any of the allowed destinations|not permitted).*")):
            result = self.create_app(self.app_manifest(self.run_id + suffix, repo, namespace), directory)
            require_denial(result, pattern)
            self.report["checks"].append({"check": "argocd" + suffix + "_rejected", "status": "passed"})
        forbidden = Path(directory) / "forbidden"
        forbidden.mkdir()
        rb = yaml.safe_load((GOV / "fixtures/live-forbidden/rolebinding.yaml").read_text())
        rb["metadata"] = {"name": self.run_id, "namespace": NAMESPACE, "labels": self.labels}
        (forbidden / "resource.json").write_text(json.dumps(rb))
        result = run(self.argo + ["app", "sync", self.run_id, "--local", str(forbidden),
                                 "--timeout", "120"], check=False)
        # A CLI timeout, repo failure, Kubernetes Forbidden or missing override privilege
        # cannot substitute for this specific AppProject denial.
        require_denial(result, r"resource rbac\.authorization\.k8s\.io:RoleBinding is not permitted in project '?kyobo-platform-validation'?")
        objects = document(run(self.kube + ["-n", NAMESPACE, "get", "rolebindings", "-o", "json"]))
        require(all(x["metadata"]["name"] != self.run_id for x in objects["items"]),
                "forbidden RoleBinding exists despite rejected sync")
        current = self.get("rolebinding", NAMESPACE, "kyobo-developer-readonly")
        require(all(current.get(k) == self.binding.get(k) for k in ("subjects", "roleRef")),
                "developer binding changed during verification")
        self.report["checks"].append({"check": "argocd_rolebinding_rejected", "status": "passed"})

    def rbac(self):
        base = self.kube + ["-n", NAMESPACE, "--as", PRINCIPAL,
                           "--as-group=system:authenticated", "--as-group=system:serviceaccounts",
                           f"--as-group=system:serviceaccounts:{NAMESPACE}"]
        identity = document(run(base + ["auth", "whoami", "-o", "json"]))
        require(identity["status"]["userInfo"]["username"] == PRINCIPAL, "impersonation identity mismatch")
        for verb, resource, subresource, allowed in (
            ("get", "pods", None, True), ("list", "configmaps", None, True),
            ("get", "secrets", None, False), ("list", "secrets", None, False),
            ("create", "pods", "exec", False), ("create", "pods", None, False),
            ("create", "rolebindings.rbac.authorization.k8s.io", None, False),
            ("patch", "rolebindings.rbac.authorization.k8s.io", None, False),
            ("create", "serviceaccounts", None, False),
            ("create", "roles.rbac.authorization.k8s.io", None, False),
            ("bind", "roles.rbac.authorization.k8s.io/kyobo-developer-readonly", None, False)):
            cmd = base + ["auth", "can-i", verb, resource]
            if subresource:
                cmd += ["--subresource", subresource]
            response = run(cmd, check=False)
            require(response.returncode == (0 if allowed else 1) and
                    response.stdout.strip() == ("yes" if allowed else "no") and not response.stderr.strip(),
                    "RBAC query error or unexpected privilege")
        response = run(self.kube + ["--as", PRINCIPAL, "--as-group=system:authenticated",
                       "--as-group=system:serviceaccounts", f"--as-group=system:serviceaccounts:{NAMESPACE}",
                       "auth", "can-i", "bind", "clusterrole/admin", "--all-namespaces"], check=False)
        require(response.returncode == 1 and response.stdout.strip() == "no" and not response.stderr.strip(),
                "cluster role bind query error or unexpected privilege")
        # Exercise a real read through the impersonated identity as the positive control.
        document(run(base + ["get", "configmaps", "-o", "json"]))
        self.report["checks"].append({"check": "developer_read_secrets_exec_create_bind", "status": "passed",
                                      "method": "impersonated_authorization_and_actual_read"})

    def quota(self):
        pod = quota_pod(self.run_id, 1, "1m")
        result = run(self.kube + ["-n", NAMESPACE, "create", "--dry-run=server", "-f", "-", "-o", "json"],
                     json.dumps(pod), check=False)
        require(result.returncode == 0, "quota positive admission control failed")
        pod = quota_pod(self.run_id, 5, "1")
        result = run(self.kube + ["-n", NAMESPACE, "create", "--dry-run=server", "-f", "-", "-o", "json"],
                     json.dumps(pod), check=False)
        require_denial(result, r"exceeded quota: platform-validation-quota,.*requests\.cpu")
        self.report["checks"].append({"check": "quota_cpu_admission_rejected", "status": "passed",
                                      "method": "server_dry_run_no_pod_created"})

    def owned(self, obj):
        return all(obj.get("metadata", {}).get("labels", {}).get(k) == v for k, v in self.labels.items())

    def presync_failure_gate(self, directory):
        image = Path(self.a.presync_image_file).read_text().strip()
        expected = f"{self.a.account}.dkr.ecr.{self.a.region}.amazonaws.com/kyobo-{self.a.session}/data-pipeline-app@sha256:"
        require(image.startswith(expected) and bool(re.fullmatch(r"[0-9a-f]{64}", image[len(expected):])),
                "PreSync image must be the exact session ECR digest")
        name = self.run_id + "-gate"
        result = self.create_app(self.app_manifest(name), directory)
        require(result.returncode == 0, "PreSync test Application creation failed")
        local = Path(directory) / "presync"
        local.mkdir()
        job = yaml.safe_load((GOV / "fixtures/live-presync-failure/job.yaml").read_text())
        job["metadata"].update(name=name, labels=self.labels)
        job["spec"]["template"]["metadata"] = {"labels": self.labels}
        job["spec"]["template"]["spec"]["containers"][0]["image"] = image
        cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
            "name": name, "namespace": NAMESPACE, "labels": self.labels},
            "data": {"must-not-apply": "PreSync failure blocks this normal sync resource"}}
        (local / "job.json").write_text(json.dumps(job))
        (local / "configmap.json").write_text(json.dumps(cm))
        result = run(self.argo + ["app", "sync", name, "--local", str(local), "--timeout", "150"], check=False)
        require(result.returncode != 0, "PreSync failure unexpectedly allowed sync")
        app = document(run(self.argo + ["app", "get", name, "-o", "json"]))
        operation = app.get("status", {}).get("operationState", {})
        require(operation.get("phase") == "Failed", "PreSync operation failure not proven")
        hooks = [r for r in operation.get("syncResult", {}).get("resources", [])
                 if r.get("kind") == "Job" and r.get("name") == name and r.get("hookType") == "PreSync"]
        require(len(hooks) == 1 and hooks[0].get("hookPhase") == "Failed", "failed PreSync hook not proven")
        live_job = self.get("job", NAMESPACE, name)
        require(self.owned(live_job) and any(c.get("type") == "Failed" and c.get("status") == "True"
                                            for c in live_job.get("status", {}).get("conditions", [])),
                "migration Job failure not observed")
        maps = document(run(self.kube + ["-n", NAMESPACE, "get", "configmaps", "-o", "json"]))["items"]
        require(all(c["metadata"]["name"] != name for c in maps), "normal ConfigMap applied despite failed PreSync")
        pods = document(run(self.kube + ["-n", NAMESPACE, "get", "pods", "-l", f"job-name={name}", "-o", "json"]))["items"]
        require(len(pods) == 1 and self.owned(pods[0]), "unexpected PreSync Pod ownership/count")
        statuses = pods[0].get("status", {}).get("containerStatuses", [])
        require(len(statuses) == 1 and statuses[0].get("state", {}).get("terminated", {}).get("exitCode") == 1,
                "migration process did not fail with the expected exit code")
        logs = run(self.kube + ["-n", NAMESPACE, "logs", f"job/{name}", "-c", "migration", "--tail=50"]).stdout
        expected_log = ("RuntimeError: DB_WRITER_HOST, DB_ADMIN_PASSWORD, DB_APP_USER, and DB_APP_PASSWORD "
                        "must be configured for migrations")
        require(expected_log in logs, "migration failure was not the intended missing-DB guard")
        self.report["checks"].append({"check": "presync_migration_failure_blocks_sync", "status": "passed",
            "image": image, "operation_phase": "Failed", "hook_phase": "Failed", "job_exit_code": 1,
            "normal_configmap_present": False, "hook_log_excerpt": expected_log,
            "hook_log_sha256": hashlib.sha256(logs.encode()).hexdigest(),
            "limitation": "Proves PreSync sync gating only; no database rollback or existing application health claim"})

    def cleanup(self):
        # Discover uncertain create outcomes as well as successful ones. Never adopt/delete
        # pre-existing objects; both the unique run label and exact name must match.
        errors = []
        for kind, namespace, names in (("applications", "argocd", self.names),
                                       ("jobs", NAMESPACE, [self.run_id + "-gate"]),
                                       ("configmaps", NAMESPACE, [self.run_id, self.run_id + "-gate"]),
                                       ("rolebindings", NAMESPACE, [self.run_id])):
            try:
                objects = document(run(self.kube + ["-n", namespace, "get", kind,
                    "-l", f"live-lab-governance-run={self.run_id}", "-o", "json"]))["items"]
                for obj in objects:
                    name = obj["metadata"]["name"]
                    require(name in names and self.owned(obj), "cleanup ownership mismatch")
                    if kind == "applications":
                        require(obj["spec"].get("project") == PROJECT and
                                not obj["spec"].get("syncPolicy", {}).get("automated"), "unsafe cleanup Application")
                        if obj.get("operation"):
                            run(self.argo + ["app", "terminate-op", name])
                        run(self.argo + ["app", "delete", name, "--cascade=false", "--yes"])
                    else:
                        run(self.kube + ["-n", namespace, "delete", kind, name, "--wait=true", "--timeout=30s"])
                remaining = document(run(self.kube + ["-n", namespace, "get", kind,
                    "-l", f"live-lab-governance-run={self.run_id}", "-o", "json"]))["items"]
                require(not remaining, "cleanup left test resources")
            except Exception:
                errors.append(kind)
        try:
            pods = document(run(self.kube + ["-n", NAMESPACE, "get", "pods", "-l",
                f"live-lab-governance-run={self.run_id}", "-o", "json"]))["items"]
            for pod in pods:
                require(self.owned(pod) and any(o.get("kind") == "Job" and o.get("name") == self.run_id + "-gate"
                                              for o in pod["metadata"].get("ownerReferences", [])),
                        "cleanup Pod ownership mismatch")
                run(self.kube + ["-n", NAMESPACE, "wait", "--for=delete", "pod/" + pod["metadata"]["name"], "--timeout=60s"])
        except Exception:
            errors.append("pods")
        self.report["cleanup"] = {"status": "failed" if errors else "passed", "unresolved_kinds": errors}
        require(not errors, "cleanup incomplete; use run_id to inspect residual test objects")


def require_denial(result, pattern):
    text = result.stdout + "\n" + result.stderr
    require(result.returncode != 0 and bool(re.search(pattern, text, re.IGNORECASE)),
            "expected policy denial not proven")


def quota_pod(name, count, cpu):
    container = {"image": "registry.k8s.io/pause:3.10", "resources": {
        "requests": {"cpu": cpu, "memory": "1Mi"}, "limits": {"cpu": "1", "memory": "1Mi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NAMESPACE},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 65534,
                                         "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [dict(copy.deepcopy(container), name=f"probe-{i}") for i in range(count)]}}


def interrupted(signum, frame):
    raise CheckFailed("interrupted; recovery attempted")


def main(argv=None):
    args = parser().parse_args(argv)
    report = {"schema_version": 1, "status": "failed", "checks": [],
              "observed_at": datetime.now(timezone.utc).isoformat()}
    try:
        validate_args(args)
        report["scope"] = {key: getattr(args, key) for key in ("account", "region", "session", "approval", "cluster")}
        if not args.execute:
            print(json.dumps({"status": "plan_only", "session": args.session,
                              "checks": ["scope", "argocd_local_sync", "repo_destination_rolebinding_denials",
                                         "developer_rbac", "quota_admission", "cleanup"]}))
            return 0
        # Reserve evidence before any mutation, refusing overwrite/symlink targets.
        fd = os.open(args.evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as evidence:
            try:
                signal.signal(signal.SIGTERM, interrupted)
                signal.signal(signal.SIGINT, interrupted)
                verifier = Governance(args, report)
                try:
                    verifier.preflight()
                    with tempfile.TemporaryDirectory(prefix="governance-live-") as directory:
                        try:
                            verifier.rbac()
                            verifier.quota()
                            verifier.positive_and_denials(directory)
                            if args.presync_image_file:
                                verifier.presync_failure_gate(directory)
                        finally:
                            verifier.cleanup()
                finally:
                    verifier.cli_directory.cleanup()
                report["status"] = "passed"
            except Exception as exc:
                report["error"] = str(exc) if isinstance(exc, CheckFailed) else "unexpected response or execution failure"
            finally:
                json.dump(report, evidence, indent=2)
                evidence.write("\n")
        print(json.dumps({"status": report["status"], "evidence": args.evidence}))
        return 0 if report["status"] == "passed" else 1
    except Exception:
        print(json.dumps({"status": "failed", "error": "invalid scope or evidence destination"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
