import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "platform/live-lab/terraform/policies/aws-load-balancer-controller-policy.json"
BOOTSTRAP_PATH = ROOT / "platform/live-lab/scripts/bootstrap_cluster.sh"
MAIN_PATH = ROOT / "platform/live-lab/terraform/main.tf"
VALUES = {
    "region": "ap-northeast-2",
    "account": "123456789012",
    "cluster_name": "kyobo-test-0001",
    "vpc_arn": "arn:aws:ec2:ap-northeast-2:123456789012:vpc/vpc-0123456789abcdef0",
    "waf_arn": "arn:aws:wafv2:ap-northeast-2:123456789012:regional/webacl/kyobo-test-0001-web-acl/01234567-abcd-0123-abcd-0123456789ab",
}
CLUSTER_TAG = "elbv2.k8s.aws/cluster"


def rendered_policy(values=None):
    raw = POLICY_PATH.read_text(encoding="utf-8")
    for key, value in (values or VALUES).items():
        raw = raw.replace("${" + key + "}", value)
    assert "${" not in raw
    return raw, json.loads(raw)


def statements_for(policy, action):
    return [
        statement for statement in policy["Statement"]
        if action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    ]


def test_rendered_policy_is_below_iam_size_limit_and_scoped():
    rendered, policy = rendered_policy()
    assert len(bytes(c for c in rendered.encode() if c not in b" \t\r\n")) <= 6144

    wildcards = [
        action
        for statement in policy["Statement"]
        if statement["Resource"] == "*"
        for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    ]
    assert "iam:CreateServiceLinkedRole" in wildcards
    assert all(action.startswith(("ec2:Describe", "ec2:Get", "elasticloadbalancing:Describe",
                                 "acm:", "cognito-idp:Describe", "iam:Get", "iam:List",
                                 "wafv2:Get")) for action in wildcards if action != "iam:CreateServiceLinkedRole")
    slr = statements_for(policy, "iam:CreateServiceLinkedRole")[0]
    assert slr["Condition"]["StringEquals"]["iam:AWSServiceName"] == "elasticloadbalancing.amazonaws.com"


def test_policy_fits_even_at_the_longest_accepted_session_name():
    values = dict(VALUES)
    values["cluster_name"] = "kyobo-" + "a" * 41
    values["waf_arn"] = values["waf_arn"].replace(VALUES["cluster_name"], values["cluster_name"])
    _, policy = rendered_policy(values)
    assert len(json.dumps(policy, separators=(",", ":")).encode()) <= 6144


