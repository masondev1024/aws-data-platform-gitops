"""Offline tests: fake AWS clients only, never credentials or remote calls."""

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("capture_cloudwatch", ROOT / "platform/live-lab/scripts/capture_cloudwatch.py")
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)
ACCOUNT = "854745312525"
ALB = f"arn:aws:elasticloadbalancing:ap-northeast-2:{ACCOUNT}:loadbalancer/app/lab/0123456789abcdef"
START = 1_800_000_000


def argv(output="unused.json"):
    return ["--profile", "develope-test", "--account", ACCOUNT, "--region", "ap-northeast-2",
            "--session", "session01", "--approval", "SS0-test", "--alb-arn", ALB,
            "--start", str(START), "--end", str(START + 120), "--output", str(output)]


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(cw.time, "time", lambda: START + 3600)


class FakeAWS:
    def __init__(self, bad=None, pages=None):
        self.calls = []
        self.bad = bad
        self.pages = iter(pages if pages is not None else [{"MetricDataResults": []}])

    def get_caller_identity(self):
        self.calls.append(("sts", {}))
        return {"Account": "000000000000" if self.bad == "account" else ACCOUNT}

    def describe_db_instances(self, **kwargs):
        self.calls.append(("describe_rds", kwargs))
        identifier = kwargs["DBInstanceIdentifier"]
        return {"DBInstances": [{"DBInstanceIdentifier": identifier,
                                 "DBInstanceArn": f"arn:aws:rds:ap-northeast-2:{ACCOUNT}:db:{identifier}"}]}

    def tags(self, resource):
        tags = {"Project": "kyobo-platform-live-lab", "Session": "session01", "Approval": "SS0-test"}
        if self.bad and self.bad in resource:
            tags["Approval"] = "other"
        return [{"Key": k, "Value": v} for k, v in tags.items()]

    def list_tags_for_resource(self, **kwargs):
        self.calls.append(("rds_tags", kwargs))
        return {"TagList": self.tags(kwargs["ResourceName"])}

    def describe_load_balancers(self, **kwargs):
        self.calls.append(("describe_alb", kwargs))
        return {"LoadBalancers": [{"LoadBalancerArn": ALB, "Type": "application"}]}

    def describe_tags(self, **kwargs):
        self.calls.append(("alb_tags", kwargs))
        return {"TagDescriptions": [{"ResourceArn": ALB, "Tags": self.tags(ALB)}]}

    def get_metric_data(self, **kwargs):
        self.calls.append(("metrics", kwargs))
        return next(self.pages)

    def clients(self):
        return dict.fromkeys(("sts", "rds", "elbv2", "cloudwatch"), self)


def row(key="m0", values=(0,), stamps=(START,), status="Complete"):
    return {"Id": key, "StatusCode": status, "Values": list(values),
            "Timestamps": [datetime.fromtimestamp(t, timezone.utc) for t in stamps]}


def test_identity_and_all_three_tags_precede_single_metric_batch():
    aws = FakeAWS(pages=[{"MetricDataResults": [row()]}])
    report = cw.capture(cw.arguments(argv()), aws.clients())
    assert [name for name, _ in aws.calls] == ["sts", "describe_rds", "rds_tags", "describe_rds", "rds_tags",
                                              "describe_alb", "alb_tags", "metrics"]
    request = aws.calls[-1][1]
    queries = request["MetricDataQueries"]
    assert len(queries) == 19
    assert request["ScanBy"] == "TimestampAscending"
    assert request["MaxDatapoints"] == 19 * 180
    assert all(q["MetricStat"]["Period"] == 60 for q in queries)
    lag = [q for q in queries if q["MetricStat"]["Metric"]["MetricName"] == "ReplicaLag"]
    assert len(lag) == 1 and lag[0]["MetricStat"]["Stat"] == "Maximum"
    assert lag[0]["MetricStat"]["Metric"]["Dimensions"][0]["Value"].endswith("-reader")
    latencies = [q for q in queries if q["MetricStat"]["Metric"]["MetricName"] == "TargetResponseTime"]
    assert {q["MetricStat"]["Stat"] for q in latencies} == {"p95", "Average"}
    assert latencies[0]["MetricStat"]["Metric"]["Dimensions"] == [{"Name": "LoadBalancer", "Value": "app/lab/0123456789abcdef"}]
    assert report["metrics"][0]["buckets"] == [
        {"epoch": START, "status": "observed", "value": 0},
        {"epoch": START + 60, "status": "missing", "value": None},
    ]
    assert report["metrics"][1]["status"] == "missing"


@pytest.mark.parametrize("bad", ["account", "primary", "reader", "loadbalancer"])
def test_mismatched_identity_or_any_tag_blocks_metric_calls(bad):
    aws = FakeAWS(bad=bad)
    with pytest.raises(ValueError):
        cw.capture(cw.arguments(argv()), aws.clients())
    assert "metrics" not in [name for name, _ in aws.calls]


@pytest.mark.parametrize("flag,value", [("--region", "us-east-1"), ("--account", "bad"),
    ("--alb-arn", ALB.replace(ACCOUNT, "000000000000")), ("--session", "../other"),
    ("--start", str(START + 1)), ("--end", str(START + 10860)),
    ("--end", str(START)), ("--start", str(START - 16 * 86400))])
def test_bounds_and_scope_rejected_locally(flag, value):
    args = argv()
    args[args.index(flag) + 1] = value
    with pytest.raises(ValueError):
        cw.arguments(args)


def test_exact_three_hour_interval_allowed_but_one_extra_bucket_rejected():
    args = argv()
    args[args.index("--start") + 1] = str(START - 7200)
    args[args.index("--end") + 1] = str(START + 3600)
    parsed = cw.arguments(args)
    assert parsed.end - parsed.start == 10800
    args[args.index("--start") + 1] = str(START - 7260)
    with pytest.raises(ValueError, match="interval_exceeds_3h"):
        cw.arguments(args)


