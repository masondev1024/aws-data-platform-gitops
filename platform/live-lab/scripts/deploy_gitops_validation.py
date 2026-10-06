#!/usr/bin/env python3
"""Bootstrap session values, then ask Argo to sync reviewed Git (offline by default).

Never applies app Pods, Rollouts or migration Jobs directly. This is an admin
bootstrap, not a developer-permission or HTTP/data-correctness verification.
"""
from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "delivery_installer", ROOT / "platform/governance/scripts/install_argocd_live.py"
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
g = installer.g
APP_NAME = "data-pipeline-validation"


class SyncRequestUnknown(g.CheckFailed):
    """The server may have accepted the request; inspect state before retrying."""


def render(path):
    return [d for d in yaml.safe_load_all(g.run(["kubectl", "kustomize", str(path)]).stdout) if d]


def validate_workload(documents, image_reference):
    g.require(len(documents) > 0, "empty GitOps workload")
    for kind, name in (("Job", "data-pipeline-schema-migration"), ("Rollout", "data-pipeline-rollout"),
                       ("CronJob", "raffle-draw-job")):
        matches = [obj for obj in documents if obj.get("kind") == kind]
        g.require(len(matches) == 1 and matches[0].get("metadata", {}).get("name") == name,
                  "expected exactly the approved migration, rollout and draw workloads")
    job = next(obj for obj in documents if obj["kind"] == "Job")
    annotations = job["metadata"].get("annotations", {})
    g.require(annotations.get("argocd.argoproj.io/hook") == "PreSync" and
              set(annotations.get("argocd.argoproj.io/hook-delete-policy", "").split(",")) == {"BeforeHookCreation", "HookSucceeded"},
              "migration must gate sync with the reviewed PreSync hook policy")
    images = []
    for obj in documents:
        g.require(obj["kind"] not in {"Ingress", "Secret", "Namespace", "Role", "RoleBinding"},
                  "platform bootstrap resources must not enter the application lane")
        g.require(obj.get("metadata", {}).get("namespace") == g.NAMESPACE, "workload namespace mismatch")
        if obj["kind"] in {"Rollout", "Job"}:
            pod = obj["spec"]["template"]["spec"]
        elif obj["kind"] == "CronJob":
            pod = obj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        else:
            continue
        images.extend(c["image"] for c in pod.get("containers", []) + pod.get("initContainers", []))
    g.require(len(images) == 3 and all(image == image_reference for image in images),
              "Git must pin the reviewed image for application, migration and draw job")
    templates = [obj for obj in documents if obj["kind"] == "AnalysisTemplate" and
                 obj["metadata"]["name"] == "data-pipeline-canary"]
    g.require(len(templates) == 1, "exactly one canary AnalysisTemplate is required")
    template = templates[0]
    g.require({arg["name"] for arg in template["spec"]["args"]} >=
              {"service-name", "alb-name", "alb-id"}, "CloudWatch ALB arguments are required")
    metrics = {metric["name"]: metric for metric in template["spec"]["metrics"]}
    for name, threshold in (("alb-elb-error-rate", "< 0.01"),
                            ("alb-target-error-rate", "< 0.01"),
                            ("alb-target-response-p95", "< 0.5")):
        metric = metrics.get(name, {})
        cloudwatch = metric.get("provider", {}).get("cloudWatch", {})
        g.require(metric.get("count") == 3 and metric.get("failureLimit") == 0 and
                  metric.get("inconclusiveLimit") == 0 and
                  "len(result[0].Values) >= 3" in metric.get("successCondition", "") and
                  threshold in metric.get("successCondition", "") and
                  cloudwatch.get("metricDataQueries"),
                  f"CloudWatch {name} promotion gate is missing or weakened")
    rollout = next(obj for obj in documents if obj["kind"] == "Rollout")
    for index in (2, 5):
        args = {arg["name"]: arg for arg in rollout["spec"]["strategy"]["canary"]["steps"][index]["analysis"]["args"]}
        for name in ("alb-name", "alb-id"):
            g.require(args.get(name, {}).get("valueFrom", {}).get("fieldRef", {}).get("fieldPath") ==
                      f"metadata.labels['live-lab.aws/{name}']", "CloudWatch Rollout ALB identity is missing")
    g.require("__" not in yaml.safe_dump_all(documents), "unresolved runtime placeholder in Git workload")


