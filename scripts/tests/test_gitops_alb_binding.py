"""The operator may bind only the ALB owned by the current live session."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "alb_binding", ROOT / "platform/live-lab/scripts/bind_gitops_alb_identity.py"
)
binding = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(binding)


@pytest.fixture
def owned():
    session = "live-261006-01"
    cluster = f"kyobo-{session}"
    arn = ("arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:"
           "loadbalancer/app/k8s-platform-example/0123456789abcdef")
    acl = ("arn:aws:wafv2:ap-northeast-2:123456789012:regional/webacl/"
           f"{cluster}-web-acl/01234567-89ab-cdef-0123-456789abcdef")
    args = SimpleNamespace(account="123456789012", region="ap-northeast-2", session=session,
                           approval="SS0-20261006-test", cluster=cluster)
    report = {
        "status": "verified", "account": args.account, "region": args.region,
        "session": session, "approval": args.approval, "cluster": cluster,
        "alb_arn": arn, "alb_dns": "example.ap-northeast-2.elb.amazonaws.com",
        "alb_cloudwatch_load_balancer_name": "k8s-platform-example",
        "alb_cloudwatch_load_balancer_id": "0123456789abcdef",
        "alb_cloudwatch_load_balancer_dimension": "app/k8s-platform-example/0123456789abcdef",
        "web_acl_arn": acl,
    }
    ingress = {"metadata": {"name": "data-pipeline-ingress", "namespace": "platform-validation"},
               "status": {"loadBalancer": {"ingress": [{"hostname": report["alb_dns"]}]}}}
    rollout = {"metadata": {"name": "data-pipeline-rollout", "namespace": "platform-validation",
                            "resourceVersion": "123", "labels": {"argocd.argoproj.io/instance": "data-pipeline-validation"}},
               "spec": {"selector": {"matchLabels": {"app": "data-pipeline-app"}}}}
    alb = {"LoadBalancerArn": arn, "DNSName": report["alb_dns"], "Type": "application",
           "Scheme": "internet-facing", "State": {"Code": "active"}}
    tags = {"Project": "kyobo-platform-live-lab", "Session": session,
            "Approval": args.approval, "elbv2.k8s.aws/cluster": cluster}
    return args, report, ingress, rollout, alb, tags, acl


def test_verified_session_binding_preserves_existing_labels(owned):
    args, report, ingress, rollout, alb, tags, acl = owned
    labels = binding.derive_labels(args, report, ingress, rollout, alb, tags, acl)
    assert labels["argocd.argoproj.io/instance"] == "data-pipeline-validation"
    assert labels["live-lab.aws/alb-name"] == "k8s-platform-example"
    assert labels["live-lab.aws/alb-id"] == "0123456789abcdef"


def test_ingress_status_with_controller_ports_is_accepted(owned):
    args, report, ingress, rollout, alb, tags, acl = owned
    ingress["status"]["loadBalancer"]["ingress"][0]["ports"] = [
        {"port": 80, "protocol": ""}, {"port": 443, "protocol": ""},
    ]
    labels = binding.derive_labels(args, report, ingress, rollout, alb, tags, acl)
    assert labels["live-lab.aws/alb-id"] == "0123456789abcdef"


def test_canary_target_group_must_belong_to_the_session_alb(owned):
    args, _, _, _, alb, _, _ = owned
    arn = ("arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:"
           "targetgroup/k8s-platform-canary/fedcba9876543210")
    bindings = [{"metadata": {"namespace": "platform-validation"}, "spec": {
        "serviceRef": {"name": "data-pipeline-svc-canary", "port": 80},
        "targetType": "ip", "targetGroupARN": arn,
    }}]
    target_group = {"TargetGroupArn": arn, "TargetGroupName": "k8s-platform-canary",
                    "Protocol": "HTTP", "Port": 8080, "TargetType": "ip",
                    "VpcId": "vpc-1234", "LoadBalancerArns": [alb["LoadBalancerArn"]]}
    alb["VpcId"] = "vpc-1234"
    tags = {"Project": "kyobo-platform-live-lab", "Session": args.session,
            "Approval": args.approval, "elbv2.k8s.aws/cluster": args.cluster,
            "ingress.k8s.aws/resource":
                "platform-validation/data-pipeline-ingress-data-pipeline-svc-canary:80"}
    labels, dimension = binding.canary_target_group_labels(
        args, bindings, target_group, tags, alb)
    assert labels == {"live-lab.aws/canary-tg-name": "k8s-platform-canary",
                      "live-lab.aws/canary-tg-id": "fedcba9876543210"}
    assert dimension == "targetgroup/k8s-platform-canary/fedcba9876543210"
    target_group["LoadBalancerArns"] = []
    with pytest.raises(binding.BindingError, match="not_attached"):
        binding.canary_target_group_labels(args, bindings, target_group, tags, alb)


@pytest.mark.parametrize("addresses", [
    [{"hostname": "example.ap-northeast-2.elb.amazonaws.com"}, {"hostname": "other.example.test"}],
    [{"hostname": "example.ap-northeast-2.elb.amazonaws.com", "ip": "192.0.2.1"}],
])
def test_ambiguous_ingress_addresses_are_rejected(owned, addresses):
    args, report, ingress, rollout, alb, tags, acl = owned
    ingress["status"]["loadBalancer"]["ingress"] = addresses
    with pytest.raises(binding.BindingError, match="ingress_alb_mismatch"):
        binding.derive_labels(args, report, ingress, rollout, alb, tags, acl)


@pytest.mark.parametrize("field,value", [
    ("session", "other-session"),
    ("alb_cloudwatch_load_balancer_id", "ffffffffffffffff"),
    ("web_acl_arn", "arn:aws:wafv2:ap-northeast-2:123456789012:regional/webacl/foreign/abc"),
])
def test_foreign_or_tampered_waf_evidence_is_rejected(owned, field, value):
    args, report, ingress, rollout, alb, tags, acl = owned
    report[field] = value
    with pytest.raises(binding.BindingError):
        binding.derive_labels(args, report, ingress, rollout, alb, tags, acl)


def test_stale_ingress_or_conflicting_rollout_label_is_rejected(owned):
    args, report, ingress, rollout, alb, tags, acl = owned
    ingress["status"]["loadBalancer"]["ingress"][0]["hostname"] = "old.example.test"
    with pytest.raises(binding.BindingError):
        binding.derive_labels(args, report, ingress, rollout, alb, tags, acl)
    ingress["status"]["loadBalancer"]["ingress"][0]["hostname"] = report["alb_dns"]
    rollout["metadata"]["labels"]["live-lab.aws/alb-id"] = "other"
    with pytest.raises(binding.BindingError):
        binding.derive_labels(args, report, ingress, rollout, alb, tags, acl)