@pytest.mark.parametrize("resource", ["rds", "alb"])
def test_returned_resource_identity_is_verified_not_just_tags(resource):
    aws = FakeAWS()
    if resource == "rds":
        aws.describe_db_instances = lambda **kw: {"DBInstances": [{
            "DBInstanceIdentifier": kw["DBInstanceIdentifier"],
            "DBInstanceArn": "arn:aws:rds:ap-northeast-2:000000000000:db:other"}]}
    else:
        aws.describe_load_balancers = lambda **kw: {"LoadBalancers": [{
            "LoadBalancerArn": ALB, "Type": "network"}]}
    with pytest.raises(ValueError, match="identity_mismatch"):
        cw.capture(cw.arguments(argv()), aws.clients())
    assert not any(name == "metrics" for name, _ in aws.calls)


def test_generated_batch_validates_against_aws_sdk_schema_without_network():
    session = pytest.importorskip("botocore.session")
    from botocore.validate import validate_parameters
    aws = FakeAWS()
    cw.capture(cw.arguments(argv()), aws.clients())
    shape = session.get_session().get_service_model("cloudwatch").operation_model("GetMetricData").input_shape
    validate_parameters(aws.calls[-1][1], shape)


def test_pagination_merges_without_zero_fill():
    aws = FakeAWS(pages=[{"MetricDataResults": [row(status="PartialData")], "NextToken": "next"},
                        {"MetricDataResults": [row(values=(2,), stamps=(START + 60,))]}])
    result = cw.capture(cw.arguments(argv()), aws.clients())
    assert result["metric_pages"] == 2
    assert [p["value"] for p in result["metrics"][0]["buckets"]] == [0, 2]
    assert aws.calls[-1][1]["NextToken"] == "next"


@pytest.mark.parametrize("pages", [
    [{"MetricDataResults": [row()], "NextToken": str(n)} for n in range(3)],
    [{"MetricDataResults": [], "NextToken": "same"}] * 2,
    [{"MetricDataResults": [row(status="PartialData")]}],
    [{"MetricDataResults": [row(status="Forbidden")]}],
    [{"MetricDataResults": [row(values=(float("nan"),))]}],
    [{"MetricDataResults": [row(values=(0, 1), stamps=(START, START))]}],
    [{"MetricDataResults": [row(stamps=(START - 60,))]}],
    [{"MetricDataResults": [], "Messages": [{"Code": "bad", "Value": "secret"}]}],
])
def test_incomplete_errors_or_malformed_data_are_unknown(pages):
    aws = FakeAWS(pages=pages)
    report = cw.capture(cw.arguments(argv()), aws.clients())
    assert report["metrics"][0]["status"] == "unknown"
    assert all(p["value"] is None for p in report["metrics"][0]["buckets"])
    assert report["metric_pages"] <= 3
    assert "secret" not in json.dumps(report)


def test_page_limit_does_not_follow_fourth_page():
    aws = FakeAWS(pages=[{"MetricDataResults": [], "NextToken": str(n)} for n in range(5)])
    report = cw.capture(cw.arguments(argv()), aws.clients())
    assert report["metric_pages"] == 3
    assert report["metrics"][0]["reason"] == "pagination_limit"


def test_metric_error_does_not_poison_independent_average():
    aws = FakeAWS(pages=[{"MetricDataResults": [row("m14", status="Forbidden"), row("m15", values=(0.25,))]}])
    report = cw.capture(cw.arguments(argv()), aws.clients())
    assert report["metrics"][14]["Metric"]["MetricName"] == "TargetResponseTime"
    assert report["metrics"][14]["status"] == "unknown"
    assert report["metrics"][15]["Stat"] == "Average"
    assert report["metrics"][15]["buckets"][0]["value"] == 0.25


def test_sdk_error_unknown_and_never_prints_provider_details():
    aws = FakeAWS(pages=[])
    report = cw.capture(cw.arguments(argv()), aws.clients())
    assert all(m["status"] == "unknown" for m in report["metrics"])


def test_output_private_no_overwrite_or_symlink_and_no_aws_before_reservation(monkeypatch, tmp_path):
    aws = FakeAWS()
    monkeypatch.setattr(cw, "aws_clients", lambda args: aws.clients())
    output = tmp_path / "evidence.json"
    assert cw.main(argv(output)) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    original, calls = output.read_text(), len(aws.calls)
    assert cw.main(argv(output)) == 2
    link = tmp_path / "link.json"
    link.symlink_to(output)
    assert cw.main(argv(link)) == 2
    assert output.read_text() == original and len(aws.calls) == calls


def test_blocked_verification_is_saved_safely(monkeypatch, tmp_path):
    monkeypatch.setattr(cw, "aws_clients", lambda args: FakeAWS(bad="reader").clients())
    output = tmp_path / "blocked.json"
    assert cw.main(argv(output)) == 2
    assert json.loads(output.read_text())["status"] == "blocked"


def test_sdk_profile_region_timeout_and_retry_bounds(monkeypatch):
    seen = []
    class Session:
        def __init__(self, **kwargs):
            seen.append(kwargs)
        def client(self, name, **kwargs):
            seen.append((name, kwargs))
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=Session))
    monkeypatch.setitem(sys.modules, "botocore.config", SimpleNamespace(Config=lambda **kwargs: kwargs))
    cw.aws_clients(cw.arguments(argv()))
    assert seen[0] == {"profile_name": "develope-test", "region_name": "ap-northeast-2"}
    assert len(seen) == 5
    assert all(item[1]["config"]["retries"]["total_max_attempts"] == 1 for item in seen[1:])
