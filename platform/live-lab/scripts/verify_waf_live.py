#!/usr/bin/env python3
"""Bounded WAF drill; default is an entirely offline plan.

Requires Python 3.10+, boto3 for --execute, explicitly approved credentials and
exclusive operation of this session's drill rule (pause Terraform reconciliation).
Only GET /healthz is sent; no cookies, redirects, proxies, or response bodies.
For the lab's self-signed certificate, supply its public PEM as --ca-bundle and
--tls-server-name live-lab.invalid. TLS verification is never disabled.

AWS references:
https://docs.aws.amazon.com/waf/latest/APIReference/API_UpdateWebACL.html
https://docs.aws.amazon.com/waf/latest/APIReference/API_GetSampledRequests.html
https://docs.aws.amazon.com/waf/latest/developerguide/waf-metrics.html

UpdateWebACL replaces mutable configuration. Every write uses a fresh read and
LockToken; rollback changes only our unchanged rule, preserving other settings.
Concurrent edits to the drill rule require operator recovery, never overwrite.
SIGINT/SIGTERM trigger finally; SIGKILL, host loss, and unavailable AWS cannot
guarantee rollback. Nonzero recovery_required output must be acted upon before
rerunning. Approval identifiers are scope assertions, not proof of authorization.
"""

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import http.client
import json
import math
import re
import signal
import ssl
import time
from urllib.parse import urlsplit
from uuid import uuid4


RULE = "custom-header-live-lab-drill"
HEADER = "x-live-lab-waf-drill"
RUN_HEADER = "x-live-lab-waf-verifier"
READ_ONLY = {
    "ARN", "Capacity", "LabelNamespace", "ManagedByFirewallManager",
    "PreProcessFirewallManagerRuleGroups", "PostProcessFirewallManagerRuleGroups",
    "RetrofittedByFirewallManager",
}
SAMPLE_CLOCK_SKEW_TOLERANCE = timedelta(seconds=1)


class VerificationError(Exception):
    """Fixed, non-sensitive failure code suitable for evidence output."""


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--execute", action="store_true")
    for flag in ("account", "region", "session", "approval", "cluster", "alb-arn", "web-acl-arn", "url"):
        result.add_argument("--" + flag, required=True)
    result.add_argument("--profile")
    result.add_argument("--header-value", default="kyobo-live-lab-deny")
    result.add_argument("--ca-bundle")
    result.add_argument("--tls-server-name")
    result.add_argument("--attempts", type=int, default=6, help="1-10 HTTP pairs per phase")
    result.add_argument("--interval", type=int, default=10, help="5-30 seconds between HTTP pairs")
    result.add_argument("--evidence-polls", type=int, default=12, help="1-20 polls, 15 seconds apart")
    return result


def validate_args(args):
    for value, pattern in (
        (args.account, r"[0-9]{12}"),
        (args.region, r"[a-z]{2}-[a-z]+-[0-9]+"),
        (args.session, r"[a-z0-9][a-z0-9-]{5,40}"),
        (args.approval, r"SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}"),
        (args.header_value, r"[A-Za-z0-9._:-]{8,64}"),
    ):
        require(re.fullmatch(pattern, value), "invalid_scope_argument")
    require(args.cluster == "kyobo-" + args.session, "cluster_name_mismatch")
    base = f"arn:aws:{{service}}:{args.region}:{args.account}:"
    require(re.fullmatch(re.escape(base.format(service="elasticloadbalancing")) +
                         r"loadbalancer/app/[A-Za-z0-9-]+/[a-f0-9]+", args.alb_arn), "alb_arn_mismatch")
    name = args.cluster + "-web-acl"
    require(re.fullmatch(re.escape(base.format(service="wafv2") + "regional/webacl/" + name + "/") +
                         r"[a-f0-9-]{36}", args.web_acl_arn), "acl_arn_mismatch")
    url = urlsplit(args.url)
    require(url.scheme in ("http", "https") and url.hostname and not url.username and
            not url.password and not url.query and not url.fragment and url.path == "/healthz" and
            url.port in (None, 80 if url.scheme == "http" else 443), "unsafe_url")
    require(not args.tls_server_name or (args.ca_bundle and url.scheme == "https" and
            re.fullmatch(r"[A-Za-z0-9.-]+", args.tls_server_name)), "invalid_tls_configuration")
    require(1 <= args.attempts <= 10 and 5 <= args.interval <= 30 and
            1 <= args.evidence_polls <= 20, "unbounded_drill")


