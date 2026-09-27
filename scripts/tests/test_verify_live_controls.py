"""Offline WAF verifier contract tests; no AWS credentials/network required."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "platform/live-lab/scripts/verify_waf_live.py"
SPEC = importlib.util.spec_from_file_location("verify_waf_live", SCRIPT)
waf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(waf)
ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
SESSION = "live-test-001"
CLUSTER = "kyobo-" + SESSION
ALB = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/lab/123abc"
ACL = f"arn:aws:wafv2:{REGION}:{ACCOUNT}:regional/webacl/{CLUSTER}-web-acl/00000000-0000-0000-0000-000000000001"
DNS = "lab-123.ap-northeast-2.elb.amazonaws.com"
ARGV = ["--account", ACCOUNT, "--region", REGION, "--session", SESSION,
        "--approval", "SS0-20260923-test", "--cluster", CLUSTER, "--alb-arn", ALB,
        "--web-acl-arn", ACL, "--url", f"https://{DNS}/healthz"]


def arguments(execute=True):
    return waf.parser().parse_args(ARGV + (["--execute"] if execute else []))


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 23, 1, 2, 3, tzinfo=timezone.utc)

    def now(self):
        self.value += timedelta(seconds=1)
        return self.value

    def sleep(self, seconds):
        self.value += timedelta(seconds=seconds)


class FakeWaf:
    def __init__(self, args, clock):
        self.clock = clock
        self.version = 0
        self.writes = []
        self.requests = []
        self.sample_queries = []
        self.hook = None
        self.no_samples = False
        self.sample_override = None
        members = {key: None for key in (
            "Name", "Id", "Scope", "DefaultAction", "Description", "Rules", "VisibilityConfig",
            "LockToken", "CustomResponseBodies", "AssociationConfig", "TokenDomains", "CaptchaConfig",
            "ChallengeConfig", "DataProtectionConfig", "OnSourceDDoSProtectionConfig", "ApplicationConfig")}

        def operation(name):
            values = members if name == "UpdateWebACL" else {
                "ResourceType": SimpleNamespace(enum=["APPLICATION_LOAD_BALANCER", "API_GATEWAY"])}
            return SimpleNamespace(input_shape=SimpleNamespace(members=values))

        self.meta = SimpleNamespace(region_name=REGION, service_model=SimpleNamespace(operation_model=operation))
        self.acl = {
            "ARN": ACL, "Name": CLUSTER + "-web-acl", "Id": ACL.rsplit("/", 1)[1],
            "DefaultAction": {"Allow": {}}, "Description": "preserve me", "Capacity": 2,
            "Rules": [{"Name": waf.RULE, "Priority": 10, "Action": {"Count": {}},
                       "Statement": {"ByteMatchStatement": {
                           "FieldToMatch": {"SingleHeader": {"Name": waf.HEADER}},
                           "PositionalConstraint": "EXACTLY", "SearchString": args.header_value.encode(),
                           "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
                       "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True,
                                            "MetricName": CLUSTER.replace("-", "") + "CustomHeaderDrill"}}],
            "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True,
                                 "MetricName": CLUSTER.replace("-", "") + "WebAcl"},
            "CustomResponseBodies": {"safe": {"ContentType": "TEXT_PLAIN", "Content": "private-content"}},
            "AssociationConfig": {"RequestBody": {"API_GATEWAY": {"DefaultSizeInspectionLimit": "KB_16"}}},
        }
        self.tags = [{"Key": key, "Value": value} for key, value in waf.expected_tags(args).items()]
        self.associated = ACL
        self.resources = [ALB]

    def get_web_acl(self, **kwargs):
        return {"WebACL": deepcopy(self.acl), "LockToken": str(self.version)}

    def get_web_acl_for_resource(self, **kwargs):
        return {"WebACL": {"ARN": self.associated}}

    def list_tags_for_resource(self, **kwargs):
        return {"TagInfoForResource": {"TagList": self.tags}}

    def list_resources_for_web_acl(self, **kwargs):
        return {"ResourceArns": self.resources if kwargs["ResourceType"] == "APPLICATION_LOAD_BALANCER" else []}

    def update_web_acl(self, **payload):
        assert payload["LockToken"] == str(self.version), "stale LockToken"
        assert payload["Scope"] == "REGIONAL"
        if self.hook:
            self.hook(payload)
        self.writes.append(deepcopy(payload))
        self.acl.update({key: deepcopy(value) for key, value in payload.items() if key not in ("Scope", "LockToken")})
        self.version += 1
        return {"NextLockToken": str(self.version)}

    def get_sampled_requests(self, **kwargs):
        self.sample_queries.append(deepcopy(kwargs))
        if self.sample_override is not None:
            return {"SampledRequests": self.sample_override}
        if self.no_samples:
            return {"SampledRequests": []}
        return {"SampledRequests": [
            {"Action": action, "Timestamp": timestamp,
             "Request": {"Method": "GET", "URI": "/healthz", "ClientIP": "sensitive-ip",
                         "Headers": [{"Name": key, "Value": value} for key, value in headers.items()]}}
            for timestamp, action, headers in self.requests if waf.HEADER in headers]}


@pytest.fixture
def lab():
    args, clock = arguments(), Clock()
    fake = FakeWaf(args, clock)
    clients = {name: Mock() for name in ("sts", "eks", "elbv2", "cloudwatch")}
    for client in clients.values():
        client.meta.region_name = REGION
    clients["wafv2"] = fake
    clients["sts"].get_caller_identity.return_value = {"Account": ACCOUNT}
    clients["eks"].describe_cluster.return_value = {"cluster": {
        "arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{CLUSTER}", "status": "ACTIVE",
        "tags": waf.expected_tags(args), "resourcesVpcConfig": {"vpcId": "vpc-test"}}}
    clients["elbv2"].describe_load_balancers.return_value = {"LoadBalancers": [{
        "LoadBalancerArn": ALB, "DNSName": DNS, "Type": "application", "State": {"Code": "active"},
        "VpcId": "vpc-test"}]}
    clients["elbv2"].describe_tags.return_value = {"TagDescriptions": [{"ResourceArn": ALB,
        "Tags": fake.tags + [{"Key": "elbv2.k8s.aws/cluster", "Value": CLUSTER}]}]}
    clients["cloudwatch"].get_metric_statistics.side_effect = lambda **kw: {
        "Datapoints": [{"Sum": 1, "Timestamp": kw["StartTime"]}]}

    def probe(a, headers):
        action = next(iter(waf.rule_from(fake.acl)["Action"]))
        fake.requests.append((clock.now(), action.upper(), deepcopy(headers)))
        return 403 if waf.HEADER in headers and action == "Block" else 200

    verifier = waf.Verifier(args, clients, probe=probe, sleep=clock.sleep, now=clock.now)
    return SimpleNamespace(args=args, clock=clock, fake=fake, clients=clients, verifier=verifier, probe=probe)


def test_default_plan_never_creates_clients(monkeypatch, capsys):
    factory = Mock(side_effect=AssertionError("network forbidden"))
    monkeypatch.setattr(waf, "make_clients", factory)
    assert waf.main(ARGV) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "offline_plan"
    factory.assert_not_called()


@pytest.mark.parametrize("description", ["", "preserve me", " ", None])
def test_description_payload_preserves_optional_config_and_sdk_contract(lab, description):
    # GetWebACL can return an empty description, but UpdateWebACL rejects it.
    # Use the real offline SDK model: the fake client does not validate lengths.
    botocore_session = pytest.importorskip("botocore.session")
    validation = pytest.importorskip("botocore.validate")
    shape = botocore_session.get_session().get_service_model("wafv2").operation_model("UpdateWebACL").input_shape
    if description is None:
        lab.fake.acl.pop("Description")
    else:
        lab.fake.acl["Description"] = description
    lab.fake.acl["TokenDomains"] = []  # Do not drop other falsy optional values.
    before = deepcopy(lab.fake.acl)
    payload = lab.verifier.payload(before, "fresh-lock-token", {"Block": {}})
    validation.validate_parameters(payload, shape)
    if description in ("", None):
        assert "Description" not in payload
    else:
        assert payload["Description"] == description
    for key in ("CustomResponseBodies", "AssociationConfig", "TokenDomains", "VisibilityConfig", "DefaultAction"):
        assert payload[key] == before[key]
    assert payload["LockToken"] == "fresh-lock-token"
    assert before == lab.fake.acl
    lab.fake.hook = lambda request: validation.validate_parameters(request, shape)
    assert lab.verifier.run()["restoration"] == "verified"
    assert lab.fake.acl == before
    assert [p["LockToken"] for p in lab.fake.writes] == ["0", "1"]


@pytest.mark.parametrize("original", ["Count", "Block"])
def test_success_restores_original_and_is_rerunnable(lab, original):
    waf.rule_from(lab.fake.acl)["Action"] = {original: {}}
    before = deepcopy(lab.fake.acl)
    for _ in range(2):
        report = lab.verifier.run()
        assert report["status"] == "passed"
        assert report["restoration"] == "verified"
        assert report["original_action"] == original.upper()
        assert report["scope"] == waf.scope_evidence(lab.args)
        assert lab.fake.acl == before
    assert all(p["sample_matches"] > 0 and p["metric_sum"] > 0 for p in report["phases"])
    encoded = json.dumps(report)
    assert "sensitive-ip" not in encoded and "private-content" not in encoded
    assert lab.args.header_value not in encoded
    for query in lab.fake.sample_queries:
        assert query["TimeWindow"]["EndTime"] <= lab.clock.value
    for phase in report["phases"]:
        started = datetime.fromisoformat(phase["started_at"])
        ended = datetime.fromisoformat(phase["ended_at"])
        assert any(query["TimeWindow"] == {
            "StartTime": (started - timedelta(seconds=1)).replace(microsecond=0),
            "EndTime": (ended + timedelta(seconds=1)).replace(microsecond=0) + timedelta(seconds=1),
        } for query in lab.fake.sample_queries)
    assert [p["LockToken"] for p in lab.fake.writes] == [str(i) for i in range(len(lab.fake.writes))]
    metrics = lab.clients["cloudwatch"].get_metric_statistics.call_args.kwargs
    assert {d["Name"]: d["Value"] for d in metrics["Dimensions"]} == {
        "WebACL": CLUSTER + "-web-acl", "Rule": CLUSTER.replace("-", "") + "CustomHeaderDrill",
        "Region": REGION}


@pytest.mark.parametrize("field,value", [
    ("account", "wrong"), ("region", "bad"), ("session", "../unsafe"), ("approval", "pending"),
    ("cluster", "production"), ("alb_arn", ALB.replace(ACCOUNT, "999999999999")),
    ("web_acl_arn", ACL.replace("regional", "global")), ("header_value", "attack\r\ninject"),
    ("url", f"https://user:secret@{DNS}/healthz"), ("url", f"https://{DNS}/write"),
    ("url", f"https://{DNS}/healthz?secret=foo"), ("url", f"https://{DNS}:8443/healthz"),
    ("attempts", 11), ("evidence_polls", 0),
])
def test_invalid_scope_arguments(lab, field, value):
    setattr(lab.args, field, value)
    with pytest.raises(waf.VerificationError):
        lab.verifier.run()
    assert not lab.fake.writes and not lab.fake.requests


@pytest.mark.parametrize("mismatch", ["account", "region", "cluster", "tags", "association", "shared", "url", "vpc", "alb_owner"])
def test_scope_mismatch_blocks_before_traffic(lab, mismatch):
    if mismatch == "account":
        lab.clients["sts"].get_caller_identity.return_value["Account"] = "999999999999"
    elif mismatch == "region":
        lab.clients["eks"].meta.region_name = "us-east-1"
    elif mismatch == "cluster":
        lab.clients["eks"].describe_cluster.return_value["cluster"]["arn"] += "-other"
    elif mismatch == "tags":
        lab.fake.tags[0]["Value"] = "production"
    elif mismatch == "association":
        lab.fake.associated = ACL + "other"
    elif mismatch == "shared":
        lab.fake.resources.append(ALB + "other")
    elif mismatch == "url":
        lab.args.url = "https://example.org/healthz"
    elif mismatch == "vpc":
        lab.clients["elbv2"].describe_load_balancers.return_value["LoadBalancers"][0]["VpcId"] = "vpc-other"
    else:
        lab.clients["elbv2"].describe_tags.return_value["TagDescriptions"][0]["Tags"][-1]["Value"] = "other"
    with pytest.raises(waf.VerificationError):
        lab.verifier.run()
    assert not lab.fake.writes and not lab.fake.requests


@pytest.mark.parametrize("shape", ["contains", "transform", "pattern", "extra", "unknown_global", "managed"])
def test_unsafe_rule_or_unknown_configuration_refused(lab, shape):
    rule = waf.rule_from(lab.fake.acl)
    statement = rule["Statement"]["ByteMatchStatement"]
    if shape == "contains":
        statement["PositionalConstraint"] = "CONTAINS"
    elif shape == "transform":
        statement["TextTransformations"][0]["Type"] = "LOWERCASE"
    elif shape == "pattern":
        statement["SearchString"] = b"something-else"
    elif shape == "extra":
        rule["RuleLabels"] = [{"Name": "unexpected"}]
    elif shape == "unknown_global":
        lab.fake.acl["FutureMutableSetting"] = True
    else:
        lab.fake.acl["ManagedByFirewallManager"] = True
    with pytest.raises(waf.VerificationError):
        lab.verifier.run()
    assert not lab.fake.writes


@pytest.mark.parametrize("failure", [RuntimeError("private-request"), KeyboardInterrupt()])
def test_restores_on_http_error_or_interrupt(lab, failure):
    def probe(args, headers):
        if waf.rule_from(lab.fake.acl)["Action"] == {"Block": {}}:
            raise failure
        return lab.probe(args, headers)
    lab.verifier.probe = probe
    with pytest.raises(type(failure)):
        lab.verifier.run()
    assert waf.rule_from(lab.fake.acl)["Action"] == {"Count": {}}
    assert lab.verifier.report["restoration"] == "verified"


def test_ambiguous_write_applied_then_timeout_restores(lab):
    update = lab.fake.update_web_acl
    first = True
    def timeout(**payload):
        nonlocal first
        result = update(**payload)
        if first:
            first = False
            raise TimeoutError("sensitive AWS request")
        return result
    lab.fake.update_web_acl = timeout
    with pytest.raises(TimeoutError):
        lab.verifier.run()
    assert waf.rule_from(lab.fake.acl)["Action"] == {"Count": {}}
    assert lab.verifier.report["restoration"] == "verified"


def test_preserves_concurrent_unrelated_rule_and_global_settings(lab):
    def probe(args, headers):
        result = lab.probe(args, headers)
        if waf.rule_from(lab.fake.acl)["Action"] == {"Block": {}}:
            lab.fake.acl["Description"] = "concurrent-owner-value"
            lab.fake.acl["TokenDomains"] = ["example.org"]
            lab.fake.acl["Rules"].append({"Name": "other-rule", "Priority": 90, "Action": {"Count": {}}})
            lab.fake.version += 1
            raise RuntimeError("abort")
        return result
    lab.verifier.probe = probe
    with pytest.raises(RuntimeError):
        lab.verifier.run()
    assert lab.fake.acl["Description"] == "concurrent-owner-value"
    assert lab.fake.acl["TokenDomains"] == ["example.org"]
    assert lab.fake.acl["Rules"][-1]["Name"] == "other-rule"
    assert waf.rule_from(lab.fake.acl)["Action"] == {"Count": {}}


def test_concurrent_drill_edit_is_never_overwritten(lab):
    def probe(args, headers):
        if waf.rule_from(lab.fake.acl)["Action"] == {"Block": {}}:
            waf.rule_from(lab.fake.acl)["Priority"] = 99
            lab.fake.version += 1
            raise RuntimeError("abort")
        return lab.probe(args, headers)
    lab.verifier.probe = probe
    with pytest.raises(waf.VerificationError):
        lab.verifier.run()
    assert waf.rule_from(lab.fake.acl)["Priority"] == 99
    assert len(lab.fake.writes) == 1
    assert lab.verifier.report["restoration"] == "recovery_required"


def test_optimistic_lock_conflict_no_retry_or_clobber(lab):
    class Conflict(Exception):
        response = {"Error": {"Code": "WAFOptimisticLockException"}}
    lab.fake.hook = Mock(side_effect=Conflict())
    with pytest.raises(Conflict):
        lab.verifier.run()
    assert lab.fake.hook.call_count == 1
    assert not lab.fake.writes
    assert lab.verifier.report["restoration"] == "verified"


@pytest.mark.parametrize("missing", ["sample", "metric", "unrelated_sample"])
def test_missing_evidence_fails_closed_and_restores(lab, missing):
    waf.rule_from(lab.fake.acl)["Action"] = {"Block": {}}
    lab.args.evidence_polls = 2
    if missing == "sample":
        lab.fake.no_samples = True
    elif missing == "metric":
        lab.clients["cloudwatch"].get_metric_statistics.side_effect = lambda **kw: {"Datapoints": []}
    else:
        lab.fake.sample_override = [{"Action": "COUNT", "Timestamp": lab.clock.now(),
                                    "Request": {"Method": "GET", "URI": "/healthz", "Headers": []}}]
    with pytest.raises(waf.VerificationError, match="evidence_missing"):
        lab.verifier.run()
    assert waf.rule_from(lab.fake.acl)["Action"] == {"Block": {}}
    assert lab.clients["cloudwatch"].get_metric_statistics.call_count == 2


def test_main_redacts_unknown_errors_and_recovery_failure(lab, monkeypatch, capsys):
    lab.args.attempts = 1
    def probe(args, headers):
        if waf.rule_from(lab.fake.acl)["Action"] == {"Block": {}}:
            lab.fake.hook = Mock(side_effect=RuntimeError("secret-token"))
            raise RuntimeError("private body")
        return lab.probe(args, headers)
    lab.verifier.probe = probe
    monkeypatch.setattr(waf, "make_clients", lambda args: lab.clients)
    monkeypatch.setattr(waf, "Verifier", lambda args, clients: lab.verifier)
    assert waf.main(ARGV + ["--execute"]) == 2
    output = capsys.readouterr().out
    assert "secret-token" not in output and "private body" not in output
    assert json.loads(output)["restoration"] == "recovery_required"


def test_http_transport_does_not_follow_redirect_or_read_body(monkeypatch):
    connection = Mock()
    connection.getresponse.return_value.status = 302
    monkeypatch.setattr(waf, "VerifiedTLSConnection", lambda *a: connection)
    assert waf.http_probe(arguments(), {}) == 302
    connection.request.assert_called_once_with("GET", "/healthz", headers={})
    connection.getresponse.return_value.read.assert_not_called()
    connection.close.assert_called_once()


def test_execute_with_invalid_approval_never_creates_clients(monkeypatch, capsys):
    factory = Mock(side_effect=AssertionError("network forbidden"))
    monkeypatch.setattr(waf, "make_clients", factory)
    assert waf.main(ARGV + ["--execute", "--approval", "pending"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_scope_argument"
    factory.assert_not_called()


def test_sigterm_restores_and_resets_signal_handlers(lab, monkeypatch, capsys):
    previous = signal.getsignal(signal.SIGTERM)
    def probe(args, headers):
        if waf.rule_from(lab.fake.acl)["Action"] == {"Block": {}}:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return lab.probe(args, headers)
    lab.verifier.probe = probe
    monkeypatch.setattr(waf, "make_clients", lambda args: lab.clients)
    monkeypatch.setattr(waf, "Verifier", lambda args, clients: lab.verifier)
    assert waf.main(ARGV + ["--execute"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "interrupted"
    assert report["restoration"] == "verified"
    assert waf.rule_from(lab.fake.acl)["Action"] == {"Count": {}}
    assert signal.getsignal(signal.SIGTERM) == previous


@pytest.mark.parametrize("status", [301, 403, 429, 500])
def test_normal_non_2xx_fails_without_block_phase(lab, status):
    lab.verifier.probe = Mock(return_value=status)
    with pytest.raises(waf.VerificationError, match="normal_traffic_failed"):
        lab.verifier.run()
    assert not lab.fake.writes
    assert lab.verifier.probe.call_count == 1


def test_propagation_retries_are_bounded_and_restore(lab):
    lab.args.attempts = 3
    lab.verifier.probe = Mock(side_effect=lambda args, headers:
                              lab.probe(args, headers) if waf.rule_from(lab.fake.acl)["Action"] == {"Count": {}} else 200)
    with pytest.raises(waf.VerificationError, match="propagation_timeout"):
        lab.verifier.run()
    assert lab.verifier.probe.call_count == 2 + 2 * lab.args.attempts
    assert waf.rule_from(lab.fake.acl)["Action"] == {"Count": {}}


def test_restore_lock_conflict_requires_recovery_without_retry(lab):
    class Conflict(Exception):
        response = {"Error": {"Code": "WAFOptimisticLockException"}}
    def hook(payload):
        if waf.rule_from(payload)["Action"] == {"Count": {}}:
            raise Conflict()
    lab.fake.hook = Mock(side_effect=hook)
    with pytest.raises(Conflict):
        lab.verifier.run()
    assert lab.verifier.report["restoration"] == "recovery_required"
    assert lab.fake.hook.call_count == 2  # BLOCK and one restoration attempt.
    assert waf.rule_from(lab.fake.acl)["Action"] == {"Block": {}}


def test_unknown_aws_evidence_error_restores(lab):
    waf.rule_from(lab.fake.acl)["Action"] = {"Block": {}}
    lab.fake.get_sampled_requests = Mock(side_effect=RuntimeError("unknown AWS error with secrets"))
    with pytest.raises(RuntimeError):
        lab.verifier.run()
    assert waf.rule_from(lab.fake.acl)["Action"] == {"Block": {}}
    assert lab.verifier.report["restoration"] == "verified"


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), "bad"])
def test_invalid_metrics_fail_closed(lab, value):
    lab.clients["cloudwatch"].get_metric_statistics.side_effect = lambda **kw: {
        "Datapoints": [{"Sum": value, "Timestamp": kw["StartTime"]}]}
    with pytest.raises(waf.VerificationError, match="invalid_metric_data"):
        lab.verifier.run()


def test_payload_validates_against_installed_aws_service_model(lab):
    session = pytest.importorskip("botocore.session")
    validate = pytest.importorskip("botocore.validate")
    model = session.get_session().get_service_model("wafv2")  # Model loading only; no client/network.
    payload = lab.verifier.payload(lab.fake.acl, "token", {"Block": {}})
    validate.validate_parameters(payload, model.operation_model("UpdateWebACL").input_shape)
    assert payload["AssociationConfig"] == lab.fake.acl["AssociationConfig"]
    assert payload["CustomResponseBodies"] == lab.fake.acl["CustomResponseBodies"]
    assert "ARN" not in payload and "Capacity" not in payload


def test_timeout_retains_attempted_http_evidence(lab):
    def timeout(*args):
        raise TimeoutError("not-to-be-logged")
    lab.verifier.probe = timeout
    with pytest.raises(TimeoutError):
        lab.verifier.run()
    assert lab.verifier.report["phases"][0]["http"] == [{"normal": "unknown"}]
    assert lab.verifier.report["restoration"] == "verified"


@pytest.mark.parametrize("code,message,allowed", [
    ("WAFInvalidParameterException", "The resource is not supported in current region", True),
    ("AccessDeniedException", "The resource is not supported in current region", False),
    ("WAFInvalidParameterException", "Invalid resource ARN", False),
])
def test_seoul_unsupported_resource_types_are_narrowly_classified(lab, code, message, allowed):
    original_model = lab.fake.meta.service_model.operation_model
    lab.fake.meta.service_model.operation_model = lambda name: (
        SimpleNamespace(input_shape=SimpleNamespace(members={"ResourceType": SimpleNamespace(
            enum=["APPLICATION_LOAD_BALANCER", "APP_RUNNER_SERVICE", "AMPLIFY"])}))
        if name == "ListResourcesForWebACL" else original_model(name))
    original_list = lab.fake.list_resources_for_web_acl
    def listing(**kwargs):
        if kwargs["ResourceType"] in {"APP_RUNNER_SERVICE", "AMPLIFY"}:
            error = RuntimeError("AWS rejected resource type")
            error.response = {"Error": {"Code": code, "Message": message}}
            raise error
        return original_list(**kwargs)
    lab.fake.list_resources_for_web_acl = listing
    if allowed:
        lab.verifier.scope()
        assert lab.verifier.report["region_unsupported_resource_types"] == ["APP_RUNNER_SERVICE", "AMPLIFY"]
    else:
        with pytest.raises(RuntimeError):
            lab.verifier.scope()


def test_sample_window_covers_final_fractional_second_without_future_bound(lab):
    original = lab.fake.get_sampled_requests
    def sampled(**kwargs):
        window = kwargs["TimeWindow"]
        assert window["EndTime"] <= lab.clock.value
        assert window["EndTime"].microsecond == 0
        result = original(**kwargs)
        result["SampledRequests"] = [sample for sample in result["SampledRequests"]
            if window["StartTime"].replace(microsecond=0) <= sample["Timestamp"] < window["EndTime"].replace(microsecond=0)]
        return result
    lab.clock.value = lab.clock.value.replace(microsecond=186000)
    lab.fake.get_sampled_requests = sampled
    assert lab.verifier.run()["status"] == "passed"


@pytest.mark.parametrize("edge,skew_ms,expected", [
    ("start", -3.175, "passed"), ("start", -999, "passed"),
    ("end", 3.175, "passed"), ("end", 999, "passed"),
    ("start", -1001, "evidence_missing_or_delayed"),
    ("end", 1001, "evidence_missing_or_delayed"),
])
def test_sample_correlation_allows_only_bounded_clock_skew(lab, edge, skew_ms, expected):
    original = lab.verifier.probe
    phase_start = None
    last_action = None

    def skewed_probe(args, headers):
        nonlocal phase_start, last_action
        action = next(iter(waf.rule_from(lab.fake.acl)["Action"]))
        if action != last_action:
            # The verifier calls now() immediately before the first phase probe.
            phase_start = lab.clock.value
            last_action = action
        status = original(args, headers)
        if waf.HEADER in headers:
            timestamp, action, request_headers = lab.fake.requests[-1]
            offset = timedelta(milliseconds=skew_ms)
            timestamp = phase_start + (timedelta(seconds=3) if edge == "end" else timedelta()) + offset
            lab.fake.requests[-1] = (timestamp, action, request_headers)
        return status

    lab.verifier.probe = skewed_probe
    if expected == "passed":
        report = lab.verifier.run()
        assert report["status"] == expected
        assert all(phase["sample_matches"] > 0 for phase in report["phases"])
        assert all(phase["sample_clock_skew_tolerance_ms"] == 1000 for phase in report["phases"])
    else:
        with pytest.raises(waf.VerificationError, match=expected):
            lab.verifier.run()
        assert lab.verifier.report["restoration"] == "verified"
