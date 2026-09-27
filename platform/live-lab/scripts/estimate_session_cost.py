#!/usr/bin/env python3
"""Conservative Seoul session estimate; defaults match the current short-run budget."""

from __future__ import annotations

import argparse
from datetime import date
import json
import math
import os


SNAPSHOT_DATE = date(2026, 9, 23)
MONTH_HOURS = 730
RATES = {
    "eks_cluster_hour": 0.10,
    "m7i_xlarge_hour": 0.2478,
    "rds_mysql_t3_small_multi_az_hour": 0.104,
    "rds_mysql_t3_small_single_az_hour": 0.052,
    "alb_hour": 0.0225,
    "alb_lcu_hour": 0.008,
    "waf_web_acl_month": 5.00,
    "waf_rule_month": 1.00,
    "waf_request": 0.0000006,
    "public_ipv4_hour": 0.005,
    "rds_gp3_multi_az_gb_month": 0.262,
    "ebs_gp3_gb_month": 0.0912,
    "secret_month": 0.40,
    "secret_api_10k": 0.05,
    "ecr_gb_month": 0.10,
    "cloudwatch_custom_metric_month": 0.30,
    "rds_t3_surplus_vcpu_hour": 0.075,
}


def estimate(hours: float, requests: int, budget: float, reserve: float) -> dict:
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               for v in (hours, budget, reserve)):
        raise ValueError("hours, budget and reserve must be finite numbers")
    if not 0 < hours <= 3:
        raise ValueError("hours must be greater than zero and at most 3 for this approved profile")
    if type(requests) is not int or requests < 0 or requests > 200_000:
        raise ValueError("requests must be between zero and the enforced 200,000 request ceiling")
    if not 0 < budget <= 5.5:
        raise ValueError("budget must be positive and no more than the approved USD 5.50 ceiling")
    if reserve < 1:
        raise ValueError("reserve must be at least USD 1.00 for this approved profile")

    billed_hours = math.ceil(hours)
    rds_hours = max(hours, 1 / 6)  # RDS has a ten-minute minimum per billable start.
    items = {
        "EKS standard control plane": RATES["eks_cluster_hour"] * hours,
        "Two fixed m7i.xlarge worker nodes": 2 * RATES["m7i_xlarge_hour"] * hours,
        "RDS MySQL Multi-AZ t3.small writer": RATES["rds_mysql_t3_small_multi_az_hour"] * rds_hours,
        "RDS MySQL Single-AZ t3.small async replica": RATES["rds_mysql_t3_small_single_az_hour"] * rds_hours,
        "ALB hours, rounded up per partial hour": RATES["alb_hour"] * billed_hours,
        "ALB LCU allowance, 2 average LCUs": 2 * RATES["alb_lcu_hour"] * hours,
        "WAF ACL and one rule, hourly prorating": (RATES["waf_web_acl_month"] + RATES["waf_rule_month"]) * hours / MONTH_HOURS,
        "WAF request allowance": requests * RATES["waf_request"],
        "Public IPv4 allowance, up to 8 addresses": 8 * RATES["public_ipv4_hour"] * hours,
        "RDS gp3 storage, conservatively 40 GB at Multi-AZ rate": 40 * RATES["rds_gp3_multi_az_gb_month"] * rds_hours / MONTH_HOURS,
        "EBS gp3 worker volumes, 40 GB": 40 * RATES["ebs_gp3_gb_month"] * hours / MONTH_HOURS,
        "ECR image storage allowance, 2 GB": 2 * RATES["ecr_gb_month"] * hours / MONTH_HOURS,
        "Secrets Manager secret plus 1,000 API calls": RATES["secret_month"] * hours / MONTH_HOURS + RATES["secret_api_10k"] / 10,
        "CloudWatch custom metric allowance, 4 metrics": 4 * RATES["cloudwatch_custom_metric_month"] * hours / MONTH_HOURS,
        "RDS burst CPU-credit worst-case allowance, 3 x 2 vCPU": 3 * 2 * RATES["rds_t3_surplus_vcpu_hour"] * hours,
        "Public data transfer and small unmodeled usage allowance": 0.10,
    }
    base = round(sum(items.values()), 4)
    planning_total = round(base + reserve, 4)
    return {
        "schema_version": 1,
        "price_list_snapshot_date": SNAPSHOT_DATE.isoformat(),
        "ec2_rds_rates_rechecked_date": "2026-09-27",
        "rds_standard_support_engine": "MySQL 8.4; extended support disabled",
        "region": "ap-northeast-2",
        "hours": hours,
        "billed_alb_hours": billed_hours,
        "billed_rds_hours": rds_hours,
        "request_ceiling": 200_000,
        "requests_estimated": requests,
        "budget_usd": budget,
        "base_estimate_usd": base,
        "uncertainty_reserve_usd": reserve,
        "planning_total_usd": planning_total,
        "within_budget": planning_total <= budget,
        "line_items_usd": {name: round(amount, 4) for name, amount in items.items()},
        "limitations": [
            "A planning estimate, not AWS Cost Explorer or an invoice.",
            "The estimate assumes the two-node pool stays fixed and uses no NAT Gateway, Bastion, managed Prometheus, or persistent Grafana storage.",
            "Taxes, delayed deletion, failed cleanup, additional ALB scaling, extra traffic, and changed AWS rates can increase charges.",
            "Refresh AWS Price List rates and re-run immediately before apply; stop if an actual plan value or rate is outside these assumptions.",
        ],
        "price_sources": {
            "eks": "https://aws.amazon.com/eks/pricing/",
            "ec2_and_rds": "AWS Price List API queried for Asia Pacific (Seoul), Linux m7i.xlarge, MySQL db.t3.small Multi-AZ and Single-AZ",
            "elb": "https://aws.amazon.com/elasticloadbalancing/pricing/",
            "waf": "https://aws.amazon.com/waf/pricing/",
            "public_ipv4": "https://aws.amazon.com/vpc/pricing/",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=3)
    parser.add_argument("--requests", type=int, default=40_000)
    parser.add_argument("--budget", type=float, default=5.50)
    parser.add_argument("--reserve", type=float, default=1.00)
    parser.add_argument("--output", help="Optional private JSON evidence path")
    args = parser.parse_args()
    try:
        result = estimate(args.hours, args.requests, args.budget, args.reserve)
    except ValueError as exc:
        parser.error(str(exc))
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
    print(serialized, end="")
    return 0 if result["within_budget"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
