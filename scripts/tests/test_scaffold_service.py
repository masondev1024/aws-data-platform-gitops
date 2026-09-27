import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scaffold_service import scaffold_service


def test_scaffold_generates_a_testable_rollout_golden_path(tmp_path):
    output_dir = tmp_path / "catalog-api"

    written = scaffold_service(
        name="catalog-api",
        owner="team-d2c-platform",
        port=8080,
        output_dir=output_dir,
    )

    assert len(written) == 14
    assert (output_dir / "catalog-info.yaml").read_text().find("team-d2c-platform") >= 0
    rollout = (output_dir / "k8s/base/rollout.yaml").read_text()
    assert "kind: Rollout" in rollout
    assert "readOnlyRootFilesystem: true" in rollout
    assert "replace-with-commit-sha" in rollout
    assert "__SERVICE_NAME__" not in rollout
    assert "/metrics" in (output_dir / "app/app.py").read_text()
    assert (output_dir / "k8s/base/service-monitor.yaml").exists()
    analysis = (output_dir / "k8s/base/analysis-template.yaml").read_text()
    assert "p95-latency" in analysis
    assert '{{args.service-name}}' in analysis
    assert "value: catalog-api-canary" in rollout


def test_scaffold_rejects_invalid_names_and_overwrites(tmp_path):
    with pytest.raises(ValueError, match="service name"):
        scaffold_service(name="Bad_Name", owner="team", port=8080, output_dir=tmp_path / "bad")

    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    (output_dir / "keep.txt").write_text("do not overwrite")
    with pytest.raises(FileExistsError):
        scaffold_service(name="catalog-api", owner="team", port=8080, output_dir=output_dir)
