#!/usr/bin/env python3
"""Bounded, read-only CloudWatch capture for an explicitly owned Seoul session.

Requires boto3 and profile credentials. Start/end must be completed, minute-aligned
epochs, at most 3h apart and within the 15-day one-minute retention window.
19 metric queries form one batch, with at most three pages and no SDK retries.
CPUCreditBalance is published every 5 minutes: missing 60s buckets stay missing.
ALB p95 and Average are requested independently; no percentile is fabricated.
"""

import argparse
from datetime import datetime, timezone
import json
import math
import os
import re
import time

PERIOD = 60
MAX_RANGE = 3 * 3600
MAX_PAGES = 3
RDS_METRICS = ("CPUUtilization", "DatabaseConnections", "FreeableMemory",
               "ReadLatency", "WriteLatency", "CPUCreditBalance")
ALB_METRICS = (("RequestCount", "Sum"), ("TargetResponseTime", "p95"),
               ("TargetResponseTime", "Average"), ("HTTPCode_ELB_5XX_Count", "Sum"),
               ("HTTPCode_Target_5XX_Count", "Sum"), ("TargetConnectionErrorCount", "Sum"))


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("profile", "account", "region", "session", "approval", "alb-arn", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    args = parser.parse_args(argv)
    require(bool(args.profile.strip()), "profile_required")
    require(bool(re.fullmatch(r"[0-9]{12}", args.account)), "invalid_account")
    require(args.region == "ap-northeast-2", "seoul_region_required")
    require(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{5,40}", args.session)), "invalid_session")
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", args.approval)), "invalid_approval")
    prefix = f"arn:aws:elasticloadbalancing:{args.region}:{args.account}:loadbalancer/app/"
    require(bool(re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9-]{1,32}/[a-f0-9]{16}", args.alb_arn)),
            "alb_arn_outside_scope")
    now = int(time.time())
    require(0 < args.start < args.end <= now, "completed_interval_required")
    require(args.end - args.start <= MAX_RANGE, "interval_exceeds_3h")
    require(args.start >= now - 15 * 86400, "outside_60s_retention")
    require(args.start % PERIOD == args.end % PERIOD == 0, "minute_aligned_epochs_required")
    return args


def aws_clients(args):
    import boto3
    from botocore.config import Config
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    config = Config(connect_timeout=5, read_timeout=15,
                    retries={"total_max_attempts": 1, "mode": "standard"})
    return {name: session.client(name, region_name=args.region, config=config)
            for name in ("sts", "rds", "elbv2", "cloudwatch")}


def verify_ownership(args, clients):
    require(clients["sts"].get_caller_identity().get("Account") == args.account, "account_mismatch")
    expected = {"Project": "kyobo-platform-live-lab", "Session": args.session, "Approval": args.approval}

    def check_tags(tags):
        actual = {tag["Key"]: tag["Value"] for tag in tags}
        require(all(actual.get(key) == value for key, value in expected.items()), "ownership_tags_mismatch")

    resources = {}
    for role in ("primary", "reader"):
        identifier = f"kyobo-{args.session}-mysql-{role}"
        arn = f"arn:aws:rds:{args.region}:{args.account}:db:{identifier}"
        rows = clients["rds"].describe_db_instances(DBInstanceIdentifier=identifier).get("DBInstances", [])
        require(len(rows) == 1 and rows[0].get("DBInstanceIdentifier") == identifier
                and rows[0].get("DBInstanceArn") == arn, "rds_identity_mismatch")
        check_tags(clients["rds"].list_tags_for_resource(ResourceName=arn).get("TagList", []))
        resources[role] = {"identifier": identifier, "arn": arn}
    rows = clients["elbv2"].describe_load_balancers(LoadBalancerArns=[args.alb_arn]).get("LoadBalancers", [])
    require(len(rows) == 1 and rows[0].get("LoadBalancerArn") == args.alb_arn
            and rows[0].get("Type") == "application", "alb_identity_mismatch")
    tagged = clients["elbv2"].describe_tags(ResourceArns=[args.alb_arn]).get("TagDescriptions", [])
    require(len(tagged) == 1 and tagged[0].get("ResourceArn") == args.alb_arn, "alb_tag_identity_mismatch")
    check_tags(tagged[0].get("Tags", []))
    resources["alb"] = {"arn": args.alb_arn, "dimension": args.alb_arn.split(":loadbalancer/", 1)[1]}
    return resources


def metric_queries(resources):
    queries = []

    def add(namespace, dimension, resource, name, stat):
        queries.append({"Id": f"m{len(queries)}", "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": namespace, "MetricName": name,
                       "Dimensions": [{"Name": dimension, "Value": resource}]},
            "Period": PERIOD, "Stat": stat,
        }})

    for role in ("primary", "reader"):
        for metric in RDS_METRICS + (("ReplicaLag",) if role == "reader" else ()):
            add("AWS/RDS", "DBInstanceIdentifier", resources[role]["identifier"], metric,
                "Maximum" if metric == "ReplicaLag" else "Average")
    for metric, stat in ALB_METRICS:
        add("AWS/ApplicationELB", "LoadBalancer", resources["alb"]["dimension"], metric, stat)
    return queries


