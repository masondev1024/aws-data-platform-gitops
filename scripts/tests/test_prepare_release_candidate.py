import importlib.util
import json
from pathlib import Path
import argparse

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "prepare_release_candidate.py"
SPEC = importlib.util.spec_from_file_location("prepare_release_candidate", SCRIPT_PATH)
candidate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(candidate)


VALID_SHA = "0123456789abcdef0123456789abcdef01234567"
VALID_DIGEST = "sha256:" + "a" * 64
IMAGE_NAME = "123456789012.dkr.ecr.eu-west-1.amazonaws.com/data-pipeline-app"
EXPECTED_REFERENCE = f"{IMAGE_NAME}:{VALID_SHA}@{VALID_DIGEST}"


def base_manifest() -> str:
    return """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
images:
  - name: data-pipeline-app
    newName: old.example.com/data-pipeline-app
    newTag: oldtag
"""


def test_candidate_manifest_uses_digest_without_mutating_source_text():
    original = base_manifest()

    rendered = candidate.build_candidate_manifest(
        original,
        image_name=IMAGE_NAME,
        source_revision=VALID_SHA,
        image_digest=VALID_DIGEST,
    )

    assert "old.example.com" in original
    assert f"newName: {IMAGE_NAME}" in rendered
    assert f"newTag: {VALID_SHA}" in rendered
    assert f"digest: {VALID_DIGEST}" in rendered


def test_candidate_manifest_rejects_multiple_image_overrides():
    manifest = base_manifest() + "  - name: other-image\n    newName: example.com/other\n"

    with pytest.raises(ValueError, match="exactly one image override"):
        candidate.build_candidate_manifest(
            manifest,
            image_name=IMAGE_NAME,
            source_revision=VALID_SHA,
            image_digest=VALID_DIGEST,
        )


def test_image_digest_must_be_sha256():
    with pytest.raises(ValueError, match="sha256"):
        candidate.validate_image_digest("latest")


def test_image_name_must_not_include_tag_or_digest():
    with pytest.raises(ValueError, match="must not include"):
        candidate.validate_image_name(f"{IMAGE_NAME}:latest")
    with pytest.raises(ValueError, match="must not include"):
        candidate.validate_image_name(f"{IMAGE_NAME}@{VALID_DIGEST}")


def test_rendered_manifest_may_reuse_same_app_image_across_resources():
    rendered = f"""
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
      - image: {EXPECTED_REFERENCE}
---
apiVersion: batch/v1
kind: Job
spec:
  template:
    spec:
      containers:
      - image: {EXPECTED_REFERENCE}
---
apiVersion: batch/v1
kind: CronJob
spec:
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - image: {EXPECTED_REFERENCE}
"""

    assert candidate.validate_rendered_images(rendered, EXPECTED_REFERENCE) == 3


def test_rendered_manifest_rejects_unexpected_image_reference():
    rendered = f"""
kind: Pod
spec:
  containers:
  - image: {EXPECTED_REFERENCE}
  - image: docker.io/library/busybox:latest
"""

    with pytest.raises(ValueError, match="unexpected image"):
        candidate.validate_rendered_images(rendered, EXPECTED_REFERENCE)


@pytest.mark.parametrize(
    ("target", "manifest_name"),
    [("prod", "prod-kustomization.yaml"), ("validation", "validation-kustomization.yaml")],
)
def test_candidate_target_writes_an_allowlisted_manifest_and_binds_source_hash(
    tmp_path, monkeypatch, target, manifest_name
):
    source = tmp_path / "kustomization.yaml"
    source.write_text(base_manifest(), encoding="utf-8")
    output = tmp_path / "candidate"
    monkeypatch.setattr(
        candidate,
        "parse_args",
        lambda: argparse.Namespace(
            source_revision=VALID_SHA,
            image_name=IMAGE_NAME,
            image_digest=VALID_DIGEST,
            manifest_path=source,
            rendered_manifest_path=None,
            output_dir=output,
            candidate_target=target,
        ),
    )

    candidate.main()

    generated_manifest = output / manifest_name
    metadata = json.loads((output / "release-candidate.json").read_text(encoding="utf-8"))
    assert generated_manifest.is_file()
    assert metadata["target"] == target
    assert metadata["gitops_candidate"]["source_manifest_sha256"] == candidate.sha256_text(
        base_manifest()
    )
    assert generated_manifest.read_text(encoding="utf-8") == candidate.build_candidate_manifest(
        base_manifest(), image_name=IMAGE_NAME, source_revision=VALID_SHA, image_digest=VALID_DIGEST
    )
