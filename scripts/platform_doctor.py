#!/usr/bin/env python3
"""Read-only, explicitly scoped observations. Empty/failed queries are not health proofs."""

import argparse
from datetime import datetime, timezone
import json
import os
import re
import subprocess


class ObservationError(Exception):
    """A safe error category, never a raw provider response containing credentials."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--execute", action="store_true", help="Execute only read-only queries; default prints a plan")
    parser.add_argument("--aws-profile")
    parser.add_argument("--account")
    parser.add_argument("--region")
    parser.add_argument("--project")
    parser.add_argument("--session")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", args.namespace):
        parser.error("namespace must be a DNS label")
    if args.context.startswith("-") or any(c in args.context for c in "\r\n\0"):
        parser.error("invalid context")
    scope = [args.aws_profile, args.account, args.region, args.project, args.session]
    if any(scope) and not all(scope):
        parser.error("AWS requires profile, account, region, project and session together")
    if args.account and not re.fullmatch(r"[0-9]{12}", args.account):
        parser.error("account must be 12 digits")
    for value in (args.aws_profile, args.region, args.project, args.session):
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            parser.error("AWS scope values must contain only letters, digits, dot, underscore, hyphen")
    return args


def run_json(command):
    # Callers build an allowlisted argv, never a shell command or user-provided query.
    environment = {**os.environ, "AWS_PAGER": "", "AWS_CLI_AUTO_PROMPT": "off"}
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=20, env=environment)
    except subprocess.TimeoutExpired as exc:
        raise ObservationError("timeout") from exc
    except FileNotFoundError as exc:
        raise ObservationError("tool_missing") from exc
    if result.returncode:
        message = result.stderr.lower()
        category = "query_failed"
        if any(s in message for s in ("expired", "sso", "credential", "unauthorized")):
            category = "authentication_failed"
        elif any(s in message for s in ("accessdenied", "access denied", "forbidden")):
            category = "access_denied"
        raise ObservationError(category)
    try:
        document = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise ObservationError("invalid_json") from exc
    if not isinstance(document, dict):
        raise ObservationError("invalid_shape")
    return document


def rows(document, key):
    if not isinstance(document, dict):
        raise ObservationError("invalid_shape")
    metadata = document.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ObservationError("invalid_shape")
    if any(document.get(k) for k in ("NextToken", "nextToken", "NextMarker", "PaginationToken")) or metadata.get("continue"):
        raise ObservationError("partial_results")
    items = document.get(key)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ObservationError("invalid_shape")
    return items


def object_field(item, key):
    if key not in item:
        raise ObservationError("invalid_shape")
    value = item.get(key)
    if not isinstance(value, dict):
        raise ObservationError("invalid_shape")
    return value


def string_field(item, key):
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ObservationError("invalid_shape")
    return value


def pod_summary(item):
    metadata, status = object_field(item, "metadata"), object_field(item, "status")
    labels = metadata.get("labels", {})
    if not isinstance(labels, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()):
        raise ObservationError("invalid_shape")
    conditions = rows({"items": status.get("conditions", [])}, "items")
    containers = rows({"items": status.get("containerStatuses", [])}, "items")
    counts = [c.get("restartCount", 0) for c in containers]
    if any(type(n) is not int or n < 0 for n in counts):
        raise ObservationError("invalid_shape")
    return {"name": string_field(metadata, "name"), "phase": string_field(status, "phase"),
            "application": labels.get("app") == "data-pipeline-app",
            "ready": any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions),
            "restarts": sum(counts)}


def rollout_summary(item):
    metadata, status = object_field(item, "metadata"), object_field(item, "status")
    return {"name": string_field(metadata, "name"), "phase": string_field(status, "phase")}


def analysis_run_summary(item):
    metadata, status = object_field(item, "metadata"), object_field(item, "status")
    return {"name": string_field(metadata, "name"), "phase": string_field(status, "phase")}


def event_summary(item):
    event_type = string_field(item, "type")
    reason = string_field(item, "reason")
    count = item.get("count", 0)
    if type(count) is not int or count < 0:
        raise ObservationError("invalid_shape")
    return {"type": event_type, "reason": reason, "count": count}


def aws_resource_state(item, *, address: bool = False):
    if address:
        return "allocated"
    if "State" not in item:
        raise ObservationError("invalid_shape")
    state = item.get("State")
    if isinstance(state, dict):
        state = state.get("Name")
    if not isinstance(state, str) or not state or state == "unknown":
        raise ObservationError("invalid_shape")
    return state


def observe(runner, command, extract):
    try:
        return {"status": "observed", "items": extract(runner(command))}
    except ObservationError as exc:
        return {"status": "unknown", "error": str(exc), "items": None}


def collect(args, runner=run_json):
    base = ["kubectl", "--context", args.context, "--namespace", args.namespace, "--request-timeout=10s", "get"]
    commands = {name: base + [resource, "-o", "json"] for name, resource in
                (("pods", "pods"), ("rollouts", "rollouts.argoproj.io"),
                 ("analysisruns", "analysisruns.argoproj.io"), ("events", "events"))}
    aws = None
    if args.aws_profile:
        aws = ["aws", "--profile", args.aws_profile, "--region", args.region, "--output", "json", "--cli-connect-timeout", "5", "--cli-read-timeout", "10"]
        commands["aws_identity"] = aws + ["sts", "get-caller-identity"]
    report = {
        "schema_version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live_read_only" if args.execute else "plan", "status": "not_observed", "exit_code": 0,
        "scope": {"context": args.context, "namespace": args.namespace, "account": args.account,
                  "region": args.region, "project": args.project, "session": args.session},
        "cost": {"status": "unmeasured", "amount": None},
        "limitations": ["Not a cleanup-completeness or authorization proof.",
                        "AWS inventory covers exact Project/Session-tagged resources for the live-lab resource types; untagged controller children and resources outside the selected account/region can be missed.",
                        "AWS billing/cost is not queried; an empty resource inventory is not proof that no charges remain.",
                        "No live HTTP, p95, 5xx, DB parity or Prometheus query is performed.",
                        "Point-in-time observations; stale Kubernetes status may differ from current service health."],
        "commands": commands, "observations": {},
    }
    if not args.execute:
        return report
    results = report["observations"]
    results["pods"] = observe(runner, commands["pods"], lambda d: [pod_summary(i) for i in rows(d, "items")])
    results["rollouts"] = observe(runner, commands["rollouts"], lambda d: [rollout_summary(i) for i in rows(d, "items")])
    results["analysisruns"] = observe(runner, commands["analysisruns"], lambda d: [analysis_run_summary(i) for i in rows(d, "items")])
    results["events"] = observe(runner, commands["events"], lambda d: [event_summary(i) for i in rows(d, "items")])
    if aws:
        try:
            identity = runner(commands["aws_identity"])
            if identity.get("Account") != args.account:
                raise ObservationError("account_mismatch")
            results["aws_identity"] = {"status": "observed", "account": args.account}
        except ObservationError as exc:
            results["aws_identity"] = {"status": "unknown", "error": str(exc)}
        if results["aws_identity"]["status"] == "observed":
            collect_aws(args, aws, runner, results, commands)
    unknown = any(result["status"] == "unknown" for result in results.values())
    unknown |= not results["pods"].get("items") or not results["rollouts"].get("items")
    attention = any(not p["ready"] or p["restarts"] for p in results["pods"].get("items") or [])
    attention |= any(r["phase"] != "Healthy" for r in results["rollouts"].get("items") or [])
    attention |= any(r["phase"] in {"Failed", "Error", "Inconclusive"} for r in results["analysisruns"].get("items") or [])
    attention |= any(e["type"] == "Warning" for e in results["events"].get("items") or [])
    report["status"] = "unknown" if unknown else "attention" if attention else "observed_ok"
    report["exit_code"] = 2 if unknown else 1 if attention else 0
    return report


def collect_aws(args, aws, runner, results, commands):
    filters = ["--filters", f"Name=tag:Project,Values={args.project}", f"Name=tag:Session,Values={args.session}"]
    specs = (("nat", "describe-nat-gateways", "NatGateways", "NatGatewayId"),
             ("instances", "describe-instances", "Reservations", "InstanceId"),
             ("addresses", "describe-addresses", "Addresses", "AllocationId"),
             ("volumes", "describe-volumes", "Volumes", "VolumeId"))
    for name, operation, key, id_key in specs:
        command = aws + ["ec2", operation] + filters
        if name != "addresses":
            command += ["--max-items", "100"]
        commands[name] = command

        def extract(document, key=key, id_key=id_key):
            items = rows(document, key)
            if key == "Reservations":
                items = [instance for reservation in items for instance in rows(reservation, "Instances")]
            output = []
            for item in items:
                identifier = item.get(id_key)
                if not isinstance(identifier, str) or not identifier:
                    raise ObservationError("invalid_shape")
                state = aws_resource_state(item, address=id_key == "AllocationId")
                output.append({"id": identifier, "state": state})
            return output
        results[name] = observe(runner, command, extract)

    tagged_resource_types = {
        "eks_clusters": "eks:cluster",
        "eks_nodegroups": "eks:nodegroup",
        "rds_instances": "rds:db",
        "ecr_repositories": "ecr:repository",
        "log_groups": "logs:log-group",
        "secrets": "secretsmanager:secret",
        "waf_acls": "wafv2:webacl",
        "albs": "elasticloadbalancing:loadbalancer",
        "target_groups": "elasticloadbalancing:targetgroup",
        "security_groups": "ec2:security-group",
        "vpcs": "ec2:vpc",
        "subnets": "ec2:subnet",
    }
    arn_resource_fragments = {
        "eks_clusters": ":cluster/",
        "eks_nodegroups": ":nodegroup/",
        "rds_instances": ":db:",
        "ecr_repositories": ":repository/",
        "log_groups": ":log-group:",
        "secrets": ":secret:",
        "waf_acls": ":regional/webacl/",
        "albs": ":loadbalancer/app/",
        "target_groups": ":targetgroup/",
        "security_groups": ":security-group/",
        "vpcs": ":vpc/",
        "subnets": ":subnet/",
    }

    def extract_tagged_resources(document, arn_fragment):
        output = []
        for item in rows(document, "ResourceTagMappingList"):
            arn = item.get("ResourceARN")
            if (not isinstance(arn, str) or f":{args.region}:{args.account}:" not in arn
                    or arn_fragment not in arn):
                raise ObservationError("invalid_shape")
            tags = item.get("Tags")
            if not isinstance(tags, list):
                raise ObservationError("invalid_shape")
            tag_map = {}
            for tag in tags:
                if not isinstance(tag, dict):
                    raise ObservationError("invalid_shape")
                key, value = tag.get("Key"), tag.get("Value")
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ObservationError("invalid_shape")
                tag_map[key] = value
            if tag_map.get("Project") != args.project or tag_map.get("Session") != args.session:
                raise ObservationError("invalid_shape")
            output.append({"arn": arn, "name": arn.rsplit("/", 1)[-1], "state": "tagged"})
        return output

    for observation, resource_type in tagged_resource_types.items():
        command = aws + [
            "resourcegroupstaggingapi", "get-resources",
            "--tag-filters", f"Key=Project,Values={args.project}", f"Key=Session,Values={args.session}",
            "--resource-type-filters", resource_type,
            "--resources-per-page", "100",
        ]
        commands[observation] = command
        results[observation] = observe(
            runner, command,
            lambda document, fragment=arn_resource_fragments[observation]:
                extract_tagged_resources(document, fragment),
        )


def markdown(report):
    lines = ["# 운영 점검", "", f"상태: {report['status']}", f"시각: {report['observed_at']}",
             f"범위: `{json.dumps(report['scope'], ensure_ascii=False)}`", "", "비용: unmeasured (청구액 조회 아님)", ""]
    for name, result in report["observations"].items():
        lines += [f"## {name}", "", "```json", json.dumps(result, ensure_ascii=False, indent=2), "```", ""]
    lines += ["## 검증 한계", ""] + [f"- {limit}" for limit in report["limitations"]]
    return "\n".join(lines) + "\n"


def main():
    args = parse_args()
    report = collect(args)
    print(markdown(report) if args.format == "markdown" else json.dumps(report, ensure_ascii=False, indent=2))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
