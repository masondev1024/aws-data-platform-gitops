"""Offline protocol/policy tests only: no credentials, network, or model calls."""

from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from agentic_ops.agent import AgentExecutionError, IncidentTriageAgent
from agentic_ops.backends import backend_preflight
from agentic_ops.bedrock_converse import BedrockConverseBackend
from agentic_ops.contracts import IncidentBundle, utc_now
from agentic_ops.live_evaluation import CASES, evaluate_case, replay_bundle
from agentic_ops.tools import TOOL_DEFINITIONS
import evaluate_agentic_live
import triage_incident


class Client:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def converse(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return next(self.responses)


def response(content, stop="tool_use"):
    return {"output": {"message": {"role": "assistant", "content": content}},
            "stopReason": stop, "usage": {"inputTokens": 100, "outputTokens": 20}}


def tool(name="inspect_data_integrity", call_id="one", arguments=None):
    return {"toolUse": {"toolUseId": call_id, "name": name,
                        "input": {} if arguments is None else arguments}}


def final(case):
    return response([{"text": json.dumps({
        "diagnosis": case.diagnosis, "confidence": "high", "evidence_refs": sorted(case.evidence),
        "unknowns": sorted(case.unknowns), "rationale": "Offline protocol test, not a model evaluation.",
    })}], "end_turn")


def backend(client):
    return BedrockConverseBackend("apac.amazon.nova-lite-v1:0", profile="test", region="ap-northeast-2", client=client)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_multiturn_tool_results_usage_and_all_fixed_fixture_scoring(case):
    client = Client(response([tool("inspect_api_health", "api")]),
                    response([tool("inspect_data_integrity", "db")]),
                    response([tool("inspect_rollout_health", "rollout")]), final(case))
    result = evaluate_case(backend(client), replay_bundle(case), case=case)
    assert result["pass"] is True
    assert result["mode"] == "replay-model-eval"
    assert result["model_calls"] == 4
    assert (result["input_tokens"], result["output_tokens"]) == (400, 80)
    assert result["elapsed_ms"] >= 0
    messages = client.requests[-1]["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant", "user", "assistant", "user"]
    assert messages[1]["content"][0]["toolUse"]["toolUseId"] == "api"
    assert messages[2]["content"][0]["toolResult"]["toolUseId"] == "api"
    observations = messages[2]["content"][0]["toolResult"]["content"][0]["json"]["observations"]
    assert observations[0]["evidence_ref"] == "api.apply_samples_5m"
    request = client.requests[0]
    assert request["inferenceConfig"]["maxTokens"] == 400
    assert request["toolConfig"]["toolChoice"] == {"auto": {}}
    assert [t["toolSpec"]["name"] for t in request["toolConfig"]["tools"]] == [t["name"] for t in TOOL_DEFINITIONS]
    assert request["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["additionalProperties"] is False


@pytest.mark.parametrize("block,reason", [
    (tool("delete_namespace"), "tool_not_allowlisted"),
    (tool(arguments={"command": "secret_value"}), "tool_arguments_not_allowed"),
    (tool(arguments="not-json"), "tool_arguments_not_allowed"),
    (tool(arguments=[]), "tool_arguments_not_allowed"),
])
def test_unknown_or_malformed_arguments_are_refused_through_same_allowlist(block, reason):
    case = CASES[-1]
    client = Client(response([block]), response([tool(call_id="two")]), final(case))
    result = evaluate_case(backend(client), replay_bundle(case), case=case)
    assert result["pass"]
    refusal = client.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert refusal["status"] == "error"
    assert refusal["content"] == [{"json": {"status": "rejected", "reason": reason}}]
    assert "secret_value" not in json.dumps(result)


@pytest.mark.parametrize("stop", ["max_tokens", "stop_sequence", "guardrail_intervened", "content_filtered", "unknown"])
def test_incomplete_responses_fail_closed_and_still_meter_usage(stop):
    client = Client(response([tool()], stop))
    result = evaluate_case(backend(client), replay_bundle(CASES[-1]), case=CASES[-1])
    assert not result["pass"]
    assert result["reason"] == "model_response_incomplete"
    assert result["input_tokens"] == 100
    assert result["output_tokens"] == 20
    assert len(client.requests) == 1


@pytest.mark.parametrize("payload", [
    {}, {"output": None}, response([]), response([{"toolUse": {}}]),
    response([tool(call_id="")]), response([tool(arguments=float("nan"))]),
    response([tool()], "end_turn"), response([{"text": "{}"}], "tool_use"),
    response([{"text": "ok", "toolUse": {}}]), response([{"image": {}}]),
    response([tool(call_id="same"), tool(call_id="same")]),
])
def test_malformed_provider_envelopes_are_incomplete(payload):
    assert backend(Client(payload)).respond([]).status == "incomplete"


def test_parallel_tools_never_execute_and_usage_survives_refusal():
    client = Client(response([tool(call_id="one"), tool("inspect_api_health", "two")]))
    result = evaluate_case(backend(client), replay_bundle(CASES[-1]), case=CASES[-1])
    assert result["reason"] == "parallel_tool_call_rejected"
    assert result["model_calls"] == 1
    assert result["input_tokens"] == 100


def test_fourth_tool_request_is_not_executed():
    client = Client(*(response([tool(call_id=str(n))]) for n in range(4)))
    result = evaluate_case(backend(client), replay_bundle(CASES[-1]), case=CASES[-1])
    assert result["reason"] == "agent_tool_budget_exhausted"
    assert result["model_calls"] == 4
    assert len(client.requests[-1]["messages"]) == 7


def test_provider_errors_do_not_leak_secrets_and_usage_is_explicitly_partial():
    class FailingClient:
        def converse(self, **kwargs):
            raise RuntimeError("AWS_SECRET_ACCESS_KEY=secret_value")
    result = evaluate_case(backend(FailingClient()), replay_bundle(CASES[-1]), case=CASES[-1])
    assert result["reason"] == "model_request_failed"
    assert result["usage_status"] == "partial_provider_failure"
    assert "secret_value" not in json.dumps(result)


def test_input_size_bound_prevents_network_call():
    client = Client()
    with pytest.raises(ValueError, match="converse_input_budget_exhausted"):
        backend(client).respond([{"role": "user", "content": "x" * 64001}])
    assert client.requests == []


@pytest.mark.parametrize("usage", [None, {}, {"inputTokens": -1, "outputTokens": 3},
                                    {"inputTokens": "10", "outputTokens": 3}])
def test_missing_or_invalid_usage_is_not_reported_as_known_zero(usage):
    payload = response([tool()])
    payload["usage"] = usage
    result = evaluate_case(backend(Client(payload)), replay_bundle(CASES[-1]), case=CASES[-1])
    assert result["reason"] == "model_response_incomplete"
    assert result["usage_status"] == "partial_usage_missing"


def test_requests_validate_against_installed_aws_sdk_schema_without_network():
    session = pytest.importorskip("botocore.session")
    from botocore.validate import validate_parameters
    shape = session.get_session().get_service_model("bedrock-runtime").operation_model("Converse").input_shape
    case = CASES[-1]
    client = Client(response([tool()]), final(case))
    assert evaluate_case(backend(client), replay_bundle(case), case=case)["pass"]
    for request in client.requests:
        validate_parameters(request, shape)


def test_sdk_uses_explicit_profile_region_and_no_retries(monkeypatch):
    captured = {}
    class Session:
        def __init__(self, **kwargs):
            captured["session"] = kwargs
        def client(self, name, **kwargs):
            captured["client"] = (name, kwargs)
            return Client()
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=Session))
    monkeypatch.setitem(sys.modules, "botocore.config", SimpleNamespace(Config=lambda **kwargs: kwargs))
    BedrockConverseBackend("model", profile="develope-test", region="ap-northeast-2")
    assert captured["session"] == {"profile_name": "develope-test", "region_name": "ap-northeast-2"}
    assert captured["client"][0] == "bedrock-runtime"
    config = captured["client"][1]["config"]
    assert config["retries"]["total_max_attempts"] == 1
    assert config["read_timeout"] == 25


