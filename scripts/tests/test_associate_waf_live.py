import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "platform/live-lab/scripts/associate_waf_live.py"
SPEC = importlib.util.spec_from_file_location("associate_waf_live", SCRIPT)
waf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(waf)

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
SESSION = "delivery-test-01"
APPROVAL = "SS0-20261001-test"
CLUSTER = f"kyobo-{SESSION}"
ALB_DNS = "k8s-platform-svc-a1b2c3d4.ap-northeast-2.elb.amazonaws.com"
ALB_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/k8s-platform-svc-a1b2c3d4/0123456789abcdef"
ACL_ARN = f"arn:aws:wafv2:{REGION}:{ACCOUNT}:regional/webacl/{CLUSTER}-web-acl/01234567-abcd-0123-abcd-0123456789ab"
VPC = "vpc-0123456789abcdef0"


def arguments(**updates):
    values = dict(account=ACCOUNT, region=REGION, session=SESSION, approval=APPROVAL,
                  cluster=CLUSTER, alb_dns=ALB_DNS, web_acl_arn=ACL_ARN,
                  profile="develope-test", evidence="unused.json")
    values.update(updates)
    return SimpleNamespace(**values)


class FakeClient:
    def __init__(self):
        self.meta = SimpleNamespace(region_name=REGION)


class FakeSts(FakeClient):
    def get_caller_identity(self):
        return {"Account": ACCOUNT}


class FakeEks(FakeClient):
    def describe_cluster(self, **kwargs):
        assert kwargs == {"name": CLUSTER}
        return {"cluster": {
            "arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{CLUSTER}",
            "status": "ACTIVE",
            "tags": {"Project": waf.PROJECT, "Session": SESSION, "Approval": APPROVAL},
            "resourcesVpcConfig": {"vpcId": VPC},
        }}


class FakeElb(FakeClient):
    def __init__(self):
        super().__init__()
        self.lbs = [{"DNSName": ALB_DNS, "LoadBalancerArn": ALB_ARN, "Type": "application",
                     "Scheme": "internet-facing", "State": {"Code": "active"}, "VpcId": VPC}]
        self.tags = [{"Key": "Project", "Value": waf.PROJECT}, {"Key": "Session", "Value": SESSION},
                     {"Key": "Approval", "Value": APPROVAL},
                     {"Key": "elbv2.k8s.aws/cluster", "Value": CLUSTER}]

    def describe_load_balancers(self, **kwargs):
        return {"LoadBalancers": self.lbs}

    def describe_tags(self, **kwargs):
        assert kwargs == {"ResourceArns": [ALB_ARN]}
        return {"TagDescriptions": [{"ResourceArn": ALB_ARN, "Tags": self.tags}]}


class FakeWaf(FakeClient):
    def __init__(self):
        super().__init__()
        self.associated = None
        self.associate_calls = []
        self.tags = [{"Key": "Project", "Value": waf.PROJECT}, {"Key": "Session", "Value": SESSION},
                     {"Key": "Approval", "Value": APPROVAL}]

    def list_tags_for_resource(self, **kwargs):
        assert kwargs == {"ResourceARN": ACL_ARN}
        return {"TagInfoForResource": {"TagList": self.tags}}

    def get_web_acl_for_resource(self, **kwargs):
        assert kwargs == {"ResourceArn": ALB_ARN}
        return {"WebACL": {"ARN": self.associated} if self.associated else None}

    def associate_web_acl(self, **kwargs):
        self.associate_calls.append(kwargs)
        self.associated = kwargs["WebACLArn"]
        return {}


def clients():
    return {"sts": FakeSts(), "eks": FakeEks(), "elbv2": FakeElb(), "wafv2": FakeWaf()}


def test_associates_only_the_owned_alb_and_acl(monkeypatch):
    aws = clients()
    writes = []
    monkeypatch.setattr(waf, "write_evidence", lambda path, report: writes.append((path, report)))

    report = waf.associate(arguments(), aws, sleep=lambda _: None)

    assert report["status"] == "verified"
    assert report["operation"] == "associated"
    assert report["alb_cloudwatch_load_balancer_name"] == "k8s-platform-svc-a1b2c3d4"
    assert report["alb_cloudwatch_load_balancer_id"] == "0123456789abcdef"
    assert report["alb_cloudwatch_load_balancer_dimension"] == (
        "app/k8s-platform-svc-a1b2c3d4/0123456789abcdef"
    )
    assert aws["wafv2"].associate_calls == [{"WebACLArn": ACL_ARN, "ResourceArn": ALB_ARN}]
    assert writes[0][1] == report


def test_same_association_is_idempotent(monkeypatch):
    aws = clients()
    aws["wafv2"].associated = ACL_ARN
    monkeypatch.setattr(waf, "write_evidence", lambda *_: None)

    report = waf.associate(arguments(), aws, sleep=lambda _: None)

    assert report["operation"] == "already_associated"
    assert aws["wafv2"].associate_calls == []


def test_refuses_to_replace_a_different_acl(monkeypatch):
    aws = clients()
    aws["wafv2"].associated = "arn:aws:wafv2:ap-northeast-2:123456789012:regional/webacl/other/01234567"
    monkeypatch.setattr(waf, "write_evidence", lambda *_: pytest.fail("must not record unverified association"))

    with pytest.raises(waf.AssociationError, match="foreign_web_acl_association"):
        waf.associate(arguments(), aws)
    assert aws["wafv2"].associate_calls == []


def test_refuses_alb_from_another_vpc(monkeypatch):
    aws = clients()
    aws["elbv2"].lbs[0]["VpcId"] = "vpc-fffffffffffffffff"
    monkeypatch.setattr(waf, "write_evidence", lambda *_: pytest.fail("must not record rejected ALB"))

    with pytest.raises(waf.AssociationError, match="alb_identity_mismatch"):
        waf.associate(arguments(), aws)
    assert aws["wafv2"].associate_calls == []


def test_refuses_alb_with_mismatched_session_tags(monkeypatch):
    aws = clients()
    aws["elbv2"].tags[2]["Value"] = "SS0-20260901-other"
    monkeypatch.setattr(waf, "write_evidence", lambda *_: pytest.fail("must not record rejected ALB"))

    with pytest.raises(waf.AssociationError, match="ownership_mismatch"):
        waf.associate(arguments(), aws)
    assert aws["wafv2"].associate_calls == []


def test_refuses_ambiguous_dns(monkeypatch):
    aws = clients()
    aws["elbv2"].lbs.append(dict(aws["elbv2"].lbs[0]))
    monkeypatch.setattr(waf, "write_evidence", lambda *_: pytest.fail("must not record ambiguous ALB"))

    with pytest.raises(waf.AssociationError, match="alb_dns_missing_or_ambiguous"):
        waf.associate(arguments(), aws)
    assert aws["wafv2"].associate_calls == []


def test_rejects_wrong_waf_arn_before_any_aws_write():
    aws = clients()

    with pytest.raises(waf.AssociationError, match="web_acl_scope_mismatch"):
        waf.associate(arguments(web_acl_arn=ACL_ARN.replace(ACCOUNT, "999999999999")), aws)
    assert aws["wafv2"].associate_calls == []