def read_metrics(args, client, queries):
    states = {q["Id"]: {"points": {}, "seen": False, "complete": False, "reason": None} for q in queries}
    token, seen_tokens, pages, global_error = None, set(), 0, None
    request = {"MetricDataQueries": queries, "StartTime": datetime.fromtimestamp(args.start, timezone.utc),
               "EndTime": datetime.fromtimestamp(args.end, timezone.utc), "ScanBy": "TimestampAscending",
               "MaxDatapoints": len(queries) * (MAX_RANGE // PERIOD)}
    try:
        for _ in range(MAX_PAGES):
            pages += 1
            response = client.get_metric_data(**request, **({"NextToken": token} if token else {}))
            require(not response.get("Messages"), "cloudwatch_batch_message")
            rows = response.get("MetricDataResults", [])
            require(isinstance(rows, list) and len(rows) <= len(queries), "malformed_metric_results")
            page_ids = set()
            for row in rows:
                key = row.get("Id")
                require(key in states and key not in page_ids, "unexpected_metric_id")
                page_ids.add(key)
                state = states[key]
                state["seen"] = True
                status = row.get("StatusCode")
                state["complete"] = status == "Complete"
                if status not in {"Complete", "PartialData"} or row.get("Messages"):
                    state["reason"] = "cloudwatch_metric_error"
                    continue
                stamps, values = row.get("Timestamps", []), row.get("Values", [])
                require(len(stamps) == len(values) <= MAX_RANGE // PERIOD, "malformed_metric_points")
                for stamp, value in zip(stamps, values):
                    require(isinstance(stamp, datetime) and stamp.tzinfo is not None, "invalid_timestamp")
                    epoch = stamp.timestamp()
                    require(epoch.is_integer() and args.start <= epoch < args.end and epoch % PERIOD == 0,
                            "timestamp_outside_window")
                    require(type(value) in (int, float) and math.isfinite(value), "non_finite_metric")
                    require(epoch not in state["points"], "duplicate_timestamp")
                    state["points"][int(epoch)] = value
                    require(len(state["points"]) <= MAX_RANGE // PERIOD, "metric_point_limit")
            token = response.get("NextToken")
            if not token:
                break
            require(isinstance(token, str) and token not in seen_tokens, "pagination_token_repeated")
            seen_tokens.add(token)
        if token:
            global_error = "pagination_limit"
    except Exception:
        # SDK exceptions and server messages can contain sensitive details.
        global_error = "cloudwatch_response_unknown"
    result = []
    for query in queries:
        state = states[query["Id"]]
        reason = global_error or state["reason"] or (
            "partial_data" if state["seen"] and not state["complete"] else None)
        buckets = [{"epoch": epoch, "status": "unknown" if reason else (
            "observed" if epoch in state["points"] else "missing"),
            "value": None if reason else state["points"].get(epoch)}
            for epoch in range(args.start, args.end, PERIOD)]
        metric = query["MetricStat"]
        result.append({"id": query["Id"], **metric,
                       "status": "unknown" if reason else "observed" if state["points"] else "missing",
                       "reason": reason, "buckets": buckets,
                       "native_period_note": "published_every_300s_on_burstable_instances"
                       if metric["Metric"]["MetricName"] == "CPUCreditBalance" else None})
    return {"metric_pages": pages, "metrics": result}


def capture(args, clients):
    resources = verify_ownership(args, clients)  # All three must pass before the first metric query.
    data = read_metrics(args, clients["cloudwatch"], metric_queries(resources))
    return {"status": "completed", "ownership": "verified", "account": args.account,
            "region": args.region, "session": args.session, "approval": args.approval,
            "start": args.start, "end_exclusive": args.end, "period_seconds": PERIOD,
            "captured_at": datetime.now(timezone.utc).isoformat(), "resources": resources, **data}


def main(argv=None):
    try:
        args = arguments(argv)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except (ValueError, OSError):
        print(json.dumps({"status": "not_run", "reason": "invalid_arguments_or_output"}))
        return 2
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        try:
            report = capture(args, aws_clients(args))
            code = 1 if any(m["status"] == "unknown" for m in report["metrics"]) else 0
        except Exception:
            report, code = {"status": "blocked", "reason": "ownership_or_setup_verification_failed"}, 2
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")
    print(json.dumps({"status": report["status"], "exit_code": code}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
