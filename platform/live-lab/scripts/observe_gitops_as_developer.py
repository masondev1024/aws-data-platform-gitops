#!/usr/bin/env python3
"""Issue a short-lived scoped credential, then run the read-only delivery check.

Credential issuance needs administrator authority. The collector authenticates as
the real ServiceAccount, without impersonation; it never uses the administrator
context for its observations. Tokens stay in a private temporary kubeconfig.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "delivery_installer", ROOT / "platform/governance/scripts/install_argocd_live.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
g = installer.g


def restricted_config(cluster, token):
    g.require(isinstance(token, str) and len(token.split(".")) == 3, "invalid service account credential")
    return {"apiVersion": "v1", "kind": "Config",
            "clusters": [{"name": "session", "cluster": {
                "server": cluster["server"], "certificate-authority-data": cluster["certificate-authority-data"]}}],
            "users": [{"name": "developer", "user": {"token": token}}],
            "contexts": [{"name": "delivery-readonly", "context": {
                "cluster": "session", "user": "developer", "namespace": g.NAMESPACE}}],
            "current-context": "delivery-readonly"}


def main():
    parser = g.parser()
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-image", required=True)
    a = parser.parse_args()
    g.validate_args(a)
    if not a.execute:
        print(json.dumps({"status": "plan_only", "credential_issued": False,
                          "principal": g.PRINCIPAL, "scope": "control_plane_only"}))
        return 0
    g.require(bool(os.environ.get("AWS_PROFILE")), "explicit AWS_PROFILE required")
    kube = ["kubectl", "--context", a.context, "--request-timeout=30s"]
    installer.validate_cluster(a, kube)
    cfg = g.document(g.run(kube + ["config", "view", "--raw", "--flatten", "--minify", "-o", "json"]))
    cluster = cfg["clusters"][0]["cluster"]
    descriptor = os.open(a.evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    report = {"status": "unknown", "reason": "observation_not_completed", "scope": "control_plane_only"}
    code = 2
    with os.fdopen(descriptor, "w") as evidence:
        try:
            token = g.run(kube + ["-n", g.NAMESPACE, "create", "token", "kyobo-developer-readonly", "--duration=10m"]).stdout.strip()
            with tempfile.TemporaryDirectory(prefix="delivery-readonly-") as directory:
                path = Path(directory) / "config"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as config:
                    yaml.safe_dump(restricted_config(cluster, token), config)
                command = [sys.executable, str(ROOT / "scripts/verify_gitops_deployment.py"),
                           "--context", "delivery-readonly", "--namespace", g.NAMESPACE,
                           "--principal", g.PRINCIPAL, "--application", "data-pipeline-validation",
                           "--expected-revision", a.expected_revision, "--expected-image", a.expected_image, "--execute"]
                result = subprocess.run(command, text=True, capture_output=True, timeout=300,
                                        env=dict(os.environ, KUBECONFIG=str(path)))
                report = json.loads(result.stdout)
                g.require(report.get("scope") == "control_plane_only" and
                          report.get("status") in {"verified", "not_ready", "unknown"}, "invalid observation report")
                code = result.returncode
        finally:
            json.dump(report, evidence, indent=2)
            evidence.write("\n")
    print(json.dumps({"status": report["status"], "evidence": a.evidence,
                      "scope": "control_plane_only", "credential_method": "real_short_lived_service_account"}))
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "unknown", "error": str(exc) if isinstance(exc, g.CheckFailed) else "observation_error"}))
        raise SystemExit(2)
