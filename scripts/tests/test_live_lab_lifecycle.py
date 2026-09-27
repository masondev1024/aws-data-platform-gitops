import json
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "platform" / "live-lab" / "scripts"))
import live_lab_lifecycle


EXPECTED = {
    "Project": "kyobo-platform-live-lab",
    "Session": "live-260923-test",
    "Approval": "SS0-20260923-codex-live-lab",
}
ACCOUNT_ID = "854745312525"
ARN = f"arn:aws:ec2:ap-northeast-2:{ACCOUNT_ID}:security-group-rule/sgr-1234567890abcdef0"
RULE_ID = "sgr-1234567890abcdef0"


def source_file(tmp_path: Path, arn: str = ARN) -> Path:
    path = tmp_path / "candidates.json"
    path.write_text(
        json.dumps({
            "PaginationToken": "",
            "ResourceTagMappingList": [{
                "ResourceARN": arn,
                "Tags": [{"Key": key, "Value": value} for key, value in EXPECTED.items()],
            }],
        }),
        encoding="utf-8",
    )
    return path


def empty_source_file(tmp_path: Path) -> Path:
    path = tmp_path / "empty-candidates.json"
    path.write_text(json.dumps({"PaginationToken": "", "ResourceTagMappingList": []}), encoding="utf-8")
    return path


def runner(describe_response, *, account_id: str = ACCOUNT_ID, calls=None):
    calls = calls if calls is not None else []

    def aws_runner(command, **kwargs):
        calls.append(command)
        if "get-caller-identity" in command:
            return subprocess.CompletedProcess(command, 0, f"{account_id}\n", "")
        return describe_response(command)

    return aws_runner


def not_found(command):
    return subprocess.CompletedProcess(
        command, 254, "", "An error occurred (InvalidSecurityGroupRuleId.NotFound)"
    )


def test_reconcile_inventory_separates_deleted_rule_from_historical_tag_index(tmp_path):
    source = source_file(tmp_path)
    target = tmp_path / "inventory.json"
    calls = []

    result = live_lab_lifecycle.reconcile_inventory(
        str(source), str(target), EXPECTED, profile="develope-test", region="ap-northeast-2",
        account_id=ACCOUNT_ID, aws_runner=runner(not_found, calls=calls),
    )

    report = json.loads(target.read_text(encoding="utf-8"))
    assert result == {"live": 0, "stale": 1, "unresolved": 0}
    assert "get-caller-identity" in calls[0]
    assert "describe-security-group-rules" in calls[1]
    assert RULE_ID in calls[1]
    assert report["status"] == "no_live_tagged_resources_observed"
    assert report["resources"] == []
    assert report["stale_tag_index_entries"] == [{
        "arn": ARN,
        "tags": source_file_tags(),
        "verification": "ec2_describe_security_group_rules_not_found",
    }]


def test_reconcile_inventory_fails_closed_on_caller_account_mismatch(tmp_path):
    source = source_file(tmp_path)
    calls = []

    result = live_lab_lifecycle.reconcile_inventory(
        str(source), str(tmp_path / "inventory.json"), EXPECTED,
        profile="develope-test", region="ap-northeast-2", account_id=ACCOUNT_ID,
        aws_runner=runner(not_found, account_id="999999999999", calls=calls),
    )

    report = json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))
    assert result == {"live": 0, "stale": 0, "unresolved": 1}
    assert len(calls) == 1
    assert report["status"] == "reconciliation_incomplete"
    assert report["unresolved_resources"][0]["verification"] == "caller_account_mismatch"


@pytest.mark.parametrize(("returncode", "caller_account", "verification"), [
    (0, "999999999999", "caller_account_mismatch"),
    (254, "", "caller_identity_unverified"),
])
def test_empty_inventory_still_fails_closed_when_identity_is_not_verified(
    tmp_path, returncode, caller_account, verification
):
    source = empty_source_file(tmp_path)
    calls = []

    def aws_runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, f"{caller_account}\n", "AccessDenied")

    result = live_lab_lifecycle.reconcile_inventory(
        str(source), str(tmp_path / "inventory.json"), EXPECTED,
        profile="develope-test", region="ap-northeast-2", account_id=ACCOUNT_ID,
        aws_runner=aws_runner,
    )

    report = json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))
    assert result == {"live": 0, "stale": 0, "unresolved": 1}
    assert len(calls) == 1
    assert report["status"] == "reconciliation_incomplete"
    assert report["unresolved_resources"] == [{"arn": None, "verification": verification}]


