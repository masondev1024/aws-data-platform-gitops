#!/usr/bin/env python3
"""Install pinned, resource-bounded Argo CD only on the explicit approved EKS.

Requires the same scope arguments as verify_governance_live.py; --evidence is a
new installation evidence JSON path. No application workload is installed.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("governance", ROOT / "platform/live-lab/scripts/verify_governance_live.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
VERSION = "v3.5.3"
URL = f"https://raw.githubusercontent.com/argoproj/argo-cd/{VERSION}/manifests/install.yaml"
SHA256 = "7efe2d6bbc03f63623640f1e4198f16c84009d510fb810ef71e56df1b7614ba9"
OPTIONAL = {"argocd-dex-server", "argocd-applicationset-controller", "argocd-notifications-controller"}
CLUSTER_KINDS = {"CustomResourceDefinition", "ClusterRole", "ClusterRoleBinding"}


def bounds(documents, session, approval):
    labels = {"live-lab-session": session, "live-lab-approval": approval,
              "app.kubernetes.io/part-of": "argocd"}
    for obj in documents:
        obj["metadata"].setdefault("labels", {}).update(labels)
        if obj["kind"] not in CLUSTER_KINDS:
            obj["metadata"]["namespace"] = "argocd"
        if obj["kind"] == "Service":
            g.require(obj.get("spec", {}).get("type", "ClusterIP") == "ClusterIP", "external Service forbidden")
        if obj["kind"] in ("Deployment", "StatefulSet"):
            name = obj["metadata"]["name"]
            obj["spec"]["replicas"] = 0 if name in OPTIONAL else 1
            template = obj["spec"]["template"]
            template["metadata"].setdefault("labels", {}).update(labels)
            for c in template["spec"].get("containers", []) + template["spec"].get("initContainers", []):
                c["resources"] = {"requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "32Mi"},
                                  "limits": {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "512Mi"}}
                if name == "argocd-application-controller":
                    c["resources"]["requests"]["memory"] = "256Mi"
                    c["resources"]["limits"].update(cpu="1", memory="1Gi")
    return documents


def validate_cluster(a, kube):
    identity = g.document(g.run(["aws", "--region", a.region, "sts", "get-caller-identity", "--output", "json"]))
    g.require(identity["Account"] == a.account, "account mismatch")
    c = g.document(g.run(["aws", "--region", a.region, "eks", "describe-cluster", "--name", a.cluster, "--output", "json"]))["cluster"]
    g.require(c["arn"] == f"arn:aws:eks:{a.region}:{a.account}:cluster/{a.cluster}" and c["status"] == "ACTIVE", "cluster mismatch")
    g.require(all(c["tags"].get(k) == v for k, v in {"Project": "kyobo-platform-live-lab", "Session": a.session,
                                                                "Approval": a.approval}.items()), "ownership mismatch")
    cfg = g.document(g.run(kube + ["config", "view", "--raw", "--flatten", "--minify", "-o", "json"]))
    g.require(len(cfg["clusters"]) == 1, "ambiguous context")
    connection = cfg["clusters"][0]["cluster"]
    g.require(connection.get("server") == c["endpoint"] and
              connection.get("certificate-authority-data") == c["certificateAuthority"]["data"] and
              not connection.get("insecure-skip-tls-verify") and not connection.get("proxy-url"), "context mismatch")


def main():
    a = g.parser().parse_args()
    g.validate_args(a)
    if not a.execute:
        print(json.dumps({"status": "plan_only", "version": VERSION, "manifest_sha256": SHA256,
                          "scope": a.cluster, "application_deployment": False}))
        return 0
    kube = ["kubectl", "--context", a.context, "--request-timeout=30s"]
    validate_cluster(a, kube)
    source = urllib.request.urlopen(URL, timeout=30).read()
    g.require(hashlib.sha256(source).hexdigest() == SHA256, "pinned manifest checksum mismatch")
    docs = bounds([d for d in yaml.safe_load_all(source) if d], a.session, a.approval)
    ns = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "argocd",
          "labels": {"live-lab-session": a.session, "live-lab-approval": a.approval}}}
    docs.insert(0, ns)
    # Check every target before applying any of them; never take over someone else's install.
    for obj in docs:
        cmd = kube + (["-n", "argocd"] if obj["kind"] not in CLUSTER_KINDS | {"Namespace"} else [])
        existing = g.run(cmd + ["get", obj["kind"], obj["metadata"]["name"], "--ignore-not-found", "-o", "json"]).stdout
        if existing.strip():
            labels = json.loads(existing)["metadata"].get("labels", {})
            g.require(labels.get("live-lab-session") == a.session and labels.get("live-lab-approval") == a.approval,
                      "existing Argo object is not owned by this session")
    report = {"status": "failed", "version": VERSION, "source": URL, "manifest_sha256": SHA256,
              "scope": {k: getattr(a, k) for k in ("account", "region", "session", "approval", "cluster", "context")},
              "objects": [{"kind": d["kind"], "name": d["metadata"]["name"]} for d in docs],
              "optional_replicas": 0, "external_service": False}
    fd = os.open(a.evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as evidence:
        try:
            # No force-conflicts: concurrent ownership conflicts must remain visible.
            g.run(kube + ["apply", "--server-side", "--field-manager=live-governance-argocd", "-f", "-"],
                  yaml.safe_dump_all(docs))
            report["status"] = "applied_readiness_pending"
        finally:
            json.dump(report, evidence, indent=2)
            evidence.write("\n")
    print(json.dumps({"status": report["status"], "version": VERSION, "evidence": a.evidence}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc) if isinstance(exc, g.CheckFailed) else "installation_error"}))
        raise SystemExit(1)