def expected_tags(args):
    return {"Project": "kyobo-platform-live-lab", "Session": args.session, "Approval": args.approval}


def scope_evidence(args):
    return {key: getattr(args, key) for key in (
        "account", "region", "session", "approval", "cluster", "alb_arn", "web_acl_arn")}


def check_tags(tags, args):
    require(all(tags.get(k) == v for k, v in expected_tags(args).items()), "ownership_mismatch")


def tag_map(items):
    result = {item["Key"]: item["Value"] for item in items}
    require(len(result) == len(items), "duplicate_tags")
    return result


def rule_from(acl):
    matches = [rule for rule in acl["Rules"] if rule["Name"] == RULE]
    require(len(matches) == 1, "drill_rule_missing_or_duplicate")
    return matches[0]


def validate_rule(acl, args):
    rule = rule_from(acl)
    expected = {
        "Name": RULE, "Priority": 10,
        "Statement": {"ByteMatchStatement": {
            "FieldToMatch": {"SingleHeader": {"Name": HEADER}},
            "PositionalConstraint": "EXACTLY", "SearchString": args.header_value.encode("ascii"),
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}],
        }},
        "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True,
                             "MetricName": args.cluster.replace("-", "") + "CustomHeaderDrill"},
    }
    require({k: v for k, v in rule.items() if k != "Action"} == expected, "unsafe_drill_rule")
    require(rule.get("Action") in ({"Count": {}}, {"Block": {}}), "unsafe_drill_action")
    require(acl["VisibilityConfig"] == {
        "SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True,
        "MetricName": args.cluster.replace("-", "") + "WebAcl",
    }, "acl_visibility_mismatch")
    require(not any(acl.get(key) for key in READ_ONLY if "FirewallManager" in key), "managed_acl_refused")
    return rule


class VerifiedTLSConnection(http.client.HTTPSConnection):
    """Connect to verified ALB DNS, optionally verify the session certificate name."""

    def __init__(self, host, context, server_name):
        super().__init__(host, timeout=5, context=context)
        self.server_name = server_name or host

    def connect(self):
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.server_name)


def http_probe(args, headers):
    url = urlsplit(args.url)
    if url.scheme == "https":
        connection = VerifiedTLSConnection(url.hostname, ssl.create_default_context(cafile=args.ca_bundle),
                                           args.tls_server_name)
    else:
        connection = http.client.HTTPConnection(url.hostname, timeout=5)
    try:
        connection.request("GET", "/healthz", headers=headers)
        response = connection.getresponse()
        return response.status  # Never read or log bodies/response headers; never redirect.
    finally:
        connection.close()


