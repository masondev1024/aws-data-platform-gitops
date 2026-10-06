"""Keep canary surge and scheduled jobs inside a bounded CPU quota."""

from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_validation_cpu_limit_and_quota_cover_hpa_surge_and_jobs():
    patch = yaml.safe_load((ROOT / "k8s/overlays/validation/patch-rollout.yaml").read_text())
    limits = next(item["value"] for item in patch
                  if item.get("path") == "/spec/template/spec/containers/0/resources/limits")
    quota = next(item for item in yaml.safe_load_all(
        (ROOT / "platform/governance/bootstrap/resource-controls.yaml").read_text())
        if item["kind"] == "ResourceQuota")
    hpa = yaml.safe_load((ROOT / "k8s/base/hpa.yaml").read_text())

    # HPA max (4) + one canary surge pod + two bounded 500m jobs.
    assert limits["cpu"] == "1"
    assert int(quota["spec"]["hard"]["limits.cpu"]) >= hpa["spec"]["maxReplicas"] + 2


@pytest.mark.parametrize("overlay", [
    "k8s/overlays/validation",
    "platform/live-lab/manifests/app",
])
def test_ephemeral_canary_prewarms_stable_capacity_without_changing_base(overlay):
    rendered = subprocess.run(
        ["kubectl", "kustomize", str(ROOT / overlay)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    hpas = [item for item in yaml.safe_load_all(rendered.stdout)
            if item and item.get("kind") == "HorizontalPodAutoscaler"]
    assert len(hpas) == 1
    assert hpas[0]["spec"]["minReplicas"] == 4
    assert hpas[0]["spec"]["maxReplicas"] == 4

    base = yaml.safe_load((ROOT / "k8s/base/hpa.yaml").read_text())
    assert base["spec"]["minReplicas"] == 2
