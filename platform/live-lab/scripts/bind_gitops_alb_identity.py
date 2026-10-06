#!/usr/bin/env python3
"""Bind a verified session ALB to the GitOps Rollout without changing its spec."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
INSTALLER = importlib.util.spec_from_file_location(
    "delivery_installer", ROOT / "platform/governance/scripts/install_argocd_live.py"
)
installer = importlib.util.module_from_spec(INSTALLER)
INSTALLER.loader.exec_module(installer)
g = installer.g
EVIDENCE_ROOT = ROOT / "platform/live-lab/evidence"


class BindingError(Exception):
    """The live ALB or rollout does not match the approved session."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise BindingError(reason)


def private_evidence(path_value: str, *, must_exist: bool) -> Path:
    path = Path(path_value)
    require(path.is_absolute() or not any(part == ".." for part in path.parts), "unsafe_evidence_path")
    resolved = path.resolve()
    require(resolved.parent == EVIDENCE_ROOT.resolve(), "evidence_outside_session_directory")
    require(not path.is_symlink(), "symlinked_evidence")
    if must_exist:
        require(path.is_file() and path.stat().st_mode & 0o777 == 0o600, "missing_private_waf_evidence")
    else:
        require(not path.exists(), "binding_evidence_already_exists")
    return resolved


def derive_labels(args, report: dict, ingress: dict, rollout: dict, alb: dict,
                  tags: dict, web_acl_arn: str) -> dict[str, str]:
    expected = {
        "account": args.account, "region": args.region, "session": args.session,
        "approval": args.approval, "cluster": args.cluster,
    }
    require(args.region == "ap-northeast-2" and report.get("status") == "verified", "unverified_waf_evidence")
    require(all(report.get(key) == value for key, value in expected.items()), "waf_evidence_scope_mismatch")
    arn = report.get("alb_arn", "")
    match = re.fullmatch(
        rf"arn:aws:elasticloadbalancing:{re.escape(args.region)}:{args.account}:"
        r"loadbalancer/app/([A-Za-z0-9-]{1,32})/([a-f0-9]{16,32})", arn,
    )
    require(match is not None, "alb_arn_scope_mismatch")
    name, identifier = match.groups()
    require(report.get("alb_cloudwatch_load_balancer_name") == name and
            report.get("alb_cloudwatch_load_balancer_id") == identifier and
            report.get("alb_cloudwatch_load_balancer_dimension") == f"app/{name}/{identifier}",
            "alb_metric_dimension_mismatch")
    require(alb.get("LoadBalancerArn") == arn and alb.get("Type") == "application" and
            alb.get("Scheme") == "internet-facing" and alb.get("State", {}).get("Code") == "active" and
            alb.get("DNSName") == report.get("alb_dns"), "live_alb_mismatch")
    require(all(tags.get(key) == value for key, value in {
        "Project": "kyobo-platform-live-lab", "Session": args.session,
        "Approval": args.approval, "elbv2.k8s.aws/cluster": args.cluster,
    }.items()), "alb_ownership_mismatch")
    acl_prefix = (f"arn:aws:wafv2:{args.region}:{args.account}:regional/webacl/"
                  f"{args.cluster}-web-acl/")
    require(report.get("web_acl_arn", "").startswith(acl_prefix) and
            report.get("web_acl_arn") == web_acl_arn, "live_waf_association_mismatch")
    ingress_addresses = ingress.get("status", {}).get("loadBalancer", {}).get("ingress", [])
    require(ingress.get("metadata", {}).get("name") == "data-pipeline-ingress" and
            ingress.get("metadata", {}).get("namespace") == "platform-validation" and
            isinstance(ingress_addresses, list) and len(ingress_addresses) == 1 and
            isinstance(ingress_addresses[0], dict) and
            ingress_addresses[0].get("hostname") == report.get("alb_dns") and
            not ingress_addresses[0].get("ip"), "ingress_alb_mismatch")
    metadata = rollout.get("metadata", {})
    require(metadata.get("name") == "data-pipeline-rollout" and
            metadata.get("namespace") == "platform-validation" and
            bool(metadata.get("resourceVersion")) and
            rollout.get("spec", {}).get("selector", {}).get("matchLabels", {}).get("app") == "data-pipeline-app",
            "rollout_scope_mismatch")
    labels = dict(metadata.get("labels") or {})
    for key, value in (("live-lab.aws/alb-name", name), ("live-lab.aws/alb-id", identifier)):
        require(labels.get(key) in (None, value), "conflicting_rollout_alb_identity")
        labels[key] = value
    return labels


