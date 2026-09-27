import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location("platform_doctor", Path(__file__).parents[1] / "platform_doctor.py")
doctor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doctor)


def args(*extra):
    return doctor.parse_args(["--context", "lab", "--namespace", "platform-validation", *extra])


def test_dry_run_never_invokes_commands():
    def forbidden(command):
        raise AssertionError(command)
    report = doctor.collect(args(), runner=forbidden)
    assert report["mode"] == "plan"
    assert report["status"] == "not_observed"


def test_kubernetes_health_and_redacted_events():
    def fake(command):
        resource = command[command.index("get") + 1]
        if resource == "pods":
            return {"items": [{"metadata": {"name": "api"}, "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}], "containerStatuses": [{"restartCount": 2}]}}]}
        if resource == "events":
            return {"items": [{"reason": "BackOff", "type": "Warning", "message": "secret-password", "count": 2}]}
        return {"items": [{"metadata": {"name": "api"}, "status": {"phase": "Healthy"}}]}
    report = doctor.collect(args("--execute"), runner=fake)
    assert report["status"] == "attention"
    assert "secret-password" not in json.dumps(report)
    assert report["observations"]["pods"]["items"][0]["restarts"] == 2


@pytest.mark.parametrize("bad", [{}, {"items": "not-list"}, {"items": [None]}, {"items": [], "metadata": {"continue": "more"}}])
def test_malformed_or_partial_is_unknown(bad):
    report = doctor.collect(args("--execute"), runner=lambda _: bad)
    assert report["status"] == "unknown"
    assert report["exit_code"] == 2


def test_empty_cluster_is_not_healthy():
    report = doctor.collect(args("--execute"), runner=lambda _: {"items": []})
    assert report["status"] == "unknown"


@pytest.mark.parametrize("payload", [
    {"items": [{"metadata": {"name": "api"}}]},
    {"items": [{"metadata": {"name": "api"}, "status": {}}]},
    {"items": [{"metadata": {"name": "api"}, "status": {"phase": None}}]},
])
def test_missing_or_malformed_pod_status_is_unknown(payload):
    def fake(command):
        resource = command[command.index("get") + 1]
        if resource == "pods":
            return payload
        if resource == "rollouts.argoproj.io":
            return {"items": [{"metadata": {"name": "api"}, "status": {"phase": "Healthy"}}]}
        return {"items": []}
    report = doctor.collect(args("--execute"), runner=fake)
    assert report["status"] == "unknown"
    assert report["observations"]["pods"]["status"] == "unknown"


@pytest.mark.parametrize("payload", [
    {"items": [{"metadata": {"name": "api"}}]},
    {"items": [{"metadata": {"name": "api"}, "status": {}}]},
    {"items": [{"metadata": {"name": "api"}, "status": {"phase": 200}}]},
])
def test_missing_or_malformed_rollout_phase_is_unknown(payload):
    def fake(command):
        resource = command[command.index("get") + 1]
        if resource == "pods":
            return {"items": [{"metadata": {"name": "api"}, "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}], "containerStatuses": []}}]}
        if resource == "rollouts.argoproj.io":
            return payload
        return {"items": []}
    report = doctor.collect(args("--execute"), runner=fake)
    assert report["status"] == "unknown"
    assert report["observations"]["rollouts"]["status"] == "unknown"


@pytest.mark.parametrize("event", [
    {"type": None, "reason": "BackOff", "count": 1},
    {"type": "Warning", "reason": "", "count": 1},
    {"type": "Warning", "reason": "BackOff", "count": "one"},
])
def test_malformed_events_are_unknown_not_attention(event):
    def fake(command):
        resource = command[command.index("get") + 1]
        if resource == "pods":
            return {"items": [{"metadata": {"name": "api"}, "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}], "containerStatuses": []}}]}
        if resource == "rollouts.argoproj.io":
            return {"items": [{"metadata": {"name": "api"}, "status": {"phase": "Healthy"}}]}
        return {"items": [event]}
    report = doctor.collect(args("--execute"), runner=fake)
    assert report["status"] == "unknown"
    assert report["observations"]["events"]["status"] == "unknown"


def test_failed_aws_identity_stops_all_inventory():
    calls = []
    def fake(command):
        calls.append(command)
        if command[0] == "kubectl":
            return {"items": []}
        return {"Account": "999999999999"}
    report = doctor.collect(args("--execute", "--aws-profile", "test", "--account", "123456789012", "--region", "ap-northeast-2", "--project", "test", "--session", "run1"), runner=fake)
    assert report["observations"]["aws_identity"]["error"] == "account_mismatch"
    assert all("describe" not in " ".join(c) for c in calls)


def test_aws_scope_pagination_and_unknown_not_zero_includes_alb_tag_discovery():
    calls = []
    def fake(command):
        calls.append(command)
        if command[0] == "kubectl":
            return {"items": []}
        if "get-caller-identity" in command:
            return {"Account": "123456789012"}
        if "describe-nat-gateways" in command:
            return {"NatGateways": [{"NatGatewayId": "nat-1", "State": "deleting"}]}
        if "describe-volumes" in command:
            return {"Volumes": [], "NextToken": "page2"}
        if "get-resources" in command and "elasticloadbalancing:loadbalancer" in command:
            return {"ResourceTagMappingList": [{"ResourceARN": "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:loadbalancer/app/lab/abc", "Tags": [{"Key": "Project", "Value": "test"}, {"Key": "Session", "Value": "run1"}]}]}
        if "get-resources" in command:
            return {"ResourceTagMappingList": []}
        return {"Reservations": [], "Addresses": []}
    report = doctor.collect(args("--execute", "--aws-profile", "test", "--account", "123456789012", "--region", "ap-northeast-2", "--project", "test", "--session", "run1"), runner=fake)
    assert report["observations"]["nat"]["items"][0]["state"] == "deleting"
    assert report["observations"]["volumes"]["status"] == "unknown"
    assert report["observations"]["albs"]["items"][0]["state"] == "tagged"
    assert report["cost"]["status"] == "unmeasured"
    for command in calls:
        if "describe-" in " ".join(command):
            assert "Name=tag:Project,Values=test" in command
            assert "Name=tag:Session,Values=run1" in command
        if "get-resources" in command:
            assert "Key=Project,Values=test" in command
            assert "Key=Session,Values=run1" in command
            assert command[command.index("--resource-type-filters") + 1]
            assert "get-secret-value" not in command
    assert not any(token in {"delete", "terminate-instances", "destroy", "apply"} for c in calls for token in c)


def healthy_kubernetes(command):
    resource = command[command.index("get") + 1]
    if resource == "pods":
        return {"items": [{"metadata": {"name": "api"}, "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}], "containerStatuses": []}}]}
    if resource == "rollouts.argoproj.io":
        return {"items": [{"metadata": {"name": "api"}, "status": {"phase": "Healthy"}}]}
    return {"items": []}


@pytest.mark.parametrize(("operation", "payload", "observation"), [
    ("describe-nat-gateways", {"NatGateways": [{"NatGatewayId": "nat-1"}]}, "nat"),
    ("describe-nat-gateways", {"NatGateways": [{"NatGatewayId": "nat-1", "State": "unknown"}]}, "nat"),
    ("describe-instances", {"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {}}]}]}, "instances"),
    ("describe-volumes", {"Volumes": [{"VolumeId": "vol-1", "State": None}]}, "volumes"),
])
def test_ec2_inventory_missing_or_unknown_state_is_unknown_even_when_kubernetes_is_healthy(operation, payload, observation):
    def fake(command):
        if command[0] == "kubectl":
            return healthy_kubernetes(command)
        if "get-caller-identity" in command:
            return {"Account": "123456789012"}
        if operation in command:
            return payload
        if "get-resources" in command:
            return {"ResourceTagMappingList": []}
        return {"NatGateways": [], "Reservations": [], "Addresses": [], "Volumes": []}

    report = doctor.collect(args("--execute", "--aws-profile", "test", "--account", "123456789012", "--region", "ap-northeast-2", "--project", "test", "--session", "run1"), runner=fake)

    assert report["observations"][observation]["status"] == "unknown"
    assert report["status"] == "unknown"
    assert report["exit_code"] == 2


def test_eip_state_is_explicitly_inferred_as_allocated_with_healthy_kubernetes():
    def fake(command):
        if command[0] == "kubectl":
            return healthy_kubernetes(command)
        if "get-caller-identity" in command:
            return {"Account": "123456789012"}
        if "describe-addresses" in command:
            return {"Addresses": [{"AllocationId": "eipalloc-1"}]}
        if "get-resources" in command:
            return {"ResourceTagMappingList": []}
        return {"NatGateways": [], "Reservations": [], "Volumes": []}

    report = doctor.collect(args("--execute", "--aws-profile", "test", "--account", "123456789012", "--region", "ap-northeast-2", "--project", "test", "--session", "run1"), runner=fake)

    assert report["observations"]["addresses"]["items"] == [{"id": "eipalloc-1", "state": "allocated"}]
    assert report["status"] == "observed_ok"
    assert report["exit_code"] == 0


@pytest.mark.parametrize("payload", [
    {"ResourceTagMappingList": [], "PaginationToken": "next"},
    {"ResourceTagMappingList": [{"ResourceARN": "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:loadbalancer/app/lab/abc", "Tags": [{"Key": "Project", "Value": "test"}]}]},
    {"ResourceTagMappingList": [{"ResourceARN": "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:loadbalancer/net/lab/abc", "Tags": [{"Key": "Project", "Value": "test"}, {"Key": "Session", "Value": "run1"}]}]},
])
def test_alb_inventory_partial_or_malformed_is_unknown(payload):
    def fake(command):
        if command[0] == "kubectl":
            return {"items": []}
        if "get-caller-identity" in command:
            return {"Account": "123456789012"}
        if "get-resources" in command:
            return payload
        return {"NatGateways": [], "Reservations": [], "Addresses": [], "Volumes": []}
    report = doctor.collect(args("--execute", "--aws-profile", "test", "--account", "123456789012", "--region", "ap-northeast-2", "--project", "test", "--session", "run1"), runner=fake)
    assert report["observations"]["albs"]["status"] == "unknown"
    assert report["status"] == "unknown"


@pytest.mark.parametrize("error, expected", [(subprocess.TimeoutExpired("aws", 3), "timeout"), (FileNotFoundError(), "tool_missing")])
def test_subprocess_failures_are_classified_without_details(monkeypatch, error, expected):
    def fail(*a, **kw):
        raise error
    monkeypatch.setattr(doctor.subprocess, "run", fail)
    with pytest.raises(doctor.ObservationError, match=expected):
        doctor.run_json(["aws", "sts", "get-caller-identity"])


def test_invalid_or_incomplete_scope_rejected():
    with pytest.raises(SystemExit):
        args("--aws-profile", "test")
    with pytest.raises(SystemExit):
        doctor.parse_args(["--context", "--bad", "--namespace", "x"])


def test_markdown_reports_unknown_and_scope():
    report = doctor.collect(args("--execute"), runner=lambda _: {"items": []})
    text = doctor.markdown(report)
    assert "unknown" in text and "platform-validation" in text
    assert "unmeasured" in text


def test_tag_inventory_covers_runtime_resources_without_reading_secret_values():
    calls = []

    def fake(command):
        calls.append(command)
        if command[0] == "kubectl":
            return {"items": []}
        if "get-caller-identity" in command:
            return {"Account": "123456789012"}
        return {"ResourceTagMappingList": []}

    report = doctor.collect(args("--execute", "--aws-profile", "test", "--account", "123456789012", "--region", "ap-northeast-2", "--project", "test", "--session", "run1"), runner=fake)

    filters = {
        command[command.index("--resource-type-filters") + 1]
        for command in calls
        if "--resource-type-filters" in command
    }
    assert {
        "eks:cluster", "eks:nodegroup", "rds:db", "ecr:repository", "logs:log-group",
        "secretsmanager:secret", "wafv2:webacl", "elasticloadbalancing:loadbalancer",
        "elasticloadbalancing:targetgroup", "ec2:security-group", "ec2:vpc", "ec2:subnet",
    }.issubset(filters)
    for command in calls:
        if "get-resources" in command:
            assert "Key=Project,Values=test" in command
            assert "Key=Session,Values=run1" in command
        assert "get-secret-value" not in command
    assert report["observations"]["rds_instances"]["items"] == []
    assert report["cost"]["status"] == "unmeasured"


@pytest.mark.parametrize("stderr, category", [("AccessDenied: hidden-secret", "access_denied"), ("SSO token expired: hidden-secret", "authentication_failed"), ("unavailable hidden-secret", "query_failed")])
def test_errors_never_echo_provider_output(monkeypatch, stderr, category):
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", stderr))
    with pytest.raises(doctor.ObservationError) as caught:
        doctor.run_json(["aws", "sts", "get-caller-identity"])
    assert str(caught.value) == category
    assert "hidden-secret" not in str(caught.value)


@pytest.mark.parametrize("stdout", ["not-json", "[]", "null"])
def test_invalid_provider_json(monkeypatch, stdout):
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout, ""))
    with pytest.raises(doctor.ObservationError):
        doctor.run_json(["kubectl", "get", "pods"])


def test_output_is_repeatable_except_observation_time():
    first = doctor.collect(args("--execute"), runner=lambda _: {"items": []})
    second = doctor.collect(args("--execute"), runner=lambda _: {"items": []})
    first.pop("observed_at")
    second.pop("observed_at")
    assert first == second