class Verifier:
    def __init__(self, args, clients, *, probe=http_probe, sleep=time.sleep,
                 now=lambda: datetime.now(timezone.utc)):
        self.args, self.clients = args, clients
        self.waf = clients["wafv2"]
        self.probe, self.sleep, self.now = probe, sleep, now
        self.original = self.owned = self.pending = None
        self.report = {"status": "failed", "restoration": "not_needed", "phases": [],
                       "scope": scope_evidence(args)}
        self.key = {"Name": args.cluster + "-web-acl", "Id": args.web_acl_arn.rsplit("/", 1)[1],
                    "Scope": "REGIONAL"}

    def scope(self):
        a, c = self.args, self.clients
        require(c["sts"].get_caller_identity()["Account"] == a.account, "account_mismatch")
        for client in c.values():
            require(client.meta.region_name == a.region, "client_region_mismatch")
        cluster = c["eks"].describe_cluster(name=a.cluster)["cluster"]
        require(cluster["arn"] == f"arn:aws:eks:{a.region}:{a.account}:cluster/{a.cluster}" and
                cluster["status"] == "ACTIVE", "cluster_identity_mismatch")
        check_tags(cluster["tags"], a)
        lbs = c["elbv2"].describe_load_balancers(LoadBalancerArns=[a.alb_arn])["LoadBalancers"]
        require(len(lbs) == 1 and lbs[0]["LoadBalancerArn"] == a.alb_arn and
                lbs[0]["Type"] == "application" and lbs[0]["State"]["Code"] == "active" and
                lbs[0]["VpcId"] == cluster["resourcesVpcConfig"]["vpcId"], "alb_identity_mismatch")
        require(urlsplit(a.url).hostname == lbs[0]["DNSName"], "url_not_bound_to_alb")
        descriptions = c["elbv2"].describe_tags(ResourceArns=[a.alb_arn])["TagDescriptions"]
        require(len(descriptions) == 1 and descriptions[0]["ResourceArn"] == a.alb_arn, "alb_tags_mismatch")
        tags = tag_map(descriptions[0]["Tags"])
        check_tags(tags, a)
        require(tags.get("elbv2.k8s.aws/cluster") == a.cluster, "alb_cluster_mismatch")
        resource_tags = self.waf.list_tags_for_resource(ResourceARN=a.web_acl_arn)
        require(not resource_tags.get("NextMarker"), "incomplete_acl_tags")
        check_tags(tag_map(resource_tags["TagInfoForResource"]["TagList"]), a)
        associated = self.waf.get_web_acl_for_resource(ResourceArn=a.alb_arn)["WebACL"]
        require(associated["ARN"] == a.web_acl_arn, "wrong_acl_association")
        # Reject shared ACLs, including non-ALB regional resource types supported by the SDK.
        types = self.waf.meta.service_model.operation_model("ListResourcesForWebACL").input_shape.members["ResourceType"].enum
        for resource_type in types:
            try:
                arns = self.waf.list_resources_for_web_acl(WebACLArn=a.web_acl_arn,
                                                          ResourceType=resource_type)["ResourceArns"]
            except Exception as exc:
                error = getattr(exc, "response", {}).get("Error", {})
                # Verified AWS region restrictions, not an empty-result fallback:
                # App Runner has no Seoul endpoint; Amplify requires CLOUDFRONT ACLs.
                # https://docs.aws.amazon.com/general/latest/gr/apprunner.html
                # https://docs.aws.amazon.com/waf/latest/developerguide/how-aws-waf-works-resources.html
                known_unsupported = (a.region == "ap-northeast-2" and
                                     resource_type in {"APP_RUNNER_SERVICE", "AMPLIFY"})
                if not (known_unsupported and error.get("Code") == "WAFInvalidParameterException" and
                        error.get("Message") == "The resource is not supported in current region"):
                    raise
                skipped = self.report.setdefault("region_unsupported_resource_types", [])
                if resource_type not in skipped:
                    skipped.append(resource_type)
                continue
            require(arns == ([a.alb_arn] if resource_type == "APPLICATION_LOAD_BALANCER" else []),
                    "acl_is_shared_or_unassociated")

    def read(self):
        response = self.waf.get_web_acl(**self.key)
        acl = response["WebACL"]
        require(acl["ARN"] == self.args.web_acl_arn and acl["Name"] == self.key["Name"] and
                acl["Id"] == self.key["Id"], "acl_identity_mismatch")
        validate_rule(acl, self.args)
        require(bool(response["LockToken"]), "missing_lock_token")
        return acl, response["LockToken"]

    def payload(self, acl, token, action):
        members = self.waf.meta.service_model.operation_model("UpdateWebACL").input_shape.members
        require(not (set(acl) - set(members) - READ_ONLY), "unknown_acl_configuration")
        payload = {k: deepcopy(v) for k, v in acl.items() if k in members}
        # GetWebACL returns "" for an unset description; UpdateWebACL requires
        # length >= 1 when supplied. Omission preserves the unset state. Do not
        # broadly filter falsy values: other optional settings must survive.
        if payload.get("Description") == "":
            del payload["Description"]
        rule_from(payload)["Action"] = deepcopy(action)
        return {**payload, **self.key, "LockToken": token}

    def change(self, action):
        self.scope()
        acl, token = self.read()
        rule = rule_from(acl)
        require(rule == self.owned, "concurrent_drill_edit")
        if rule["Action"] == action:
            return
        payload = self.payload(acl, token, action)
        self.pending = deepcopy(rule_from(payload))  # Write can succeed even if the response is lost.
        try:
            self.waf.update_web_acl(**payload)
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "WAFOptimisticLockException":
                self.pending = None  # AWS guarantees that this write did not apply.
            raise
        self.owned, self.pending = self.pending, None

    def restore(self):
        if self.original is None:
            return
        # Even unknown write failures enter this path. Never restore a cached ACL snapshot.
        self.report["restoration"] = "recovery_required"
        self.scope()
        acl, token = self.read()
        current = rule_from(acl)
        require(current in (self.original, self.owned, self.pending), "concurrent_drill_edit")
        if current != self.original:
            self.waf.update_web_acl(**self.payload(acl, token, self.original["Action"]))
        restored, _ = self.read()
        require(rule_from(restored) == self.original, "restoration_unconfirmed")
        self.report["restoration"] = "verified"

    def phase(self, mode):
        self.change({mode: {}})
        started = self.now()
        marker = uuid4().hex
        expected = 403 if mode == "Block" else 200
        statuses = []
        evidence = {"mode": mode.upper(), "started_at": started.isoformat(),
                    "http": statuses, "sample_matches": 0, "metric_sum": 0,
                    "sample_clock_skew_tolerance_ms": int(SAMPLE_CLOCK_SKEW_TOLERANCE.total_seconds() * 1000),
                    "metric_scope": "rule_minute_buckets_not_individual_requests"}
        self.report["phases"].append(evidence)
        for attempt in range(self.args.attempts):
            self.scope()
            current, _ = self.read()
            require(rule_from(current) == self.owned, "concurrent_drill_edit")
            # Record attempts before I/O so timeouts still leave bounded traffic evidence.
            pair = {"normal": "unknown"}
            statuses.append(pair)
            normal = self.probe(self.args, {RUN_HEADER: marker})
            pair["normal"] = normal
            require(normal == 200, "normal_traffic_failed")
            pair["synthetic"] = "unknown"
            synthetic = self.probe(self.args, {RUN_HEADER: marker, HEADER: self.args.header_value})
            pair["synthetic"] = synthetic
            if synthetic == expected:
                break
            require(synthetic in (200, 403), "unexpected_synthetic_response")
            if attempt + 1 < self.args.attempts:
                self.sleep(self.args.interval)
        require(statuses[-1]["synthetic"] == expected, "propagation_timeout")
        ended = self.now()
        evidence["ended_at"] = ended.isoformat()
        rule_metric = self.owned["VisibilityConfig"]["MetricName"]
        # WAF bounds have whole-second precision. Expand both bounds by the
        # small clock-skew tolerance, then round outward. Match filtering below
        # still requires this phase's unique marker and exact request contract.
        sample_start = (started - SAMPLE_CLOCK_SKEW_TOLERANCE).replace(microsecond=0)
        sample_end = (ended + SAMPLE_CLOCK_SKEW_TOLERANCE).replace(microsecond=0) + timedelta(seconds=1)
        delay = (sample_end - self.now()).total_seconds()
        if delay > 0:
            self.sleep(delay)
        for poll in range(self.args.evidence_polls):
            samples = self.waf.get_sampled_requests(
                WebAclArn=self.args.web_acl_arn, RuleMetricName=rule_metric, Scope="REGIONAL",
                TimeWindow={"StartTime": sample_start, "EndTime": sample_end}, MaxItems=500)
            count = 0
            for sample in samples["SampledRequests"]:
                request = sample["Request"]
                headers = {h["Name"].lower(): h["Value"] for h in request["Headers"]}
                if (headers.get(RUN_HEADER) == marker and headers.get(HEADER) == self.args.header_value and
                        request["Method"] == "GET" and request["URI"] == "/healthz" and
                        sample["Action"].upper() == mode.upper() and
                        started - SAMPLE_CLOCK_SKEW_TOLERANCE <= sample["Timestamp"] <=
                        ended + SAMPLE_CLOCK_SKEW_TOLERANCE):
                    count += 1
            metrics = self.clients["cloudwatch"].get_metric_statistics(
                Namespace="AWS/WAFV2", MetricName="BlockedRequests" if mode == "Block" else "CountedRequests",
                Dimensions=[{"Name": "WebACL", "Value": self.key["Name"]},
                            {"Name": "Rule", "Value": rule_metric}, {"Name": "Region", "Value": self.args.region}],
                StartTime=started.replace(second=0, microsecond=0), EndTime=ended + timedelta(minutes=1),
                Period=60, Statistics=["Sum"])
            require(all(isinstance(point["Sum"], (int, float)) and math.isfinite(point["Sum"]) and
                        point["Sum"] >= 0 for point in metrics["Datapoints"]), "invalid_metric_data")
            total = sum(point["Sum"] for point in metrics["Datapoints"]
                        if started.replace(second=0, microsecond=0) <= point["Timestamp"] <= ended)
            evidence.update(sample_matches=count, metric_sum=total)
            if count > 0 and total > 0:
                return
            if poll + 1 < self.args.evidence_polls:
                self.sleep(15)
        raise VerificationError("evidence_missing_or_delayed")

    def run(self):
        self.original = self.owned = self.pending = None
        self.report = {"status": "failed", "restoration": "not_needed", "phases": [],
                       "scope": scope_evidence(self.args)}
        validate_args(self.args)
        require(self.args.execute, "execution_not_requested")
        self.scope()
        acl, _ = self.read()
        # Validate that the SDK can preserve all fields before any mutation.
        self.payload(acl, "preflight", rule_from(acl)["Action"])
        self.original = deepcopy(rule_from(acl))
        self.owned = deepcopy(self.original)
        self.report["original_action"] = next(iter(self.original["Action"])).upper()
        try:
            self.phase("Count")
            self.phase("Block")
        finally:
            self.restore()
        self.report["status"] = "passed"
        return self.report