def test_reconcile_inventory_preserves_unknowns_over_previous_report(tmp_path):
    source = source_file(tmp_path)
    target = tmp_path / "inventory.json"
    target.write_text('{"status":"no_live_tagged_resources_observed"}\n', encoding="utf-8")
    document = json.loads(source.read_text(encoding="utf-8"))
    document["ResourceTagMappingList"][0]["ResourceARN"] = (
        f"arn:aws:ec2:ap-northeast-2:{ACCOUNT_ID}:security-group/sg-1234567890abcdef0"
    )
    source.write_text(json.dumps(document), encoding="utf-8")
    calls = []

    result = live_lab_lifecycle.reconcile_inventory(
        str(source), str(target), EXPECTED, profile="develope-test", region="ap-northeast-2",
        account_id=ACCOUNT_ID, aws_runner=runner(not_found, calls=calls),
    )

    report = json.loads(target.read_text(encoding="utf-8"))
    assert result == {"live": 0, "stale": 0, "unresolved": 1}
    assert len(calls) == 1
    assert report["status"] == "reconciliation_incomplete"
    assert report["unresolved_resources"] == [{
        "arn": document["ResourceTagMappingList"][0]["ResourceARN"],
        "verification": "unsupported_resource_type",
    }]


def test_reconcile_inventory_keeps_existing_tagged_resources_live(tmp_path):
    source = source_file(tmp_path)

    def describe(command):
        return subprocess.CompletedProcess(
            command, 0,
            json.dumps({"SecurityGroupRules": [{"SecurityGroupRuleId": RULE_ID}]}),
            "",
        )

    result = live_lab_lifecycle.reconcile_inventory(
        str(source), str(tmp_path / "inventory.json"), EXPECTED,
        profile="develope-test", region="ap-northeast-2", account_id=ACCOUNT_ID,
        aws_runner=runner(describe),
    )

    report = json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))
    assert result == {"live": 1, "stale": 0, "unresolved": 0}
    assert report["status"] == "tagged_residuals_found"
    assert report["resources"][0]["arn"] == ARN
    assert report["resources"][0]["verification"] == "ec2_describe_security_group_rules_found"
    assert report["stale_tag_index_entries"] == []


@pytest.mark.parametrize("response", [
    [],
    {},
    {"SecurityGroupRules": [{}]},
    {"SecurityGroupRules": [{"SecurityGroupRuleId": "sgr-another-rule"}]},
    {"SecurityGroupRules": []},
    {"SecurityGroupRules": [{"SecurityGroupRuleId": RULE_ID}], "NextToken": "next-page"},
])
def test_reconcile_inventory_never_treats_ambiguous_ec2_success_as_absence(tmp_path, response):
    source = source_file(tmp_path)

    def describe(command):
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    result = live_lab_lifecycle.reconcile_inventory(
        str(source), str(tmp_path / "inventory.json"), EXPECTED,
        profile="develope-test", region="ap-northeast-2", account_id=ACCOUNT_ID,
        aws_runner=runner(describe),
    )

    report = json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))
    assert result == {"live": 0, "stale": 0, "unresolved": 1}
    assert report["stale_tag_index_entries"] == []
    assert report["unresolved_resources"][0]["verification"] == "ec2_response_ambiguous"


@pytest.mark.parametrize("arn", [
    "arn:aws:ec2:ap-northeast-2:999999999999:security-group-rule/sgr-1234567890abcdef0",
    "arn:aws-us-gov:ec2:ap-northeast-2:854745312525:security-group-rule/sgr-1234567890abcdef0",
    "arn:aws:ec2:us-east-1:854745312525:security-group-rule/sgr-1234567890abcdef0",
])
def test_reconcile_inventory_fails_closed_when_candidate_arn_is_out_of_scope(tmp_path, arn):
    source = source_file(tmp_path, arn=arn)
    calls = []

    result = live_lab_lifecycle.reconcile_inventory(
        str(source), str(tmp_path / "inventory.json"), EXPECTED,
        profile="develope-test", region="ap-northeast-2", account_id=ACCOUNT_ID,
        aws_runner=runner(not_found, calls=calls),
    )

    report = json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))
    assert result == {"live": 0, "stale": 0, "unresolved": 1}
    assert len(calls) == 1
    assert report["unresolved_resources"][0]["verification"] == "arn_scope_mismatch"


def test_reconcile_inventory_records_ec2_authorization_failure_as_unknown(tmp_path):
    source = source_file(tmp_path)

    def access_denied(command):
        return subprocess.CompletedProcess(command, 254, "", "AccessDenied")

    result = live_lab_lifecycle.reconcile_inventory(
        str(source), str(tmp_path / "inventory.json"), EXPECTED,
        profile="develope-test", region="ap-northeast-2", account_id=ACCOUNT_ID,
        aws_runner=runner(access_denied),
    )

    report = json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))
    assert result == {"live": 0, "stale": 0, "unresolved": 1}
    assert report["unresolved_resources"][0]["verification"] == "service_verification_failed"


def source_file_tags():
    return [{"Key": key, "Value": value} for key, value in EXPECTED.items()]