def test_elbv2_mutations_bind_region_account_and_cluster_tag():
    _, policy = rendered_policy()
    create = statements_for(policy, "elasticloadbalancing:CreateLoadBalancer")[0]
    assert create["Resource"] == f"arn:aws:elasticloadbalancing:{VALUES['region']}:{VALUES['account']}:*"
    assert create["Condition"]["StringEquals"][f"aws:RequestTag/{CLUSTER_TAG}"] == VALUES["cluster_name"]

    listener = statements_for(policy, "elasticloadbalancing:CreateListener")[0]
    rule = statements_for(policy, "elasticloadbalancing:CreateRule")[0]
    assert listener["Resource"].endswith(":loadbalancer/app/*/*")
    assert rule["Resource"].endswith(":listener/app/*/*/*")
    for statement in (listener, rule):
        tags = statement["Condition"]["StringEquals"]
        assert tags[f"aws:ResourceTag/{CLUSTER_TAG}"] == VALUES["cluster_name"]
        assert tags[f"aws:RequestTag/{CLUSTER_TAG}"] == VALUES["cluster_name"]

    create_tags = next(statement for statement in statements_for(policy, "elasticloadbalancing:AddTags")
                       if "elasticloadbalancing:CreateAction" in statement.get("Condition", {}).get("StringEquals", {}))
    assert set(create_tags["Condition"]["StringEquals"]["elasticloadbalancing:CreateAction"]) == {
        "CreateLoadBalancer", "CreateTargetGroup", "CreateListener", "CreateRule"
    }
    resource_arns = create_tags["Resource"]
    assert resource_arns == f"arn:aws:elasticloadbalancing:{VALUES['region']}:{VALUES['account']}:*"
    assert create_tags["Condition"]["StringEquals"][f"aws:RequestTag/{CLUSTER_TAG}"] == VALUES["cluster_name"]

    for statement in policy["Statement"]:
        actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        if any(action.startswith("elasticloadbalancing:") and action not in {
            "elasticloadbalancing:AddTags", "elasticloadbalancing:CreateLoadBalancer",
            "elasticloadbalancing:CreateTargetGroup", "elasticloadbalancing:CreateListener",
            "elasticloadbalancing:CreateRule", "elasticloadbalancing:DescribeListenerAttributes",
            "elasticloadbalancing:DescribeListeners", "elasticloadbalancing:DescribeListenerCertificates",
            "elasticloadbalancing:DescribeLoadBalancerAttributes", "elasticloadbalancing:DescribeLoadBalancers",
            "elasticloadbalancing:DescribeRules", "elasticloadbalancing:DescribeSSLPolicies",
            "elasticloadbalancing:DescribeTags", "elasticloadbalancing:DescribeTargetGroupAttributes",
            "elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeTargetHealth",
            "elasticloadbalancing:GetLoadBalancerWebACL",
        } for action in actions):
            assert statement["Resource"] != "*"
            assert statement["Condition"]["StringEquals"][f"aws:ResourceTag/{CLUSTER_TAG}"] == VALUES["cluster_name"]


def test_security_group_and_wafv2_authority_is_vpc_and_session_bounded():
    _, policy = rendered_policy()
    ingress = statements_for(policy, "ec2:AuthorizeSecurityGroupIngress")[0]
    assert ingress["Resource"] == f"arn:aws:ec2:{VALUES['region']}:{VALUES['account']}:security-group/*"
    assert ingress["Condition"]["ArnEquals"]["ec2:Vpc"] == VALUES["vpc_arn"]

    create_sg = statements_for(policy, "ec2:CreateSecurityGroup")
    assert len(create_sg) == 2
    assert VALUES["vpc_arn"] in [item["Resource"] for item in create_sg]
    scoped_sg = next(item for item in create_sg if isinstance(item["Resource"], str)
                     and item["Resource"].endswith(":security-group/*"))
    assert scoped_sg["Condition"]["ArnEquals"]["ec2:Vpc"] == VALUES["vpc_arn"]
    assert scoped_sg["Condition"]["StringEquals"][f"aws:RequestTag/{CLUSTER_TAG}"] == VALUES["cluster_name"]

    associate = statements_for(policy, "wafv2:AssociateWebACL")[0]
    assert associate["Resource"] == VALUES["waf_arn"]
    alb_association = statements_for(policy, "elasticloadbalancing:CreateWebACLAssociation")[0]
    assert alb_association["Resource"] == (
        f"arn:aws:elasticloadbalancing:{VALUES['region']}:{VALUES['account']}:*"
    )
    assert alb_association["Condition"]["StringEquals"][f"aws:ResourceTag/{CLUSTER_TAG}"] == VALUES["cluster_name"]

    all_actions = [action for statement in policy["Statement"]
                   for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])]
    assert not any(action.startswith(("shield:", "waf-regional:")) for action in all_actions)
    assert "elasticloadbalancing:SetWebAcl" not in all_actions


def test_terraform_renders_scope_and_helm_disables_unscoped_features():
    main = MAIN_PATH.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    assert 'templatefile("${path.module}/policies/aws-load-balancer-controller-policy.json"' in main
    for value in ("region", "account", "cluster_name", "vpc_arn", "waf_arn"):
        assert re.search(rf"\b{value}\s*=", main)
    assert "--set enableShield=false --set enableWaf=false --set enableWafv2=true" in bootstrap
