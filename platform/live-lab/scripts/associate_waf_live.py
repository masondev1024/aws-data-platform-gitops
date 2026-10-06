#!/usr/bin/env python3
"""Associate the exact session WAF ACL with its tagged, active ALB as the operator."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time


PROJECT = "kyobo-platform-live-lab"


class AssociationError(Exception):
    """A safe, fixed-code failure for this tightly scoped live operation."""


def require(condition, code):
    if not condition:
        raise AssociationError(code)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    for name in ("account", "region", "session", "approval", "cluster", "alb-dns", "web-acl-arn", "profile", "evidence"):
        result.add_argument("--" + name, required=True)
    return result


def validate_scope(args):
    require(re.fullmatch(r"[0-9]{12}", args.account) is not None, "invalid_account")
    require(args.region == "ap-northeast-2", "invalid_region")
    require(re.fullmatch(r"[a-z0-9][a-z0-9-]{5,40}", args.session) is not None, "invalid_session")
    require(re.fullmatch(r"SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}", args.approval) is not None,
            "invalid_approval")
    require(args.cluster == "kyobo-" + args.session, "cluster_mismatch")
    require(re.fullmatch(r"[A-Za-z0-9.-]+\.ap-northeast-2\.elb\.amazonaws\.com", args.alb_dns) is not None,
            "invalid_alb_dns")
    acl_prefix = (f"arn:aws:wafv2:{args.region}:{args.account}:regional/webacl/"
                  f"{args.cluster}-web-acl/")
    require(args.web_acl_arn.startswith(acl_prefix) and
            re.fullmatch(re.escape(acl_prefix) + r"[A-Fa-f0-9-]{36}", args.web_acl_arn) is not None,
            "web_acl_scope_mismatch")


def expected_tags(args):
    return {"Project": PROJECT, "Session": args.session, "Approval": args.approval}


def tag_map(items):
    result = {item["Key"]: item["Value"] for item in items}
    require(len(result) == len(items), "duplicate_tags")
    return result


def verify_owned_tags(tags, args, *, include_cluster=False):
    required = expected_tags(args)
    if include_cluster:
        required["elbv2.k8s.aws/cluster"] = args.cluster
    require(all(tags.get(key) == value for key, value in required.items()), "ownership_mismatch")


def load_balancers_for_dns(elbv2, dns_name):
    matches = []
    marker = None
    while True:
        response = elbv2.describe_load_balancers(**({"Marker": marker} if marker else {}))
        matches.extend(lb for lb in response.get("LoadBalancers", []) if lb.get("DNSName") == dns_name)
        marker = response.get("NextMarker")
        if not marker:
            return matches


def existing_web_acl(wafv2, alb_arn):
    response = wafv2.get_web_acl_for_resource(ResourceArn=alb_arn)
    return (response.get("WebACL") or {}).get("ARN")


def cloudwatch_load_balancer_dimension(alb_arn):
    match = re.fullmatch(
        r"arn:aws:elasticloadbalancing:[a-z0-9-]+:[0-9]{12}:loadbalancer/app/([A-Za-z0-9-]{1,32})/([a-f0-9]{16,32})",
        alb_arn,
    )
    require(match is not None, "alb_cloudwatch_dimension_invalid")
    name, resource_id = match.groups()
    return name, resource_id, f"app/{name}/{resource_id}"


def write_evidence(path_value, report):
    repo_root = Path(__file__).resolve().parents[3]
    evidence_root = (repo_root / "platform/live-lab/evidence").resolve()
    path = Path(path_value).resolve()
    try:
        path.relative_to(evidence_root)
    except ValueError as exc:
        raise AssociationError("evidence_path_out_of_scope") from exc
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".waf-association.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def associate(args, clients, *, sleep=time.sleep, now=lambda: datetime.now(timezone.utc), attempts=12):
    validate_scope(args)
    require(1 <= attempts <= 20, "invalid_retry_bound")
    require(clients["sts"].meta.region_name == args.region and
            clients["eks"].meta.region_name == args.region and
            clients["elbv2"].meta.region_name == args.region and
            clients["wafv2"].meta.region_name == args.region, "client_region_mismatch")

    require(clients["sts"].get_caller_identity().get("Account") == args.account, "account_mismatch")
    cluster = clients["eks"].describe_cluster(name=args.cluster)["cluster"]
    require(cluster.get("arn") == f"arn:aws:eks:{args.region}:{args.account}:cluster/{args.cluster}" and
            cluster.get("status") == "ACTIVE", "cluster_identity_mismatch")
    verify_owned_tags(cluster.get("tags", {}), args)
    vpc_id = cluster.get("resourcesVpcConfig", {}).get("vpcId")
    require(isinstance(vpc_id, str) and re.fullmatch(r"vpc-[0-9a-f]+", vpc_id) is not None,
            "cluster_vpc_missing")

    matches = load_balancers_for_dns(clients["elbv2"], args.alb_dns)
    require(len(matches) == 1, "alb_dns_missing_or_ambiguous")
    alb = matches[0]
    alb_arn = alb.get("LoadBalancerArn", "")
    require(re.fullmatch(
        rf"arn:aws:elasticloadbalancing:{re.escape(args.region)}:{args.account}:loadbalancer/app/[A-Za-z0-9-]+/[a-f0-9]+",
            alb_arn) is not None, "alb_arn_scope_mismatch")
    lb_name, lb_id, lb_dimension = cloudwatch_load_balancer_dimension(alb_arn)
    require(alb.get("Type") == "application" and alb.get("Scheme") == "internet-facing" and
            alb.get("State", {}).get("Code") == "active" and alb.get("VpcId") == vpc_id,
            "alb_identity_mismatch")
    tag_response = clients["elbv2"].describe_tags(ResourceArns=[alb_arn]).get("TagDescriptions", [])
    require(len(tag_response) == 1 and tag_response[0].get("ResourceArn") == alb_arn, "alb_tags_missing")
    verify_owned_tags(tag_map(tag_response[0].get("Tags", [])), args, include_cluster=True)

    acl_tags = clients["wafv2"].list_tags_for_resource(ResourceARN=args.web_acl_arn)
    require(not acl_tags.get("NextMarker"), "incomplete_web_acl_tags")
    verify_owned_tags(tag_map(acl_tags.get("TagInfoForResource", {}).get("TagList", [])), args)

    current = existing_web_acl(clients["wafv2"], alb_arn)
    require(current in (None, args.web_acl_arn), "foreign_web_acl_association")
    changed = current is None
    if changed:
        clients["wafv2"].associate_web_acl(WebACLArn=args.web_acl_arn, ResourceArn=alb_arn)

    verified = False
    for attempt in range(attempts):
        if existing_web_acl(clients["wafv2"], alb_arn) == args.web_acl_arn:
            verified = True
            break
        if attempt + 1 < attempts:
            sleep(5)
    require(verified, "association_not_visible_after_bounded_retry")

    report = {
        "status": "verified",
        "operation": "associated" if changed else "already_associated",
        "account": args.account,
        "region": args.region,
        "session": args.session,
        "approval": args.approval,
        "cluster": args.cluster,
        "alb_dns": args.alb_dns,
        "alb_arn": alb_arn,
        "alb_cloudwatch_load_balancer_name": lb_name,
        "alb_cloudwatch_load_balancer_id": lb_id,
        "alb_cloudwatch_load_balancer_dimension": lb_dimension,
        "web_acl_arn": args.web_acl_arn,
        "verified_at": now().astimezone(timezone.utc).isoformat(),
    }
    write_evidence(args.evidence, report)
    return report


def make_clients(args):
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    return {name: session.client(name) for name in ("sts", "eks", "elbv2", "wafv2")}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        report = associate(args, make_clients(args))
    except AssociationError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        # AWS client messages may contain account/request metadata; keep evidence controlled.
        code = getattr(getattr(exc, "response", {}).get("Error", {}), "get", lambda *_: None)("Code")
        print(json.dumps({"status": "failed", "reason": "aws_or_runtime_error", "aws_code": code}),
              file=sys.stderr)
        return 1
    print(json.dumps({"status": report["status"], "operation": report["operation"],
                      "alb_arn": report["alb_arn"],
                      "alb_cloudwatch_load_balancer_dimension": report["alb_cloudwatch_load_balancer_dimension"],
                      "web_acl_arn": report["web_acl_arn"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
