#!/usr/bin/env python3
"""Bootstrap only governance controls, then authenticate ephemerally and verify.

Uses the pinned session-local CLI from <evidence parent>/bin/argocd. Credentials
never enter command arguments, files, or stdout. No application deployment.
"""
import base64
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

spec = importlib.util.spec_from_file_location("installer", Path(__file__).with_name("install_argocd_live.py"))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
g = installer.g


def main():
    a = g.parser().parse_args()
    g.validate_args(a)
    if not a.execute:
        print(json.dumps({"status": "plan_only", "bootstrap": "governance_only", "session": a.session}))
        return 0
    kube = ["kubectl", "--context", a.context, "--request-timeout=30s"]
    installer.validate_cluster(a, kube)
    namespace = g.document(g.run(kube + ["get", "ns", "argocd", "-o", "json"]))
    labels = namespace["metadata"].get("labels", {})
    g.require(labels.get("live-lab-session") == a.session and labels.get("live-lab-approval") == a.approval,
              "Argo namespace ownership mismatch")
    g.require(not Path(a.evidence).exists(), "evidence already exists")
    cli_dir = Path(a.evidence).resolve().parent / "bin"
    cli = cli_dir / "argocd"
    g.require(cli.is_file(), "pinned session argocd binary missing")
    version = g.run([str(cli), "version", "--client", "--short"]).stdout
    g.require("v3.5.3+c9c369e" in version, "unexpected Argo CLI version")
    g.run(kube + ["apply", "--server-side", "--field-manager=live-governance-bootstrap", "-k", str(g.GOV / "bootstrap")])
    g.run(kube + ["apply", "--server-side", "--field-manager=live-governance-bootstrap", "-f", str(g.GOV / "argocd/app-project.yaml")])
    secret = g.document(g.run(kube + ["-n", "argocd", "get", "secret", "argocd-initial-admin-secret", "-o", "json"]))
    password = base64.b64decode(secret["data"]["password"]).decode()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    forward = subprocess.Popen(kube + ["-n", "argocd", "port-forward", "--address=127.0.0.1",
                                       "service/argocd-server", f"{port}:443"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for attempt in range(60):
            g.require(forward.poll() is None, "Argo port-forward failed")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise g.CheckFailed("Argo port-forward timed out")
        request = urllib.request.Request(f"https://127.0.0.1:{port}/api/v1/session",
                    data=json.dumps({"username": "admin", "password": password}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
        # The authenticated EKS tunnel terminates at the exact session's ClusterIP service.
        with urllib.request.urlopen(request, context=ssl._create_unverified_context(), timeout=15) as result:
            token = json.load(result)["token"]
        env = dict(os.environ, ARGOCD_AUTH_TOKEN=token, PATH=str(cli_dir) + os.pathsep + os.environ["PATH"],
                   PYTHONDONTWRITEBYTECODE="1")
        response = subprocess.run([sys.executable, str(g.ROOT / "platform/live-lab/scripts/verify_governance_live.py"),
                                   *sys.argv[1:]], env=env, text=True, capture_output=True, timeout=1200)
        # Only the verifier's fixed JSON summary is emitted, never authentication responses.
        summary = json.loads(response.stdout)
        print(json.dumps({"status": summary["status"], "evidence": a.evidence}))
        return response.returncode
    finally:
        forward.terminate()
        try:
            forward.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forward.kill()
            forward.wait()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc) if isinstance(exc, g.CheckFailed) else "session_execution_error"}))
        raise SystemExit(1)