def test_cli_gates_openai_default_and_cross_region(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = triage_incident.parse_args(["--bundle", "unused"])
    assert args.backend == "openai"
    assert backend_preflight(args).startswith("pass --live-llm")
    args.live_llm = True
    assert backend_preflight(args) == "OPENAI_API_KEY is not configured"
    args.backend = "bedrock"
    assert backend_preflight(args) == "bedrock_requires_explicit_model_profile_region"
    args.model, args.aws_profile, args.aws_region = "apac.amazon.nova-lite-v1:0", "develope-test", "ap-northeast-2"
    assert backend_preflight(args) == "cross_region_inference_requires_explicit_approval_flag"
    args.allow_cross_region_inference = True
    assert backend_preflight(args) is None
    assert evaluate_agentic_live.main(["--mode", "replay-model-eval"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "not_run"


def authorized_args(mode="replay-model-eval"):
    return ["--mode", mode, "--live-llm", "--backend", "bedrock", "--model", "apac.amazon.nova-lite-v1:0",
            "--aws-profile", "test", "--aws-region", "ap-northeast-2", "--allow-cross-region-inference"]


def test_runner_safe_private_output_and_no_overwrite(monkeypatch, tmp_path, capsys):
    case = CASES[-1]
    client = Client(response([tool()]), final(case))
    monkeypatch.setattr(evaluate_agentic_live, "create_backend", lambda args: backend(client))
    target = tmp_path / "report.json"
    args = authorized_args() + ["--case", "parity", "--case", "parity", "--private-output", str(target)]
    assert evaluate_agentic_live.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["cases"]) == 1
    assert report["cases"][0]["pass"]
    assert target.stat().st_mode & 0o777 == 0o600
    original = target.read_text()
    assert "rationale" not in original
    assert evaluate_agentic_live.main(args) == 2
    assert target.read_text() == original
    assert len(client.requests) == 2


@pytest.mark.parametrize("replay,old", [(True, False), (False, True)])
def test_fresh_mode_rejects_replay_or_expired_bundles_before_backend_creation(monkeypatch, tmp_path, replay, old):
    document = replay_bundle(CASES[0]).to_dict()
    document["mode"] = "replay" if replay else "live_observation"
    if old:
        document["collected_at"] = (utc_now() - timedelta(hours=1)).isoformat()
    target = tmp_path / "bundle.json"
    target.write_text(json.dumps(document))
    def fail(args):
        pytest.fail("backend must not be created")
    monkeypatch.setattr(evaluate_agentic_live, "create_backend", fail)
    assert evaluate_agentic_live.main(authorized_args("fresh-real-observation") + ["--bundle", str(target)]) == 2


def test_fresh_evaluation_is_policy_validation_not_ground_truth():
    case = CASES[-1]
    document = replay_bundle(case).to_dict()
    document["mode"] = "live_observation"
    result = evaluate_case(backend(Client(response([tool()]), final(case))), IncidentBundle.from_dict(document))
    assert result["mode"] == "fresh-real-observation"
    assert result["assessment"] == "evidence_policy_only_no_ground_truth"
    assert result["expected_diagnosis"] is None


def test_fixture_values_repeat_and_stale_source_remains_unknown():
    now = utc_now()
    for case in CASES:
        assert replay_bundle(case, now=now).to_dict() == replay_bundle(case, now=now).to_dict()
    stale = replay_bundle(CASES[4])
    assert stale.observations["api.apply_5xx_ratio"].reason == "prometheus_source_stale"
    assert stale.observations["api.scrape_age_seconds"].value == 300


def test_expired_replay_is_refused_without_model_calls():
    case = CASES[-1]
    result = evaluate_case(backend(Client()), replay_bundle(case, now=utc_now() - timedelta(hours=1)), case=case)
    assert result["reason"] == "observation_bundle_is_stale"
    assert result["model_calls"] == 0
