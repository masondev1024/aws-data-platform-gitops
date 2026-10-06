"""Fail closed before exposing a new image to canary traffic."""

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "precanary_gate", ROOT / "platform/live-lab/scripts/precanary_gate.py"
)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def baseline_files(tmp_path):
    session = "live-261006-03"
    run_id = "baseline-26100603"
    summary = {
        "mode": "canary-apply",
        "run_id": run_id[-24:],
        "planned_http_requests": 15600,
        "started_at": "2026-10-06T07:00:00Z",
        "metrics": {
            "http_reqs": {"values": {"count": 15600}, "thresholds": {"count==15600": {"ok": True}}},
            "raffle_canary_apply_attempts": {"thresholds": {"count>=3900": {"ok": True}}},
            "raffle_canary_apply_successes": {"thresholds": {"count>=3900": {"ok": True}}},
            "http_req_duration{endpoint:apply}": {"thresholds": {
                "p(95)<500": {"ok": True}, "p(99)<1500": {"ok": True}}},
            "dropped_iterations": {"thresholds": {"count==0": {"ok": True}}},
            "http_req_failed": {"thresholds": {"rate<0.01": {"ok": True}}},
        },
    }
    ledger = {
        "schema_version": 1,
        "session_id": session,
        "request_ceiling": 200000,
        "runs": [{"run_id": run_id, "mode": "canary-apply", "status": "completed",
                  "exit_code": 0, "planned_requests": 15600, "actual_requests": 15600,
                  "reserved_at": "2026-10-06T06:59:59+00:00",
                  "completed_at": "2026-10-06T07:13:05+00:00"}],
    }
    for name, document in ((f"k6-{run_id}.json", summary),
                           (f"request-ledger-{session}.json", ledger)):
        path = tmp_path / name
        path.write_text(json.dumps(document))
        path.chmod(0o600)
    return session, run_id, summary, ledger


def test_precanary_requires_recent_successful_full_baseline(tmp_path):
    session, run_id, summary, ledger = baseline_files(tmp_path)
    now = datetime(2026, 10, 6, 7, 14, tzinfo=timezone.utc)

    report = gate.verify_baseline_evidence(tmp_path, session, run_id, now)
    assert report["actual_requests"] == 15600

    summary["metrics"]["http_req_duration{endpoint:apply}"]["thresholds"]["p(95)<500"]["ok"] = False
    (tmp_path / f"k6-{run_id}.json").write_text(json.dumps(summary))
    with pytest.raises(gate.GateError, match="baseline_threshold_failed"):
        gate.verify_baseline_evidence(tmp_path, session, run_id, now)

    summary["metrics"]["http_req_duration{endpoint:apply}"]["thresholds"]["p(95)<500"]["ok"] = True
    (tmp_path / f"k6-{run_id}.json").write_text(json.dumps(summary))
    ledger["runs"][0]["status"] = "failed"
    (tmp_path / f"request-ledger-{session}.json").write_text(json.dumps(ledger))
    with pytest.raises(gate.GateError, match="baseline_run_not_completed"):
        gate.verify_baseline_evidence(tmp_path, session, run_id, now)


def test_precanary_rejects_stale_baseline_and_wrong_session(tmp_path):
    session, run_id, _, _ = baseline_files(tmp_path)
    with pytest.raises(gate.GateError, match="baseline_stale"):
        gate.verify_baseline_evidence(
            tmp_path, session, run_id, datetime(2026, 10, 6, 7, 30, tzinfo=timezone.utc))
    with pytest.raises(gate.GateError, match="baseline_run_not_completed"):
        gate.verify_baseline_evidence(
            tmp_path, "live-261006-04", run_id, datetime(2026, 10, 6, 7, 14, tzinfo=timezone.utc))