def canary_target_group_labels(args, bindings: list, target_group: dict, tags: dict,
                               alb: dict) -> tuple[dict[str, str], str]:
    matches = [item for item in bindings
               if item.get("spec", {}).get("serviceRef", {}).get("name") == "data-pipeline-svc-canary"]
    require(len(matches) == 1, "ambiguous_canary_target_group_binding")
    binding = matches[0]
    spec = binding["spec"]
    require(binding.get("metadata", {}).get("namespace") == "platform-validation" and
            spec.get("serviceRef", {}).get("port") == 80 and
            spec.get("targetType") == "ip", "canary_target_group_binding_mismatch")
    arn = spec.get("targetGroupARN", "")
    match = re.fullmatch(
        rf"arn:aws:elasticloadbalancing:{re.escape(args.region)}:{args.account}:"
        r"targetgroup/([A-Za-z0-9-]{1,32})/([a-f0-9]{16,32})", arn,
    )
    require(match is not None, "canary_target_group_arn_mismatch")
    name, identifier = match.groups()
    require(target_group.get("TargetGroupArn") == arn and
            target_group.get("TargetGroupName") == name and
            target_group.get("Protocol") == "HTTP" and target_group.get("Port") == 8080 and
            target_group.get("TargetType") == "ip" and
            target_group.get("VpcId") == alb.get("VpcId") and
            target_group.get("LoadBalancerArns") == [alb.get("LoadBalancerArn")],
            "canary_target_group_not_attached_to_owned_alb")
    require(all(tags.get(key) == value for key, value in {
        "Project": "kyobo-platform-live-lab", "Session": args.session,
        "Approval": args.approval, "elbv2.k8s.aws/cluster": args.cluster,
        "ingress.k8s.aws/resource":
            "platform-validation/data-pipeline-ingress-data-pipeline-svc-canary:80",
    }.items()), "canary_target_group_ownership_mismatch")
    return {"live-lab.aws/canary-tg-name": name,
            "live-lab.aws/canary-tg-id": identifier}, f"targetgroup/{name}/{identifier}"


def aws_json(region: str, *arguments: str) -> dict:
    return g.document(g.run(["aws", "--region", region, "--output", "json", *arguments]))