def session_bootstrap(outputs, certificate, session, approval, account, region):
    """Produce only platform-owned namespace/RBAC/config/Ingress, never workloads."""
    values = {key: value["value"] for key, value in outputs.items()}
    g.require(values.get("session_id") == session and values.get("approval_id") == approval,
              "Terraform outputs belong to another session")
    g.require(values.get("cluster_name") == f"kyobo-{session}", "Terraform cluster mismatch")
    for key in ("db_writer_endpoint", "db_reader_endpoint"):
        g.require(bool(re.fullmatch(r"[a-z0-9.-]+\." + re.escape(region) + r"\.rds\.amazonaws\.com", values[key])),
                  "database endpoint is outside the approved region")
    network = ipaddress.ip_network(values["operator_cidr"], strict=True)
    g.require(network.version == 4 and network.prefixlen == 32, "operator CIDR must be one IPv4 address")
    g.require(bool(re.fullmatch(rf"arn:aws:acm:{region}:{account}:certificate/[a-f0-9-]+", certificate)),
              "certificate is outside approved account/region")
    waf = values["waf_web_acl_arn"]
    g.require(bool(re.fullmatch(rf"arn:aws:wafv2:{region}:{account}:regional/webacl/kyobo-{session}-web-acl/[a-f0-9-]+", waf)),
              "WAF is outside approved session")
    docs = render(ROOT / "platform/governance/bootstrap")
    config = yaml.safe_load((ROOT / "platform/live-lab/manifests/bootstrap/resources/raffle-config.yaml").read_text())
    config["data"].update(DB_WRITER_HOST=values["db_writer_endpoint"], DB_READER_HOST=values["db_reader_endpoint"])
    ingress = yaml.safe_load((ROOT / "k8s/base/ingress.yaml").read_text())
    patch = yaml.safe_load((ROOT / "platform/live-lab/manifests/app/patches/patch-ingress-live-lab.yaml").read_text())
    substitutions = {"__ALB_CERTIFICATE_ARN__": certificate, "__WAF_WEB_ACL_ARN__": waf,
                     "__OPERATOR_CIDR__": str(network), "__SESSION_ID__": session, "__APPROVAL_ID__": approval}
    for key, value in patch["metadata"]["annotations"].items():
        for marker, replacement in substitutions.items():
            value = value.replace(marker, replacement)
        ingress["metadata"]["annotations"][key] = value
    ingress["metadata"]["namespace"] = g.NAMESPACE
    docs.extend([config, ingress])
    docs.extend(yaml.safe_load_all((g.GOV / "argocd/developer-application-reader.yaml").read_text()))
    docs.append(yaml.safe_load((g.GOV / "argocd/app-project.yaml").read_text()))
    for obj in docs:
        obj["metadata"].setdefault("labels", {}).update(
            {"live-lab-session": session, "live-lab-approval": approval})
    return docs


def check_ownership(kube, documents, session, approval):
    for obj in documents:
        meta = obj["metadata"]
        namespace = ["-n", meta["namespace"]] if meta.get("namespace") else []
        result = g.run(kube + namespace + ["get", obj["kind"], meta["name"], "--ignore-not-found", "-o", "json"])
        if result.stdout.strip():
            existing = json.loads(result.stdout)
            labels = existing["metadata"].get("labels", {})
            g.require(labels.get("live-lab-session") == session and labels.get("live-lab-approval") == approval,
                      "bootstrap refuses to overwrite an object owned by another session")


def checked_revision(revision):
    g.require(g.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).stdout.strip() == revision,
              "run from the exact reviewed release checkout")
    g.require(not g.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"]).stdout.strip(),
              "tracked release checkout must be clean")
    head = g.run(["gh", "api", "repos/masondev1024/aws-data-platform-gitops/git/ref/heads/main",
                  "--jq", ".object.sha"]).stdout.strip()
    g.require(head == revision, "approved GitOps revision is no longer main HEAD")


