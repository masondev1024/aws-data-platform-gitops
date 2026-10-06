#!/usr/bin/env python3
"""Fail-closed, session-scoped evidence checks for the AWS live-lab lifecycle."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def walk_modules(module: dict):
    for resource in module.get("resources", []):
        yield resource
    for child in module.get("child_modules", []):
        yield from walk_modules(child)


def validate_state(path: str, session: str, approval: str) -> int:
    document = load(path)
    resources = list(walk_modules(document.get("values", {}).get("root_module", {})))
    managed = [resource for resource in resources if resource.get("mode") == "managed" and
               (resource.get("type", "").startswith("aws_") or resource.get("type") == "terraform_data")]
    if not managed:
        raise ValueError("Terraform state contains no managed AWS resources")
    untagged_allowed = {"aws_iam_role_policy_attachment", "aws_vpc_security_group_ingress_rule", "aws_route_table_association", "terraform_data"}
    prefix = f"kyobo-{session}"
    for resource in managed:
        kind = resource.get("type", "")
        address = resource.get("address", "")
        values = resource.get("values", {})
        tags = {**(values.get("tags_all") or {}), **(values.get("tags") or {})}
        if tags:
            expected = {"Project": "kyobo-platform-live-lab", "Session": session, "Approval": approval}
            if any(tags.get(key) != value for key, value in expected.items()):
                raise ValueError(f"state resource ownership mismatch: {address}")
            continue
        if kind not in untagged_allowed:
            raise ValueError(f"untagged state resource needs manual review: {address}")
        if kind == "terraform_data":
            identity = values.get("input", {})
            if identity.get("session_id") != session or identity.get("approval_id") != approval:
                raise ValueError("terraform approval state does not match the approved session")
        elif kind == "aws_iam_role_policy_attachment":
            if not str(values.get("role", "")).startswith(prefix):
                raise ValueError(f"IAM attachment outside this session: {address}")
        elif kind == "aws_vpc_security_group_ingress_rule":
            if "db_mysql_from_eks_nodes" not in address:
                raise ValueError(f"unexpected untagged security-group rule: {address}")
        elif kind == "aws_route_table_association":
            if "route_table_association" not in address:
                raise ValueError(f"unexpected untagged route association: {address}")
    return len(managed)


def validate_owner_tags(document: dict, expected: dict[str, str]) -> None:
    tags = document.get("Tags", document.get("tags", []))
    if "TagDescriptions" in document:
        descriptions = document["TagDescriptions"]
        if len(descriptions) != 1:
            raise ValueError("expected exactly one tagged resource")
        tags = descriptions[0].get("Tags", [])
    tag_map = {item.get("Key"): item.get("Value") for item in tags if isinstance(item, dict)}
    if any(tag_map.get(key) != value for key, value in expected.items()):
        raise ValueError("resource ownership tags do not match the exact approved session")


def write_inventory(source: str, target: str, expected: dict[str, str]) -> int:
    document = load(source)
    if document.get("PaginationToken") or document.get("PaginationTokens"):
        raise ValueError("resource inventory is paginated and incomplete")
    items = document.get("ResourceTagMappingList")
    if not isinstance(items, list):
        raise ValueError("resource inventory response shape is unknown")
    for item in items:
        validate_owner_tags(item, expected)
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "status": "tagged_residuals_found" if items else "no_tagged_residuals_observed",
        "resources": [{"arn": item.get("ResourceARN"), "tags": item.get("Tags", [])} for item in items],
        "limitations": [
            "Does not prove that untagged resources or delayed billing are absent.",
            "Not a final invoice.",
        ],
    }
    destination = Path(target)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    return len(items)


def _write_reconciliation_report(
    target: str,
    *,
    live_resources: list[dict],
    stale_tag_index_entries: list[dict],
    unresolved_resources: list[dict],
) -> None:
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "status": "reconciliation_incomplete" if unresolved_resources else (
            "tagged_residuals_found" if live_resources else "no_live_tagged_resources_observed"
        ),
        "resources": live_resources,
        "stale_tag_index_entries": stale_tag_index_entries,
        "unresolved_resources": unresolved_resources,
        "limitations": [
            "Known EC2 security-group-rule and subnet candidates were checked with service Describe APIs; unknown resource types fail closed.",
            "Resource Groups Tagging API may retain previously tagged ARNs after resource deletion.",
            "Does not prove that untagged resources or delayed billing are absent.",
            "Not a final invoice.",
        ],
    }
    destination = Path(target)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    destination.chmod(0o600)


def _verify_security_group_rule(
    arn: str, *, profile: str, region: str, aws_runner=subprocess.run
) -> tuple[bool, str]:
    """Verify an EC2 security-group rule with EC2, not the historical tag index."""
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or parts[2] != "ec2" or parts[3] != region:
        raise ValueError(f"security-group-rule ARN does not match the selected AWS region: {arn}")
    resource = parts[5]
    prefix = "security-group-rule/"
    if not resource.startswith(prefix) or not resource[len(prefix):]:
        raise ValueError(f"invalid EC2 security-group-rule ARN: {arn}")
    rule_id = resource[len(prefix):]
    command = [
        "aws", "--profile", profile, "--region", region,
        "ec2", "describe-security-group-rules", "--security-group-rule-ids", rule_id,
        "--output", "json",
    ]
    response = aws_runner(command, capture_output=True, text=True, check=False)
    if response.returncode != 0:
        error_text = f"{response.stderr}\n{response.stdout}"
        if "(InvalidSecurityGroupRuleId.NotFound)" in error_text:
            return False, "ec2_describe_security_group_rules_not_found"
        raise RuntimeError(f"could not verify EC2 security-group rule {rule_id}: {response.stderr.strip()}")
    try:
        result = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"EC2 security-group rule verification returned invalid JSON: {rule_id}") from exc
    if not isinstance(result, dict):
        raise ValueError(f"EC2 security-group rule response is not an object: {rule_id}")
    rules = result.get("SecurityGroupRules")
    if not isinstance(rules, list):
        raise ValueError(f"EC2 security-group rule response shape is unknown: {rule_id}")
    if result.get("NextToken"):
        raise ValueError(f"EC2 security-group rule response is paginated: {rule_id}")
    if any(not isinstance(rule, dict) or not isinstance(rule.get("SecurityGroupRuleId"), str) for rule in rules):
        raise ValueError(f"EC2 security-group rule response contains malformed entries: {rule_id}")
    matching = [rule for rule in rules if rule.get("SecurityGroupRuleId") == rule_id]
    if len(matching) == 1 and len(rules) == 1:
        return True, "ec2_describe_security_group_rules_found"
    raise ValueError(f"EC2 success response did not prove exact rule state: {rule_id}")


def _verify_secret(
    arn: str, *, profile: str, region: str, aws_runner=subprocess.run
) -> tuple[bool, str]:
    """Verify a Secrets Manager tag-index candidate with DescribeSecret."""
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or parts[1] != "aws" or parts[2] != "secretsmanager" or parts[3] != region:
        raise ValueError("Secrets Manager ARN does not match the selected AWS region")
    if not parts[5].startswith("secret:") or len(parts[5]) <= len("secret:"):
        raise ValueError("invalid Secrets Manager secret ARN")
    command = [
        "aws", "--profile", profile, "--region", region,
        "secretsmanager", "describe-secret", "--secret-id", arn, "--output", "json",
    ]
    response = aws_runner(command, capture_output=True, text=True, check=False)
    if response.returncode != 0:
        error_text = f"{response.stderr}\n{response.stdout}"
        if "(ResourceNotFoundException)" in error_text:
            return False, "secretsmanager_describe_secret_not_found"
        raise RuntimeError(f"could not verify Secrets Manager secret ARN: {response.stderr.strip()}")
    try:
        result = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Secrets Manager DescribeSecret returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("ARN") != arn:
        raise ValueError("Secrets Manager DescribeSecret did not prove the exact secret ARN")
    return True, "secretsmanager_describe_secret_found"


def _verify_subnet(
    arn: str, *, profile: str, region: str, aws_runner=subprocess.run
) -> tuple[bool, str]:
    """Distinguish a live subnet from an eventual-consistency tag-index entry."""
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or parts[2] != "ec2" or parts[3] != region:
        raise ValueError("subnet ARN does not match the selected AWS region")
    resource = parts[5]
    if not resource.startswith("subnet/"):
        raise ValueError("invalid subnet ARN")
    subnet_id = resource.removeprefix("subnet/")
    if not subnet_id.startswith("subnet-") or len(subnet_id) <= len("subnet-") or any(
        char not in "0123456789abcdef" for char in subnet_id[len("subnet-"):]
    ):
        raise ValueError("invalid subnet identifier")
    command = [
        "aws", "--profile", profile, "--region", region,
        "ec2", "describe-subnets", "--subnet-ids", subnet_id, "--output", "json",
    ]
    response = aws_runner(command, capture_output=True, text=True, check=False)
    if response.returncode != 0:
        error_text = f"{response.stderr}\n{response.stdout}"
        if "(InvalidSubnetID.NotFound)" in error_text:
            return False, "ec2_describe_subnets_not_found"
        raise RuntimeError(f"could not verify EC2 subnet {subnet_id}: {response.stderr.strip()}")
    try:
        result = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("EC2 DescribeSubnets returned invalid JSON") from exc
    subnets = result.get("Subnets") if isinstance(result, dict) else None
    if (not isinstance(subnets, list) or len(subnets) != 1 or
            not isinstance(subnets[0], dict) or subnets[0].get("SubnetId") != subnet_id or
            result.get("NextToken")):
        raise ValueError("EC2 DescribeSubnets did not prove the exact subnet state")
    return True, "ec2_describe_subnets_found"


def reconcile_inventory(
    source: str,
    target: str,
    expected: dict[str, str],
    *,
    profile: str,
    region: str,
    account_id: str,
    aws_runner=subprocess.run,
) -> dict[str, int]:
    """Reconcile tagged candidates, using authoritative service APIs where known."""
    live_resources = []
    stale_tag_index_entries = []
    unresolved_resources = []

    def summary() -> dict[str, int]:
        return {
            "live": len(live_resources),
            "stale": len(stale_tag_index_entries),
            "unresolved": len(unresolved_resources),
        }

    def persist() -> None:
        _write_reconciliation_report(
            target,
            live_resources=live_resources,
            stale_tag_index_entries=stale_tag_index_entries,
            unresolved_resources=unresolved_resources,
        )

    try:
        document = load(source)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        unresolved_resources.append({"arn": None, "verification": "tag_inventory_unreadable"})
        persist()
        return summary()
    candidates = document.get("ResourceTagMappingList")
    if not isinstance(candidates, list):
        unresolved_resources.append({"arn": None, "verification": "tag_inventory_response_invalid"})
        persist()
        return summary()
    if document.get("PaginationToken") or document.get("PaginationTokens"):
        unresolved_resources.extend(
            {"arn": candidate.get("ResourceARN") if isinstance(candidate, dict) else None,
             "verification": "tag_inventory_incomplete"}
            for candidate in candidates
        )
        if not candidates:
            unresolved_resources.append({"arn": None, "verification": "tag_inventory_incomplete"})
        persist()
        return summary()

    # The teardown wrapper checks identity too, but this CLI must remain safe when
    # invoked directly or from another script with an accidentally switched profile.
    identity_command = [
        "aws", "--profile", profile, "--region", region,
        "sts", "get-caller-identity", "--query", "Account", "--output", "text",
    ]
    try:
        identity_response = aws_runner(identity_command, capture_output=True, text=True, check=False)
    except OSError:
        identity_response = None
    caller_account = (
        identity_response.stdout.strip()
        if identity_response is not None and identity_response.returncode == 0
        else ""
    )
    identity_verified = len(caller_account) == 12 and caller_account.isdecimal() and caller_account == account_id
    if not identity_verified:
        verification = (
            "caller_account_mismatch"
            if identity_response is not None and identity_response.returncode == 0
            else "caller_identity_unverified"
        )
        identity_candidates = candidates or [None]
        unresolved_resources.extend(
            {
                "arn": candidate.get("ResourceARN") if isinstance(candidate, dict) else None,
                "verification": verification,
            }
            for candidate in identity_candidates
        )
        persist()
        return summary()

    for candidate in candidates:
        arn = candidate.get("ResourceARN") if isinstance(candidate, dict) else None
        unresolved = None
        if not isinstance(candidate, dict) or not isinstance(arn, str) or not arn:
            unresolved = "invalid_tag_inventory_candidate"
        else:
            try:
                validate_owner_tags(candidate, expected)
            except (ValueError, TypeError, KeyError):
                unresolved = "ownership_tags_mismatch"

        arn_parts = arn.split(":", 5) if isinstance(arn, str) else []
        if unresolved is None and (
            len(arn_parts) != 6 or arn_parts[0] != "arn" or arn_parts[1] != "aws"
            or arn_parts[3] != region or arn_parts[4] != account_id
        ):
            unresolved = "arn_scope_mismatch"
        if unresolved is not None:
            unresolved_resources.append({"arn": arn, "verification": unresolved})
            continue

        arn_parts = arn.split(":", 5)
        if len(arn_parts) != 6:
            unresolved_resources.append({"arn": arn, "verification": "arn_scope_mismatch"})
            continue
        service, resource = arn_parts[2], arn_parts[5]
        if service == "ec2" and resource.startswith("security-group-rule/"):
            try:
                is_live, verification = _verify_security_group_rule(
                    arn, profile=profile, region=region, aws_runner=aws_runner
                )
            except ValueError:
                unresolved_resources.append({"arn": arn, "verification": "ec2_response_ambiguous"})
                continue
            except (OSError, RuntimeError, TypeError):
                unresolved_resources.append({"arn": arn, "verification": "service_verification_failed"})
                continue
        elif service == "ec2" and resource.startswith("subnet/"):
            try:
                is_live, verification = _verify_subnet(
                    arn, profile=profile, region=region, aws_runner=aws_runner
                )
            except ValueError:
                unresolved_resources.append({"arn": arn, "verification": "ec2_response_ambiguous"})
                continue
            except (OSError, RuntimeError, TypeError):
                unresolved_resources.append({"arn": arn, "verification": "service_verification_failed"})
                continue
        elif service == "secretsmanager" and resource.startswith("secret:"):
            try:
                is_live, verification = _verify_secret(
                    arn, profile=profile, region=region, aws_runner=aws_runner
                )
            except ValueError:
                unresolved_resources.append({"arn": arn, "verification": "secretsmanager_response_ambiguous"})
                continue
            except (OSError, RuntimeError, TypeError):
                unresolved_resources.append({"arn": arn, "verification": "service_verification_failed"})
                continue
        else:
            # A tag-index omission is not proof of deletion. Unknown ARN types stay
            # fail-closed until a service-specific Describe/Get verifier is added.
            unresolved_resources.append({"arn": arn, "verification": "unsupported_resource_type"})
            continue
        record = {
            "arn": arn,
            "tags": candidate.get("Tags", []),
            "verification": verification,
        }
        if is_live:
            live_resources.append(record)
        else:
            stale_tag_index_entries.append(record)

    persist()
    return summary()


def write_status(path: str, status: str, detail: str, session: str, region: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "status": status,
        "detail": detail,
        "session_id": session,
        "region": region,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "billing_note": "resource inventory is not a final invoice",
    }
    destination.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    destination.chmod(0o600)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    state = subparsers.add_parser("validate-state")
    state.add_argument("path")
    state.add_argument("--session", required=True)
    state.add_argument("--approval", required=True)
    inventory = subparsers.add_parser("write-inventory")
    inventory.add_argument("source")
    inventory.add_argument("target")
    inventory.add_argument("--session", required=True)
    inventory.add_argument("--approval", required=True)
    inventory.add_argument("--project", default="kyobo-platform-live-lab")
    reconcile = subparsers.add_parser("reconcile-inventory")
    reconcile.add_argument("source")
    reconcile.add_argument("target")
    reconcile.add_argument("--session", required=True)
    reconcile.add_argument("--approval", required=True)
    reconcile.add_argument("--project", default="kyobo-platform-live-lab")
    reconcile.add_argument("--profile", required=True)
    reconcile.add_argument("--region", required=True)
    reconcile.add_argument("--account-id", required=True)
    tags = subparsers.add_parser("validate-tags")
    tags.add_argument("path")
    tags.add_argument("--session", required=True)
    tags.add_argument("--approval", required=True)
    tags.add_argument("--project", default="kyobo-platform-live-lab")
    status = subparsers.add_parser("write-status")
    status.add_argument("path")
    status.add_argument("value", choices=("active", "incomplete", "completed"))
    status.add_argument("detail")
    status.add_argument("--session", required=True)
    status.add_argument("--region", required=True)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "validate-state":
            count = validate_state(args.path, args.session, args.approval)
            print(f"validated {count} session-scoped Terraform resources")
        elif args.command == "write-inventory":
            expected = {"Project": args.project, "Session": args.session, "Approval": args.approval}
            count = write_inventory(args.source, args.target, expected)
            print(f"observed {count} tagged residual resources")
            return 1 if count else 0
        elif args.command == "reconcile-inventory":
            expected = {"Project": args.project, "Session": args.session, "Approval": args.approval}
            counts = reconcile_inventory(
                args.source, args.target, expected, profile=args.profile,
                region=args.region, account_id=args.account_id,
            )
            print(
                f"observed {counts['live']} live tagged residual resources; "
                f"{counts['stale']} historical tag-index entries; "
                f"{counts['unresolved']} unresolved resources"
            )
            if counts["unresolved"]:
                return 2
            return 1 if counts["live"] else 0
        elif args.command == "validate-tags":
            expected = {"Project": args.project, "Session": args.session, "Approval": args.approval}
            validate_owner_tags(load(args.path), expected)
            print("validated exact Project/Session/Approval ownership tags")
        else:
            write_status(args.path, args.value, args.detail, args.session, args.region)
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
