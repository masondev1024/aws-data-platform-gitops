#!/usr/bin/env python3
"""Reject a Terraform plan that escapes the approved two-node Seoul live-lab shape."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys

from estimate_session_cost import estimate


EXPECTED = {
    "aws_eks_cluster.lab",
    "aws_eks_node_group.lab",
    "aws_db_instance.mysql_primary",
    "aws_db_instance.mysql_reader",
    "aws_wafv2_web_acl.lab",
    "aws_ecr_repository.app",
}
ALLOWED_AWS_TYPES = {
    "aws_db_instance",
    "aws_db_subnet_group",
    "aws_ecr_repository",
    "aws_eks_cluster",
    "aws_eks_node_group",
    "aws_iam_openid_connect_provider",
    "aws_iam_policy",
    "aws_iam_role",
    "aws_iam_role_policy_attachment",
    "aws_internet_gateway",
    "aws_route_table",
    "aws_route_table_association",
    "aws_security_group",
    "aws_subnet",
    "aws_vpc",
    "aws_vpc_security_group_ingress_rule",
    "aws_wafv2_web_acl",
}
FORBIDDEN_TYPES = {
    "aws_nat_gateway",
    "aws_eip",
    "aws_instance",
    "aws_cloudwatch_log_group",
    "aws_vpc_endpoint",
    "aws_lb",
    "aws_lb_target_group",
}


def walk(module: dict):
    for resource in module.get("resources", []):
        yield resource
    for child in module.get("child_modules", []):
        yield from walk(child)


def validate(path: str, account: str, region: str, session: str, approval: str, budget: float,
             hours: float = 3, reserve: float = 1, requests: int = 200_000) -> list[str]:
    cost = estimate(hours, requests, budget, reserve)
    if not cost["within_budget"]:
        raise ValueError("estimated session including reserve exceeds approved budget")
    if not math.isfinite(hours) or not 1 <= hours <= 3:
        raise ValueError("approved hours must be between one and three")
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    format_version = plan.get("format_version")
    if not isinstance(format_version, str) or not re.fullmatch(r"1\.[0-9]+", format_version):
        raise ValueError("Terraform plan JSON format is missing or has an unsupported major version")
    if plan.get("errored") is not False:
        raise ValueError("Terraform plan is errored or does not explicitly report errored=false")
    if plan.get("applyable") is not True:
        raise ValueError("Terraform plan is not applyable")
    if plan.get("complete") is not True:
        raise ValueError("Terraform plan is incomplete")
    variables = {key: item.get("value") for key, item in plan.get("variables", {}).items()}
    expected_variables = {
        "aws_account_id": account,
        "aws_region": region,
        "session_id": session,
        "approval_id": approval,
        "cost_budget_usd": budget,
        "max_session_hours": hours,
        "apply_approval_phrase": "APPROVED_FOR_EPHEMERAL_APPLY",
        "eks_node_instance_type": "m7i.xlarge",
        "eks_node_min_size": 2,
        "eks_node_desired_size": 2,
        "eks_node_max_size": 2,
    }
    mismatch = {key: (value, variables.get(key)) for key, value in expected_variables.items() if variables.get(key) != value}
    if mismatch:
        raise ValueError(f"plan input mismatch: {mismatch}")
    if region != "ap-northeast-2" or not 0 < budget <= 5.5:
        raise ValueError("plan is outside the approved Seoul region or USD 5.50 ceiling")

    resources = list(walk(plan.get("planned_values", {}).get("root_module", {})))
    managed = [item for item in resources if item.get("mode") == "managed" and item.get("type", "").startswith("aws_")]
    by_address = {item.get("address"): item for item in managed}
    missing = sorted(address for address in EXPECTED if address not in by_address)
    if missing:
        raise ValueError(f"required approved resources are missing from plan: {missing}")
    resource_types = [item.get("type") for item in managed]
    forbidden = sorted(set(resource_types) & FORBIDDEN_TYPES)
    if forbidden:
        raise ValueError(f"unexpected cost/network resources in plan: {forbidden}")
    unknown = sorted({kind for kind in resource_types if kind not in ALLOWED_AWS_TYPES})
    if unknown:
        raise ValueError(f"unreviewed AWS resource types in plan: {unknown}")
    if resource_types.count("aws_eks_cluster") != 1 or resource_types.count("aws_eks_node_group") != 1:
        raise ValueError("the plan must contain exactly one EKS cluster and one managed node group")
    if resource_types.count("aws_db_instance") != 2:
        raise ValueError("the plan must contain exactly one writer and one asynchronous reader")
    if resource_types.count("aws_wafv2_web_acl") != 1 or resource_types.count("aws_ecr_repository") != 1:
        raise ValueError("the plan must contain exactly one session WAF ACL and one ECR repository")

    node_values = by_address["aws_eks_node_group.lab"].get("values") or {}
    scaling = node_values.get("scaling_config") or []
    if len(scaling) != 1 or any(scaling[0].get(key) != 2 for key in ("min_size", "desired_size", "max_size")):
        raise ValueError("managed node group must be fixed at exactly two nodes")
    if node_values.get("instance_types") != ["m7i.xlarge"]:
        raise ValueError("managed node instance type must match the approved cost estimate")
    if node_values.get("disk_size") != 20:
        raise ValueError("worker disk size must match the 20 GB per node cost estimate")

    writer = by_address["aws_db_instance.mysql_primary"].get("values") or {}
    reader = by_address["aws_db_instance.mysql_reader"].get("values") or {}
    if writer.get("engine") != "mysql" or not re.fullmatch(r"8\.4(?:\.[0-9]+)?", str(writer.get("engine_version"))):
        raise ValueError("new writer must use standard-support MySQL 8.4")
    for db in (writer, reader):
        if db.get("engine_lifecycle_support") != "open-source-rds-extended-support-disabled":
            raise ValueError("paid RDS extended support must be explicitly disabled")
    configuration = plan.get("configuration", {}).get("root_module", {}).get("resources", [])
    replica_config = next((r for r in configuration if r.get("address") == "aws_db_instance.mysql_reader"), {})
    references = replica_config.get("expressions", {}).get("replicate_source_db", {}).get("references", [])
    if "aws_db_instance.mysql_primary.arn" not in references:
        raise ValueError("reader must inherit its engine/version from the approved writer ARN")
    if reader.get("engine_version") is not None and not re.fullmatch(r"8\.4(?:\.[0-9]+)?", str(reader["engine_version"])):
        raise ValueError("reader engine version does not match the approved writer")
    if (writer.get("instance_class"), writer.get("multi_az"), writer.get("publicly_accessible"), writer.get("allocated_storage"), writer.get("storage_type")) != (
        "db.t3.small", True, False, 20, "gp3"
    ):
        raise ValueError("writer must be private MySQL db.t3.small, Multi-AZ, gp3, 20 GB")
    if (reader.get("instance_class"), reader.get("multi_az"), reader.get("publicly_accessible")) != (
        "db.t3.small", False, False
    ):
        raise ValueError("reader must be private Single-AZ MySQL db.t3.small")

    expected_tags = {"Project": "kyobo-platform-live-lab", "Session": session, "Approval": approval}
    for resource in managed:
        values = resource.get("values") or {}
        tags = values.get("tags_all") or values.get("tags") or {}
        if tags and any(tags.get(key) != value for key, value in expected_tags.items()):
            raise ValueError(f"planned resource tag mismatch: {resource.get('address')}")

    changes = plan.get("resource_changes", [])
    aws_changes = {
        item.get("address"): item
        for item in changes
        if item.get("mode") == "managed" and item.get("type", "").startswith("aws_")
    }
    missing_actions = sorted(set(by_address) - set(aws_changes))
    if missing_actions:
        raise ValueError(f"plan omits explicit actions for managed AWS resources: {missing_actions}")

    expected_attachments = {
        "aws_iam_role_policy_attachment.ecr_readonly": (
            f"kyobo-{session}-node", "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
        ),
        "aws_iam_role_policy_attachment.eks_cluster": (
            f"kyobo-{session}-eks-cluster", "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
        ),
        "aws_iam_role_policy_attachment.eks_cni": (
            f"kyobo-{session}-node", "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
        ),
        "aws_iam_role_policy_attachment.eks_worker_node": (
            f"kyobo-{session}-node", "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
        ),
    }
    expected_tags = {"Project": "kyobo-platform-live-lab", "Session": session, "Approval": approval}
    unsupported_actions = []
    for address, item in aws_changes.items():
        change = item.get("change", {})
        actions = change.get("actions", [])
        if actions == ["create"]:
            continue
        if actions != ["no-op"]:
            unsupported_actions.append(address)
            continue

        before = change.get("before")
        after = change.get("after")
        planned = by_address.get(address, {}).get("values")
        if not isinstance(before, dict) or not isinstance(after, dict) or before != after or after != planned:
            raise ValueError(f"pre-existing AWS resource does not match refreshed, unchanged plan state: {address}")

        tags = before.get("tags_all") or before.get("tags") or {}
        if tags:
            if any(tags.get(key) != value for key, value in expected_tags.items()):
                raise ValueError(f"pre-existing AWS resource is outside the approved session tags: {address}")
            continue

        if item.get("type") == "aws_iam_role_policy_attachment":
            expected_attachment = expected_attachments.get(address)
            if expected_attachment is None or (before.get("role"), before.get("policy_arn")) != expected_attachment:
                raise ValueError(f"untagged IAM attachment is outside the approved session roles: {address}")
            continue

        association = re.fullmatch(r"aws_route_table_association\.(private_db|public)\[([01])\]", address)
        if item.get("type") == "aws_route_table_association" and association:
            subnet_group, index = association.groups()
            route_table = by_address.get(f"aws_route_table.{subnet_group}", {}).get("values") or {}
            subnet = by_address.get(f"aws_subnet.{subnet_group}[{index}]", {}).get("values") or {}
            if before.get("route_table_id") != route_table.get("id") or before.get("subnet_id") != subnet.get("id"):
                raise ValueError(f"untagged route-table association is outside the approved VPC subnets: {address}")
            continue

        raise ValueError(f"pre-existing untagged AWS resource has no approved ownership proof: {address}")

    if unsupported_actions:
        raise ValueError(f"plan contains AWS actions other than create/no-op: {sorted(unsupported_actions)}")
    return sorted(item.get("address", "") for item in managed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan_json")
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--approval", required=True)
    parser.add_argument("--budget", type=float, default=5.5)
    parser.add_argument("--hours", type=float, default=3)
    parser.add_argument("--reserve", type=float, default=1)
    parser.add_argument("--requests", type=int, default=200_000)
    args = parser.parse_args()
    try:
        resources = validate(args.plan_json, args.account, args.region, args.session, args.approval,
                             args.budget, args.hours, args.reserve, args.requests)
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(f"Validated {len(resources)} AWS plan resources for {args.session} in {args.region}.")
    for address in resources:
        print(f"  {address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