def verify_signed_delivery(bundle_dir, image_reference):
    """Bind the signed source candidate to the exact reviewed GitOps manifest."""
    image, digest = image_reference.rsplit("@", 1)
    image_name, source_revision = image.rsplit(":", 1)
    g.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", source_revision, "HEAD"])
    relative_manifest = "k8s/overlays/validation/kustomization.yaml"
    candidate = bundle_dir / "validation-candidate/validation-kustomization.yaml"
    approved_bytes = (ROOT / relative_manifest).read_bytes()
    g.require(candidate.read_bytes() == approved_bytes,
              "reviewed Git manifest differs from the signed image-only candidate")
    source_manifest = g.run(["git", "-C", str(ROOT), "show", f"{source_revision}:{relative_manifest}"]).stdout
    with tempfile.TemporaryDirectory(prefix="delivery-source-") as directory:
        path = Path(directory) / "kustomization.yaml"
        path.write_text(source_manifest)
        result = g.document(g.run([
            sys.executable, str(ROOT / "scripts/verify_release_bundle.py"),
            "--candidate-target", "validation", "--candidate-dir", str(bundle_dir / "validation-candidate"),
            "--release-evidence", str(bundle_dir / "validation-release-evidence.json"),
            "--sbom", str(bundle_dir / "data-pipeline-app.sbom.spdx.json"),
            "--current-manifest", str(path), "--source-revision", source_revision,
            "--image-name", image_name, "--verify-attestation", "--verify-image-attestation",
        ]))
    g.require(result.get("attestation") == "verified" and result.get("oci_image_subject_verified") is True
              and result.get("image_digest") == digest, "signed delivery provenance was not verified")
    g.require(candidate.read_bytes() == approved_bytes == (ROOT / relative_manifest).read_bytes(),
              "approved manifest changed during provenance verification")
    return {"source_revision": source_revision, "release_evidence_provenance": "verified",
            "oci_image_sbom_subject": "verified"}


def validate_canary_binding(rollout, image_reference, proof, session, approval, account, region, cluster):
    """A changed image must not enter canary without the session ALB gate."""
    if not rollout:
        return  # Initial rollout skips canary steps; the ALB does not exist yet.
    current = rollout["spec"]["template"]["spec"]["containers"][0]["image"]
    if current == image_reference:
        return
    g.require(isinstance(proof, dict) and proof.get("status") == "verified" and
              proof.get("session") == session and proof.get("approval") == approval and
              proof.get("account") == account and proof.get("region") == region and
              proof.get("cluster") == cluster and
              proof.get("rollout_uid") == rollout["metadata"].get("uid"),
              "changed image requires current session ALB binding evidence")
    dimension = proof.get("alb_dimension", "")
    match = re.fullmatch(r"app/([A-Za-z0-9-]{1,32})/([a-f0-9]{16,32})", dimension)
    g.require(match is not None, "ALB binding evidence has an invalid CloudWatch dimension")
    labels = rollout["metadata"].get("labels", {})
    g.require(labels.get("live-lab.aws/alb-name") == match.group(1) and
              labels.get("live-lab.aws/alb-id") == match.group(2),
              "Rollout ALB labels do not match verified binding evidence")