def stable_snapshot():
    arn = ("arn:aws:elasticloadbalancing:ap-northeast-2:854745312525:"
           "targetgroup/k8s-platform-stable/1234567890abcdef")
    alb = ("arn:aws:elasticloadbalancing:ap-northeast-2:854745312525:"
           "loadbalancer/app/k8s-platform-alb/0123456789abcdef")
    hpa = {"metadata": {"name": "data-pipeline-hpa", "namespace": "platform-validation"},
           "spec": {"minReplicas": 4, "maxReplicas": 4},
           "status": {"currentReplicas": 4, "desiredReplicas": 4}}
    rollout = {"metadata": {"name": "data-pipeline-rollout", "namespace": "platform-validation"},
               "status": {"phase": "Healthy", "readyReplicas": 4, "availableReplicas": 4}}
    binding = {"metadata": {"namespace": "platform-validation"},
               "spec": {"serviceRef": {"name": "data-pipeline-svc-stable", "port": 80},
                        "targetType": "ip", "targetGroupARN": arn}}
    group = {"TargetGroupArn": arn, "TargetType": "ip", "Protocol": "HTTP", "Port": 8080,
             "LoadBalancerArns": [alb], "VpcId": "vpc-0123456789abcdef0"}
    tags = {"Project": "kyobo-platform-live-lab", "Session": "live-261006-03",
            "Approval": "SS0-20261006-codex-live-lab",
            "elbv2.k8s.aws/cluster": "kyobo-live-261006-03",
            "ingress.k8s.aws/resource":
                "platform-validation/data-pipeline-ingress-data-pipeline-svc-stable:80"}
    targets = [{"Target": {"Id": f"10.0.0.{i}"}, "TargetHealth": {"State": "healthy"}}
               for i in range(1, 5)]
    return hpa, rollout, [binding], group, tags, targets, alb


def test_precanary_requires_four_healthy_owned_stable_targets():
    hpa, rollout, bindings, group, tags, targets, alb = stable_snapshot()
    args = {"account": "854745312525", "region": "ap-northeast-2",
            "session": "live-261006-03", "approval": "SS0-20261006-codex-live-lab",
            "cluster": "kyobo-live-261006-03", "alb_arn": alb, "vpc_id": group["VpcId"]}
    assert gate.validate_stable_capacity(
        hpa, rollout, bindings, group, tags, targets, **args)["healthy_targets"] == 4

    targets[0]["TargetHealth"]["State"] = "unhealthy"
    with pytest.raises(gate.GateError, match="stable_targets_not_healthy"):
        gate.validate_stable_capacity(hpa, rollout, bindings, group, tags, targets, **args)
    targets[0]["TargetHealth"]["State"] = "healthy"
    hpa["status"]["currentReplicas"] = 3
    with pytest.raises(gate.GateError, match="stable_hpa_not_prewarmer"):
        gate.validate_stable_capacity(hpa, rollout, bindings, group, tags, targets, **args)


def test_live_capacity_checks_bound_alb_and_stable_target_group():
    hpa, rollout, bindings, group, tags, targets, alb = stable_snapshot()
    proof = {"alb_arn": alb, "alb_dimension": alb.split(":loadbalancer/", 1)[1]}
    kwargs = {"account": "854745312525", "region": "ap-northeast-2",
              "session": "live-261006-03", "approval": "SS0-20261006-codex-live-lab",
              "cluster": "kyobo-live-261006-03"}

    def run_json(command):
        if "get" in command:
            return hpa if "hpa" in command else {"items": bindings}
        if "describe-load-balancers" in command:
            return {"LoadBalancers": [{"LoadBalancerArn": alb, "State": {"Code": "active"},
                                       "VpcId": group["VpcId"]}]}
        if "describe-target-groups" in command:
            return {"TargetGroups": [group]}
        if "describe-tags" in command:
            return {"TagDescriptions": [{"ResourceArn": group["TargetGroupArn"],
                                         "Tags": [{"Key": key, "Value": value}
                                                  for key, value in tags.items()]}]}
        if "describe-target-health" in command:
            return {"TargetHealthDescriptions": targets}
        raise AssertionError(f"unexpected read: {command}")

    assert gate.verify_live_capacity(["kubectl"], rollout, proof, run_json=run_json,
                                     **kwargs)["healthy_targets"] == 4
    with pytest.raises(gate.GateError, match="precanary_alb_scope_mismatch"):
        gate.verify_live_capacity(["kubectl"], rollout,
                                  {**proof, "alb_arn": "foreign"}, run_json=run_json, **kwargs)
