"""Read-only, session-scoped stable-capacity and baseline gate before a canary sync."""

from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re


class GateError(ValueError):
    """The stable baseline is not proven safe for canary exposure."""


def require(condition, reason):
    if not condition:
        raise GateError(reason)


def private_json(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o777 == 0o600,
            "baseline_run_not_completed")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError("baseline_evidence_unreadable") from exc


REQUIRED_THRESHOLDS = {
    "http_reqs": ("count==15600",),
    "raffle_canary_apply_attempts": ("count>=3900",),
    "raffle_canary_apply_successes": ("count>=3900",),
    "http_req_duration{endpoint:apply}": ("p(95)<500", "p(99)<1500"),
    "dropped_iterations": ("count==0",),
    "http_req_failed": ("rate<0.01",),
}


def verify_baseline_evidence(evidence_dir: Path, session: str, run_id: str,
                             now: datetime | None = None) -> dict:
    require(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{5,40}", session)), "invalid_baseline_session")
    require(bool(re.fullmatch(r"[A-Za-z0-9_.:-]{6,64}", run_id)), "invalid_baseline_run_id")
    now = now or datetime.now(timezone.utc)
    require(now.tzinfo is not None, "baseline_clock_missing_timezone")
    ledger = private_json(evidence_dir / f"request-ledger-{session}.json")
    summary = private_json(evidence_dir / f"k6-{run_id}.json")
    require(isinstance(ledger, dict) and isinstance(summary, dict), "baseline_evidence_unreadable")
    rows = ledger.get("runs", [])
    require(isinstance(rows, list), "baseline_run_not_completed")
    matches = [row for row in rows if isinstance(row, dict) and row.get("run_id") == run_id]
    require(ledger.get("schema_version") == 1 and ledger.get("session_id") == session and
            ledger.get("request_ceiling") == 200000 and len(matches) == 1,
            "baseline_run_not_completed")
    run = matches[0]
    require(run.get("mode") == "canary-apply" and run.get("status") == "completed" and
            run.get("exit_code") == 0 and run.get("planned_requests") == 15600 and
            run.get("actual_requests") == 15600, "baseline_run_not_completed")
    require(summary.get("mode") == "canary-apply" and
            summary.get("run_id") == re.sub(r"[^A-Za-z0-9_-]", "", run_id)[-24:] and
            summary.get("planned_http_requests") == 15600 and
            summary.get("metrics", {}).get("http_reqs", {}).get("values", {}).get("count") == 15600,
            "baseline_summary_mismatch")
    try:
        started = datetime.fromisoformat(summary["started_at"].replace("Z", "+00:00"))
        reserved = datetime.fromisoformat(run["reserved_at"].replace("Z", "+00:00"))
        completed = datetime.fromisoformat(run["completed_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise GateError("baseline_time_invalid") from exc
    require(all(item.tzinfo is not None for item in (started, reserved, completed)) and
            reserved <= started < completed <= now, "baseline_time_invalid")
    require((completed - started).total_seconds() >= 13 * 60, "baseline_duration_short")
    require(0 <= (now - completed).total_seconds() <= 900, "baseline_stale")
    metrics = summary.get("metrics", {})
    require(isinstance(metrics, dict), "baseline_threshold_failed")
    for metric_name, thresholds in REQUIRED_THRESHOLDS.items():
        row = metrics.get(metric_name, {})
        actual = row.get("thresholds", {}) if isinstance(row, dict) else {}
        require(isinstance(actual, dict) and all(isinstance(actual.get(key), dict) and
                actual[key].get("ok") is True for key in thresholds) and
                all(isinstance(item, dict) and item.get("ok") is True for item in actual.values()),
                "baseline_threshold_failed")
    return {"run_id": run_id, "actual_requests": 15600,
            "completed_at": completed.isoformat(), "thresholds": "passed"}


def validate_stable_capacity(hpa: dict, rollout: dict, bindings: list, target_group: dict,
                             tags: dict, target_health: list, *, account: str, region: str,
                             session: str, approval: str, cluster: str, alb_arn: str,
                             vpc_id: str) -> dict:
    require(hpa.get("metadata", {}).get("name") == "data-pipeline-hpa" and
            hpa.get("metadata", {}).get("namespace") == "platform-validation" and
            hpa.get("spec", {}).get("minReplicas") == 4 and
            hpa.get("spec", {}).get("maxReplicas") == 4 and
            hpa.get("status", {}).get("currentReplicas") == 4 and
            hpa.get("status", {}).get("desiredReplicas") == 4,
            "stable_hpa_not_prewarmer")
    require(rollout.get("metadata", {}).get("name") == "data-pipeline-rollout" and
            rollout.get("metadata", {}).get("namespace") == "platform-validation" and
            rollout.get("status", {}).get("phase") == "Healthy" and
            rollout.get("status", {}).get("readyReplicas", 0) >= 4 and
            rollout.get("status", {}).get("availableReplicas", 0) >= 4,
            "stable_rollout_not_healthy")
    matches = [row for row in bindings if row.get("spec", {}).get("serviceRef", {}).get("name") ==
               "data-pipeline-svc-stable"]
    require(len(matches) == 1, "stable_target_group_binding_ambiguous")
    binding = matches[0]
    expected_prefix = f"arn:aws:elasticloadbalancing:{region}:{account}:targetgroup/"
    arn = binding.get("spec", {}).get("targetGroupARN", "")
    require(binding.get("metadata", {}).get("namespace") == "platform-validation" and
            binding.get("spec", {}).get("serviceRef", {}).get("port") == 80 and
            binding.get("spec", {}).get("targetType") == "ip" and
            bool(re.fullmatch(re.escape(expected_prefix) + r"[A-Za-z0-9-]{1,32}/[a-f0-9]{16}", arn)),
            "stable_target_group_binding_mismatch")
    require(target_group.get("TargetGroupArn") == arn and
            target_group.get("TargetType") == "ip" and
            target_group.get("Protocol") == "HTTP" and
            target_group.get("Port") == 8080 and
            target_group.get("VpcId") == vpc_id and
            target_group.get("LoadBalancerArns") == [alb_arn],
            "stable_target_group_not_attached")
    expected_tags = {"Project": "kyobo-platform-live-lab", "Session": session,
                     "Approval": approval, "elbv2.k8s.aws/cluster": cluster,
                     "ingress.k8s.aws/resource":
                         "platform-validation/data-pipeline-ingress-data-pipeline-svc-stable:80"}
    require(all(tags.get(key) == value for key, value in expected_tags.items()),
            "stable_target_group_ownership_mismatch")
    require(len(target_health) == 4 and
            all(row.get("TargetHealth", {}).get("State") == "healthy" for row in target_health),
            "stable_targets_not_healthy")
    addresses = [row.get("Target", {}).get("Id", "") for row in target_health]
    try:
        require(len(set(addresses)) == 4 and
                all(ipaddress.ip_address(value).version == 4 for value in addresses),
                "stable_targets_not_healthy")
    except ValueError as exc:
        raise GateError("stable_targets_not_healthy") from exc
    return {"stable_target_group_arn": arn, "healthy_targets": 4,
            "hpa_current_replicas": 4}


def verify_live_capacity(kube: list[str], rollout: dict, proof: dict, *, account: str,
                         region: str, session: str, approval: str, cluster: str,
                         run_json) -> dict:
    expected_alb = (f"arn:aws:elasticloadbalancing:{region}:{account}:loadbalancer/"
                    f"{proof.get('alb_dimension', '')}")
    require(proof.get("alb_arn") == expected_alb and bool(re.fullmatch(
        rf"arn:aws:elasticloadbalancing:{region}:{account}:loadbalancer/app/"
        r"[A-Za-z0-9-]{1,32}/[a-f0-9]{16}", expected_alb)), "precanary_alb_scope_mismatch")
    namespace = ["-n", "platform-validation"]
    hpa = run_json(kube + namespace + ["get", "hpa", "data-pipeline-hpa", "-o", "json"])
    bindings = run_json(kube + namespace + ["get", "targetgroupbindings.elbv2.k8s.aws", "-o", "json"])["items"]
    matches = [row for row in bindings if row.get("spec", {}).get("serviceRef", {}).get("name") ==
               "data-pipeline-svc-stable"]
    require(len(matches) == 1, "stable_target_group_binding_ambiguous")
    arn = matches[0].get("spec", {}).get("targetGroupARN", "")
    prefix = f"arn:aws:elasticloadbalancing:{region}:{account}:targetgroup/"
    require(bool(re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9-]{1,32}/[a-f0-9]{16}", arn)),
            "stable_target_group_binding_mismatch")

    def aws(*parts):
        return run_json(["aws", "--region", region, "--output", "json", "elbv2", *parts])

    alb_rows = aws("describe-load-balancers", "--load-balancer-arns", proof["alb_arn"])["LoadBalancers"]
    require(len(alb_rows) == 1 and alb_rows[0].get("LoadBalancerArn") == proof["alb_arn"] and
            alb_rows[0].get("State", {}).get("Code") == "active", "stable_alb_not_active")
    groups = aws("describe-target-groups", "--target-group-arns", arn)["TargetGroups"]
    require(len(groups) == 1, "stable_target_group_ambiguous")
    tag_rows = aws("describe-tags", "--resource-arns", arn)["TagDescriptions"]
    require(len(tag_rows) == 1 and tag_rows[0].get("ResourceArn") == arn,
            "stable_target_group_ownership_mismatch")
    tags = {row["Key"]: row["Value"] for row in tag_rows[0]["Tags"]}
    require(len(tags) == len(tag_rows[0]["Tags"]), "stable_target_group_ownership_mismatch")
    health = aws("describe-target-health", "--target-group-arn", arn)["TargetHealthDescriptions"]
    return validate_stable_capacity(hpa, rollout, bindings, groups[0], tags, health,
                                    account=account, region=region, session=session, approval=approval,
                                    cluster=cluster, alb_arn=proof["alb_arn"], vpc_id=alb_rows[0]["VpcId"])
