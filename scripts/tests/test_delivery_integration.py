"""Delivery-lane integration tests; synthetic fixtures are not live deployment proof."""

import json
from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE = "registry.example.test/raffle"


def test_new_ephemeral_database_cannot_silently_enroll_in_paid_extended_support():
    configuration = (ROOT / "platform/live-lab/terraform/main.tf").read_text()
    assert 'engine_version    = "8.4"' in configuration
    assert configuration.count('engine_lifecycle_support = "open-source-rds-extended-support-disabled"') == 2


def test_dev_and_main_are_checked_without_publishing_from_dev():
    for filename in ("ci.yaml", "security.yaml", "codeql.yaml"):
        workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
        for event in ("push", "pull_request"):
            assert workflow["on"][event]["branches"] == ["dev", "main"]
    cd = yaml.load((ROOT / ".github/workflows/cd.yaml").read_text(), Loader=yaml.BaseLoader)
    assert cd["on"]["push"]["branches"] == ["main"]
    # A manual dispatch may select another branch; the publishing job must
    # reject it before credentials, build, or registry writes are available.
    assert cd["jobs"]["publish"]["if"] == "github.ref == 'refs/heads/main'"
    # Terraform's separate cloud-mutating trigger must not be widened to dev.
    infra = yaml.load((ROOT / ".github/workflows/infra.yaml").read_text(), Loader=yaml.BaseLoader)
    assert infra["on"]["push"]["branches"] == ["main"]


def run_script(name, *args):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / name), *map(str, args)],
        text=True, capture_output=True, timeout=20,
    )


def test_developer_can_read_only_the_named_argo_application():
    result = subprocess.run(
        ["kubectl", "kustomize", str(ROOT / "platform/governance")],
        check=True, text=True, capture_output=True, timeout=20,
    )
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    role = next(doc for doc in docs if doc["kind"] == "Role"
                and doc["metadata"]["name"] == "validation-application-reader")
    assert role["metadata"]["namespace"] == "argocd"
    assert role["rules"] == [{
        "apiGroups": ["argoproj.io"], "resources": ["applications"],
        "resourceNames": ["data-pipeline-validation"], "verbs": ["get"],
    }]
    binding = next(doc for doc in docs if doc["kind"] == "RoleBinding"
                   and doc["metadata"]["name"] == "validation-application-reader")
    assert binding["metadata"]["namespace"] == "argocd"
    assert binding["subjects"] == [{"kind": "ServiceAccount",
                                    "name": "kyobo-developer-readonly",
                                    "namespace": "platform-validation"}]
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io",
                                  "kind": "Role", "name": role["metadata"]["name"]}