def make_clients(args):
    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    # No automatic mutation retry after ambiguous errors. Caller reconciles by fresh read.
    config = Config(region_name=args.region, connect_timeout=5, read_timeout=10,
                    retries={"total_max_attempts": 1}, ignore_configured_endpoint_urls=True)
    return {name: session.client(name, config=config) for name in ("sts", "eks", "elbv2", "wafv2", "cloudwatch")}


def main(argv=None):
    args = parser().parse_args(argv)
    verifier = None
    handlers = {}
    try:
        validate_args(args)
        if not args.execute:
            print(json.dumps({"status": "offline_plan", "network_calls": 0, "scope": scope_evidence(args),
                              "phases": ["scope", "COUNT", "BLOCK", "restore_original_action"],
                              "max_http_requests": 4 * args.attempts,
                              "execute_requires": "--execute"}))
            return 0

        def interrupted(signum, frame):
            # Subsequent interrupts must not interrupt the best-effort rollback.
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, signal.SIG_IGN)
            raise VerificationError("interrupted")

        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, interrupted)
        verifier = Verifier(args, make_clients(args))
        print(json.dumps(verifier.run(), sort_keys=True))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        report = verifier.report if verifier else {"status": "failed", "restoration": "not_needed"}
        report["status"] = "failed"
        report["error"] = str(exc) if isinstance(exc, VerificationError) else "unknown_error"
        # Never serialize AWS exception messages, requests, credentials or raw samples.
        print(json.dumps(report, sort_keys=True))
        return 2
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
