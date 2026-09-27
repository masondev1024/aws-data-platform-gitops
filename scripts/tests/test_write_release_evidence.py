import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "write_release_evidence.py"
SPEC = importlib.util.spec_from_file_location("write_release_evidence", SCRIPT_PATH)
release_evidence = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(release_evidence)


def test_sha256_file_is_deterministic(tmp_path):
    target = tmp_path / "artifact.txt"
    target.write_text("release evidence\n")

    assert release_evidence.sha256_file(target) == release_evidence.sha256_file(target)


def test_invalid_commit_is_rejected():
    with pytest.raises(ValueError, match="source revision"):
        release_evidence.validate_commit("not-a-commit", "source revision")


def test_release_evidence_can_reference_candidate_without_gitops_revision(tmp_path, monkeypatch):
    source_revision = "0123456789abcdef0123456789abcdef01234567"
    digest = "sha256:" + "b" * 64
    sbom = tmp_path / "sbom.json"
    manifest = tmp_path / "kustomization.yaml"
    candidate = tmp_path / "release-candidate.json"
    output = tmp_path / "release-evidence.json"
    sbom.write_text("{}\n")
    manifest.write_text("images: []\n")
    candidate.write_text("{}\n")

    monkeypatch.setattr(
        release_evidence,
        "parse_args",
        lambda: type(
            "Args",
            (),
            {
                "source_revision": source_revision,
                "gitops_revision": None,
                "image_name": "example.com/data-pipeline-app",
                "image_digest": digest,
                "sbom_path": sbom,
                "manifest_path": manifest,
                "candidate_metadata_path": candidate,
                "publication_mode": "scanned-before-push",
                "output": output,
            },
        )(),
    )

    release_evidence.main()

    evidence = json.loads(output.read_text())
    assert evidence["gitops"]["direct_main_push"] is False
    assert evidence["gitops"]["candidate_metadata_path"] == str(candidate)
    assert evidence["image"]["publication_mode"] == "scanned-before-push"
    assert "revision" not in evidence["gitops"]