def test_cd_checks_complete_evidence_before_attestation_and_upload():
    workflow = yaml.safe_load((ROOT / ".github/workflows/cd.yaml").read_text())
    steps = workflow["jobs"]["publish"]["steps"]
    check_index, check = next((i, step) for i, step in enumerate(steps)
                             if "scripts/verify_release_bundle.py" in step.get("run", ""))
    evidence_index = next(i for i, step in enumerate(steps)
                          if "scripts/write_release_evidence.py" in step.get("run", ""))
    signing = [i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/attest@")]
    pre_sign_checks = [
        i for i, step in enumerate(steps)
        if "scripts/verify_release_bundle.py" in step.get("run", "")
        and "--verify-attestation" not in step["run"]
    ]
    post_sign_checks = [
        (i, step) for i, step in enumerate(steps)
        if "scripts/verify_release_bundle.py" in step.get("run", "")
        and "--verify-attestation" in step["run"]
    ]
    upload = next(i for i, step in enumerate(steps)
                  if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert evidence_index < min(pre_sign_checks) < min(signing) < max(signing) < upload
    assert "continue-on-error" not in check
    assert '--source-revision "${{ github.sha }}"' in check["run"]
    assert "--current-manifest k8s/overlays/prod/kustomization.yaml" in check["run"]
    assert "--verify-attestation" not in check["run"]  # This is the pre-signing consistency gate.
    validation_precheck = next(steps[i] for i in pre_sign_checks
                               if "--candidate-target validation" in steps[i]["run"])
    assert "k8s/overlays/validation/kustomization.yaml" in validation_precheck["run"]
    assert len(post_sign_checks) == 2
    assert all("--verify-attestation" in step["run"] for _, step in post_sign_checks)
    assert all("--verify-image-attestation" in step["run"] for _, step in post_sign_checks)
    assert all(i > max(signing) and i < upload for i, _ in post_sign_checks)
    assert "> release/production-verification.json" in post_sign_checks[0][1]["run"]
    assert "> release/validation-verification.json" in post_sign_checks[1][1]["run"]
    assert any("validation-release-evidence.json" in step.get("with", {}).get("subject-path", "")
               for step in steps if step.get("uses", "").startswith("actions/attest@"))
    sbom_attestation = next(step for step in steps if step.get("name") == "Attest image SBOM")
    assert sbom_attestation["with"]["push-to-registry"] is True


def test_real_generator_and_verifier_cli_chain_rejects_tampering(tmp_path):
    manifest = tmp_path / "kustomization.yaml"
    original = """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
images:
  - name: data-pipeline-app
    newName: registry.example.test/old
    newTag: old
"""
    manifest.write_text(original)
    rendered = tmp_path / "rendered.yaml"
    rendered.write_text(f"kind: Pod\nspec:\n  containers:\n  - image: {IMAGE}:{SHA}@{DIGEST}\n")
    candidate_dir = tmp_path / "candidate"
    result = run_script("prepare_release_candidate.py",
                        "--source-revision", SHA, "--image-name", IMAGE,
                        "--image-digest", DIGEST, "--manifest-path", manifest,
                        "--rendered-manifest-path", rendered, "--output-dir", candidate_dir)
    assert result.returncode == 0, result.stderr
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps({"spdxVersion": "SPDX-2.3", "packages": []}))
    evidence = tmp_path / "release-evidence.json"
    result = run_script("write_release_evidence.py",
                        "--source-revision", SHA, "--image-name", IMAGE,
                        "--image-digest", DIGEST, "--sbom-path", sbom,
                        "--manifest-path", manifest,
                        "--candidate-metadata-path", candidate_dir / "release-candidate.json",
                        "--publication-mode", "scanned-before-push", "--output", evidence)
    assert result.returncode == 0, result.stderr
    args = ("--candidate-dir", candidate_dir, "--release-evidence", evidence,
            "--sbom", sbom, "--current-manifest", manifest,
            "--source-revision", SHA, "--image-name", IMAGE)
    result = run_script("verify_release_bundle.py", *args)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["scope"] == "local_consistency"
    assert manifest.read_text() == original
    sbom.write_text("tampered")
    rejected = run_script("verify_release_bundle.py", *args)
    assert rejected.returncode != 0
    assert manifest.read_text() == original


def test_validation_candidate_cli_chain_is_image_only_and_rejects_stale_source(tmp_path):
    manifest = tmp_path / "validation-kustomization.yaml"
    original = """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
images:
  - name: data-pipeline-app
    newName: registry.example.test/old
    newTag: old
"""
    manifest.write_text(original, encoding="utf-8")
    rendered = tmp_path / "rendered-validation.yaml"
    rendered.write_text(
        "kind: Deployment\nspec:\n  template:\n    spec:\n      containers:\n"
        + "".join(f"      - image: {IMAGE}:{SHA}@{DIGEST}\n" for _ in range(3)),
        encoding="utf-8",
    )
    candidate_dir = tmp_path / "validation-candidate"
    result = run_script(
        "prepare_release_candidate.py",
        "--candidate-target", "validation",
        "--source-revision", SHA,
        "--image-name", IMAGE,
        "--image-digest", DIGEST,
        "--manifest-path", manifest,
        "--rendered-manifest-path", rendered,
        "--output-dir", candidate_dir,
    )
    assert result.returncode == 0, result.stderr
    candidate_manifest = candidate_dir / "validation-kustomization.yaml"
    metadata = json.loads((candidate_dir / "release-candidate.json").read_text())
    assert metadata["target"] == "validation"
    assert metadata["gitops_candidate"]["rendered_image_count"] == 3
    candidate_text = candidate_manifest.read_text()
    assert "  - ../../base\n" in candidate_text
    assert f"newName: {IMAGE}" in candidate_text
    assert f"newTag: {SHA}" in candidate_text
    assert manifest.read_text() == original

    sbom = tmp_path / "validation-sbom.json"
    sbom.write_text(json.dumps({"spdxVersion": "SPDX-2.3", "packages": []}))
    evidence = tmp_path / "validation-release-evidence.json"
    result = run_script(
        "write_release_evidence.py",
        "--source-revision", SHA,
        "--image-name", IMAGE,
        "--image-digest", DIGEST,
        "--sbom-path", sbom,
        "--manifest-path", manifest,
        "--candidate-metadata-path", candidate_dir / "release-candidate.json",
        "--publication-mode", "scanned-before-push",
        "--output", evidence,
    )
    assert result.returncode == 0, result.stderr
    args = (
        "--candidate-target", "validation",
        "--candidate-dir", candidate_dir,
        "--release-evidence", evidence,
        "--sbom", sbom,
        "--current-manifest", manifest,
        "--source-revision", SHA,
        "--image-name", IMAGE,
    )
    verified = run_script("verify_release_bundle.py", *args)
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["candidate_target"] == "validation"

    manifest.write_text(original + "commonLabels:\n  changed: 'true'\n", encoding="utf-8")
    stale = run_script("verify_release_bundle.py", *args)
    assert stale.returncode != 0
    assert "current manifest differs" in stale.stderr