def main() -> int:
    parser = g.parser()
    parser.add_argument("--waf-evidence", required=True)
    args = parser.parse_args()
    g.validate_args(args)
    require(args.region == "ap-northeast-2", "region_not_approved")
    waf_path = private_evidence(args.waf_evidence, must_exist=True)
    binding_path = private_evidence(args.evidence, must_exist=False)
    report = json.loads(waf_path.read_text(encoding="utf-8"))
    if not args.execute:
        print(json.dumps({"status": "plan_only", "session": args.session,
                          "operation": "verify_live_alb_then_patch_rollout_metadata_only"}))
        return 0
    require(bool(os.environ.get("AWS_PROFILE")), "explicit_aws_profile_required")
    kube = ["kubectl", "--context", args.context, "--request-timeout=30s"]
    installer.validate_cluster(args, kube)
    ingress = g.document(g.run(kube + ["-n", "platform-validation", "get", "ingress",
                                      "data-pipeline-ingress", "-o", "json"]))
    rollout = g.document(g.run(kube + ["-n", "platform-validation", "get", "rollout",
                                      "data-pipeline-rollout", "-o", "json"]))
    alb_arn = report.get("alb_arn", "")
    # Validate ARN locally before passing it to any AWS read API.
    require(bool(re.fullmatch(
        rf"arn:aws:elasticloadbalancing:{args.region}:{args.account}:loadbalancer/app/"
        r"[A-Za-z0-9-]{1,32}/[a-f0-9]{16,32}", alb_arn)), "alb_arn_scope_mismatch")
    balancers = aws_json(args.region, "elbv2", "describe-load-balancers", "--load-balancer-arns", alb_arn)
    require(len(balancers.get("LoadBalancers", [])) == 1, "ambiguous_live_alb")
    tag_descriptions = aws_json(args.region, "elbv2", "describe-tags", "--resource-arns", alb_arn)
    require(len(tag_descriptions.get("TagDescriptions", [])) == 1, "missing_alb_tags")
    tag_items = tag_descriptions["TagDescriptions"][0].get("Tags", [])
    tags = {item["Key"]: item["Value"] for item in tag_items}
    require(len(tags) == len(tag_items), "duplicate_alb_tags")
    association = aws_json(args.region, "wafv2", "get-web-acl-for-resource", "--resource-arn", alb_arn)
    labels = derive_labels(args, report, ingress, rollout, balancers["LoadBalancers"][0],
                           tags, (association.get("WebACL") or {}).get("ARN", ""))
    bindings = g.document(g.run(kube + ["-n", "platform-validation", "get",
                                      "targetgroupbindings.elbv2.k8s.aws", "-o", "json"]))["items"]
    matching = [item for item in bindings
                if item.get("spec", {}).get("serviceRef", {}).get("name") == "data-pipeline-svc-canary"]
    require(len(matching) == 1, "ambiguous_canary_target_group_binding")
    tg_arn = matching[0].get("spec", {}).get("targetGroupARN", "")
    require(bool(re.fullmatch(
        rf"arn:aws:elasticloadbalancing:{args.region}:{args.account}:targetgroup/"
        r"[A-Za-z0-9-]{1,32}/[a-f0-9]{16,32}", tg_arn)),
        "canary_target_group_arn_mismatch")
    target_groups = aws_json(args.region, "elbv2", "describe-target-groups",
                             "--target-group-arns", tg_arn)["TargetGroups"]
    require(len(target_groups) == 1, "ambiguous_canary_target_group")
    target_tag_response = aws_json(args.region, "elbv2", "describe-tags", "--resource-arns", tg_arn)
    require(len(target_tag_response.get("TagDescriptions", [])) == 1,
            "missing_canary_target_group_tags")
    tag_items = target_tag_response["TagDescriptions"][0]["Tags"]
    target_tags = {item["Key"]: item["Value"] for item in tag_items}
    require(len(target_tags) == len(tag_items), "duplicate_canary_target_group_tags")
    target_labels, target_dimension = canary_target_group_labels(
        args, bindings, target_groups[0], target_tags, balancers["LoadBalancers"][0])
    for key, value in target_labels.items():
        require(labels.get(key) in (None, value), "conflicting_canary_target_group_identity")
        labels[key] = value
    metadata = rollout["metadata"]
    patch = [
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "replace" if "labels" in metadata else "add", "path": "/metadata/labels", "value": labels},
    ]
    result = g.document(g.run(kube + ["-n", "platform-validation", "patch", "rollout",
                                      "data-pipeline-rollout", "--type=json", "-p", json.dumps(patch),
                                      "-o", "json"]))
    require(all(result.get("metadata", {}).get("labels", {}).get(key) == value for key, value in labels.items()),
            "rollout_binding_readback_mismatch")
    fd = os.open(binding_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"status": "verified", "account": args.account, "region": args.region,
                   "cluster": args.cluster, "session": args.session, "approval": args.approval,
                   "alb_arn": alb_arn, "alb_dimension": report["alb_cloudwatch_load_balancer_dimension"],
                   "canary_target_group_dimension": target_dimension,
                   "rollout_uid": result["metadata"]["uid"],
                   "rollout_resource_version": result["metadata"]["resourceVersion"]}, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"status": "verified", "session": args.session, "alb_dimension":
                      report["alb_cloudwatch_load_balancer_dimension"],
                      "canary_target_group_dimension": target_dimension}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BindingError, g.CheckFailed) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        raise SystemExit(2)
