"""Run the production expressions against Prometheus, not a Python approximation.

Use PROMTOOL=/path/to/promtool or opt into an already-local Docker image with
PROMTOOL_DOCKER_IMAGE=prom/prometheus:v2.54.1. No network or cluster is used.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentic_ops.prometheus import QUERY_SPECS


def gate_query():
    template = yaml.safe_load((ROOT / "k8s/base/analysis-template.yaml").read_text())
    metric = next(m for m in template["spec"]["metrics"] if m["name"] == "canary-apply-outbox-parity")
    assert metric["successCondition"] == "result[0] == 0"
    return metric["provider"]["prometheus"]["query"]


def test_gate_and_agent_use_identical_parity_semantics():
    gate = gate_query().replace('{service="{{args.service-name}}"}', "")
    assert " ".join(gate.split()) == " ".join(QUERY_SPECS["data.outbox_parity_gap"][0].split())


def parity_panel():
    resource = yaml.safe_load((ROOT / "k8s/base/grafana-dashboard.yaml").read_text())
    dashboard = json.loads(resource["data"]["raffle-sre-overview.json"])
    return next(panel for panel in dashboard["panels"] if panel["id"] == 6)


def test_dashboard_preserves_query_semantics_and_marks_unknown_red():
    panel = parity_panel()
    assert panel["targets"][0]["expr"] == QUERY_SPECS["data.outbox_parity_gap"][0]
    mappings = panel["fieldConfig"]["defaults"]["mappings"]
    unknown = next(m["options"]["-1"] for m in mappings if m["type"] == "value" and "-1" in m["options"])
    assert unknown == {"text": "UNAVAILABLE", "color": "red"}


CASES = [
    ("all_zero", [0, 0], 0),
    ("one_failed", [-1, 0], -1),
    ("one_failed_reversed", [0, -1], -1),
    ("one_discrepancy", [0, 1], 1),
    ("one_discrepancy_reversed", [1, 0], 1),
    ("failed_and_discrepancy", [-1, 1], -1),
    ("all_failed", [-1, -1], -1),
    ("largest_discrepancy", [2, 7, 0], 7),
    ("single_healthy", [0], 0),
    ("single_discrepancy", [3], 3),
    ("no_series", [], -1),
]


@pytest.mark.parametrize("consumer", ["gate", "agent", "dashboard"])
def test_production_promql_mixed_pods_fail_closed(tmp_path, consumer):
    expression = (gate_query().replace("{{args.service-name}}", "canary") if consumer == "gate"
                  else QUERY_SPECS["data.outbox_parity_gap"][0])
    if consumer == "dashboard":
        expression = parity_panel()["targets"][0]["expr"]
    tests = []
    for name, values, expected in CASES:
        tests.append({
            "name": name, "interval": "1m",
            "input_series": [{"series": f'raffle_apply_outbox_parity_gap{{service="canary",pod="p{i}"}}',
                              "values": str(value)} for i, value in enumerate(values)],
            "promql_expr_test": [
                {"expr": expression, "eval_time": "0m", "exp_samples": [{"labels": "{}", "value": expected}]},
                {"expr": f"({expression}) == bool 0", "eval_time": "0m",
                 "exp_samples": [{"labels": "{}", "value": int(expected == 0)}]},
            ],
        })
    if consumer == "gate":
        tests.append({
            "name": "preserve_canary_service_scope", "interval": "1m",
            "input_series": [
                {"series": 'raffle_apply_outbox_parity_gap{service="canary",pod="a"}', "values": "0"},
                {"series": 'raffle_apply_outbox_parity_gap{service="stable",pod="b"}', "values": "-1"},
            ],
            "promql_expr_test": [{"expr": expression, "eval_time": "0m",
                                  "exp_samples": [{"labels": "{}", "value": 0}]}],
        })
    fixture = tmp_path / "parity.yaml"
    fixture.write_text(yaml.safe_dump({"evaluation_interval": "1m", "tests": tests}))
    promtool = os.environ.get("PROMTOOL") or shutil.which("promtool")
    docker_image = os.environ.get("PROMTOOL_DOCKER_IMAGE")
    if promtool:
        command = [promtool, "test", "rules", str(fixture)]
    elif docker_image:
        command = ["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
                   "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
                   "--cpus=0.5", "--memory=256m", "--cap-drop=ALL",
                   "--security-opt=no-new-privileges", "--entrypoint=/bin/promtool",
                   "-v", f"{tmp_path}:/tests:ro", docker_image, "test", "rules", "/tests/parity.yaml"]
    else:
        pytest.skip("Set PROMTOOL or PROMTOOL_DOCKER_IMAGE for actual PromQL evaluation")
    run = subprocess.run(command, capture_output=True, text=True, timeout=45)
    assert run.returncode == 0, run.stdout + run.stderr