def binding_proof(path_value):
    if not path_value:
        return None
    path = Path(path_value)
    evidence_dir = ROOT / "platform/live-lab/evidence"
    g.require(path.resolve().parent == evidence_dir.resolve() and path.is_file() and
              not path.is_symlink() and path.stat().st_mode & 0o777 == 0o600,
              "ALB binding evidence must be a private session file")
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = g.parser()
    parser.add_argument("--gitops-revision", required=True)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True,
                        help="Downloaded CI release artifact; signatures are reverified before mutation")
    parser.add_argument("--alb-binding-evidence",
                        help="Verified operator ALB binding; required when the Rollout image changes")
    a = parser.parse_args()
    g.validate_args(a)
    g.require(a.region == "ap-northeast-2", "only the approved Seoul region is supported")
    g.require(bool(re.fullmatch(r"[a-f0-9]{40}", a.gitops_revision)), "full GitOps SHA required")
    g.require(bool(re.fullmatch(rf"{a.account}\.dkr\.ecr\.eu-west-1\.amazonaws\.com/data-pipeline-app:[a-f0-9]{{40}}@sha256:[a-f0-9]{{64}}", a.image_reference)),
              "only the exact digest-pinned CI image repository is supported")
    report = {"status": "plan_only", "gitops_revision": a.gitops_revision,
              "image_reference": a.image_reference, "application": APP_NAME,
              "application_resources_directly_applied": False,
              "verification_scope": "sync_request_only_not_deployment_success"}
    validate_workload(render(ROOT / "k8s/overlays/validation"), a.image_reference)
    if not a.execute:
        print(json.dumps(report))
        return 0
    g.require(bool(os.environ.get("AWS_PROFILE")), "explicit AWS_PROFILE required")
    g.require(not Path(a.evidence).exists(), "evidence must be a new local file")
    checked_revision(a.gitops_revision)
    # Execute external verification, not a caller-supplied 'verified' JSON flag.
    report["supply_chain"] = verify_signed_delivery(a.bundle_dir, a.image_reference)
    kube = ["kubectl", "--context", a.context, "--request-timeout=30s"]
    installer.validate_cluster(a, kube)
    namespace = g.run(kube + ["get", "namespace", g.NAMESPACE, "--ignore-not-found", "-o", "name"])
    existing_rollout = None
    if namespace.stdout.strip():
        current_name = g.run(kube + ["-n", g.NAMESPACE, "get", "rollout", "data-pipeline-rollout",
                                     "--ignore-not-found", "-o", "name"])
        if current_name.stdout.strip():
            existing_rollout = g.document(g.run(kube + ["-n", g.NAMESPACE, "get", "rollout",
                                                     "data-pipeline-rollout", "-o", "json"]))
    validate_canary_binding(existing_rollout, a.image_reference,
                            binding_proof(a.alb_binding_evidence), a.session, a.approval,
                            a.account, a.region, a.cluster)
    outputs = g.document(g.run(["terraform", f"-chdir={ROOT / 'platform/live-lab/terraform'}", "output", "-json"]))
    certificate = (ROOT / "platform/live-lab/evidence/acm-certificate-arn.txt").read_text().strip()
    tags = g.document(g.run(["aws", "--region", a.region, "acm", "list-tags-for-certificate",
                            "--certificate-arn", certificate, "--output", "json"]))
    tag_map = {item["Key"]: item["Value"] for item in tags["Tags"]}
    g.require(all(tag_map.get(k) == v for k, v in {"Project": "kyobo-platform-live-lab", "Session": a.session,
                                                "Approval": a.approval}.items()), "certificate ownership mismatch")
    docs = session_bootstrap(outputs, certificate, a.session, a.approval, a.account, a.region)
    app = yaml.safe_load((g.GOV / "argocd/validation-application.yaml").read_text())
    app["metadata"]["labels"] = {"live-lab-session": a.session, "live-lab-approval": a.approval}
    check_ownership(kube, docs + [app], a.session, a.approval)
    fd = os.open(a.evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    report["status"] = "failed"
    with os.fdopen(fd, "w") as handle:
        try:
            g.run(kube + ["apply", "--server-side", "--field-manager=delivery-bootstrap", "-f", "-"],
                  yaml.safe_dump_all(docs))
            # Reuse existing scope-guarded helpers; secrets are never copied to the report.
            previous_env = dict(os.environ)
            try:
                os.environ.update(AWS_REGION=a.region, EXPECTED_ACCOUNT_ID=a.account, SESSION_ID=a.session,
                                  APPROVAL_ID=a.approval, KUBE_CONTEXT=a.context, NAMESPACE=g.NAMESPACE)
                g.run(["bash", str(ROOT / "platform/live-lab/scripts/prepare_rds_ca_bundle.sh")])
                secrets = [g.run(kube + ["-n", g.NAMESPACE, "get", "secret", name, "--ignore-not-found", "-o", "json"]).stdout
                           for name in ("raffle-secret", "raffle-migration-secret")]
                if not any(value.strip() for value in secrets):
                    g.run(["bash", str(ROOT / "platform/live-lab/scripts/create_runtime_k8s_secret.sh")])
                else:
                    for value in secrets:
                        g.require(bool(value.strip()), "partial runtime credential install requires recovery")
                        labels = json.loads(value)["metadata"].get("labels", {})
                        g.require(labels.get("live-lab-session") == a.session and labels.get("live-lab-approval") == a.approval,
                                  "runtime credential ownership mismatch")
            finally:
                os.environ.clear()
                os.environ.update(previous_env)
            checked_revision(a.gitops_revision)
            g.run(kube + ["apply", "--server-side", "--field-manager=delivery-bootstrap", "-f", "-"], yaml.safe_dump(app))
            current = g.document(g.run(kube + ["-n", "argocd", "get", "application", APP_NAME, "-o", "json"]))
            g.require(not current.get("operation"), "another Argo operation is active")
            g.require(current["spec"] == app["spec"], "Argo Application contains unapproved configuration")
            # Optimistic-lock the sync request: do not overwrite a concurrently queued operation.
            patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
                     {"op": "add", "path": "/operation", "value": {"initiatedBy": {"username": "delivery-bootstrap"},
                      "sync": {"revision": a.gitops_revision, "prune": True}}}]
            report["status"] = "sync_request_outcome_unknown"
            try:
                g.run(kube + ["-n", "argocd", "patch", "application", APP_NAME, "--type=json", "-p", json.dumps(patch)])
            except g.CheckFailed as exc:
                raise SyncRequestUnknown("sync request outcome unknown; inspect Application before retrying") from exc
            report["status"] = "sync_requested"
        finally:
            json.dump(report, handle, indent=2)
            handle.write("\n")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "unknown" if isinstance(exc, SyncRequestUnknown) else "failed",
                          "error": str(exc) if isinstance(exc, g.CheckFailed) else "delivery_bootstrap_error"}))
        raise SystemExit(1)
