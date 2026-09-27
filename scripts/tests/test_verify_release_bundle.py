import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import prepare_release_candidate as candidate_builder
from scripts import verify_release_bundle as verifier
from scripts import write_release_evidence as evidence_writer


SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
IMAGE_DIGEST = "sha256:" + "a" * 64
IMAGE_NAME = "123456789012.dkr.ecr.eu-west-1.amazonaws.com/data-pipeline-app"
IMAGE_REFERENCE = f"{IMAGE_NAME}:{SOURCE_SHA}@{IMAGE_DIGEST}"
SCRIPT_PATH = Path(verifier.__file__).resolve()


def _generator_args(**values):
    return argparse.Namespace(**values)


def _write_evidence(bundle, monkeypatch):
    monkeypatch.setattr(
        evidence_writer,
        "parse_args",
        lambda: _generator_args(
            source_revision=SOURCE_SHA,
            gitops_revision=None,
            image_name=IMAGE_NAME,
            image_digest=IMAGE_DIGEST,
            sbom_path=bundle["sbom"],
            manifest_path=bundle["current_manifest"],
            candidate_metadata_path=bundle["candidate_dir"] / "release-candidate.json",
            publication_mode="scanned-before-push",
            output=bundle["release_evidence"],
        ),
    )
    evidence_writer.main()


def _regenerate_evidence(bundle, monkeypatch):
    _write_evidence(bundle, monkeypatch)


@pytest.fixture
def generated_bundle(tmp_path, monkeypatch):
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    current_manifest = tmp_path / "prod-kustomization.yaml"
    current_manifest.write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources:\n"
        "  - ../../base\n"
        "images:\n"
        "  - name: data-pipeline-app\n"
        "    newName: old.example.com/data-pipeline-app\n"
        "    newTag: old-tag\n",
        encoding="utf-8",
    )
    sbom = tmp_path / "data-pipeline-app.sbom.spdx.json"
    sbom.write_bytes(b'{"spdxVersion":"SPDX-2.3","packages":[]}\n')
    candidate_manifest = candidate_dir / "prod-kustomization.yaml"
    candidate_metadata = candidate_dir / "release-candidate.json"
    rendered = tmp_path / "rendered-production.yaml"
    release_evidence = tmp_path / "release-evidence.json"

    def run_candidate_generator(rendered_manifest_path):
        monkeypatch.setattr(
            candidate_builder,
            "parse_args",
            lambda: _generator_args(
                source_revision=SOURCE_SHA,
                image_name=IMAGE_NAME,
                image_digest=IMAGE_DIGEST,
                manifest_path=current_manifest,
                rendered_manifest_path=rendered_manifest_path,
                output_dir=candidate_dir,
            ),
        )
        candidate_builder.main()

    run_candidate_generator(None)
    rendered.write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        f"        - image: {IMAGE_REFERENCE}\n",
        encoding="utf-8",
    )
    run_candidate_generator(rendered)

    bundle = {
        "candidate_dir": candidate_dir,
        "candidate_manifest": candidate_manifest,
        "candidate_metadata": candidate_metadata,
        "current_manifest": current_manifest,
        "release_evidence": release_evidence,
        "sbom": sbom,
        "source_revision": SOURCE_SHA,
        "image_name": IMAGE_NAME,
    }
    _write_evidence(bundle, monkeypatch)
    return bundle


@pytest.fixture
def validation_bundle(tmp_path, monkeypatch):
    candidate_dir = tmp_path / "validation-candidate"
    candidate_dir.mkdir()
    current_manifest = tmp_path / "validation-kustomization.yaml"
    current_manifest.write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources:\n"
        "  - ../../base\n"
        "images:\n"
        "  - name: data-pipeline-app\n"
        "    newName: old.example.com/data-pipeline-app\n"
        "    newTag: old-tag\n",
        encoding="utf-8",
    )
    sbom = tmp_path / "validation-data-pipeline-app.sbom.spdx.json"
    sbom.write_bytes(b'{"spdxVersion":"SPDX-2.3","packages":[]}\n')
    candidate_manifest = candidate_dir / "validation-kustomization.yaml"
    candidate_metadata = candidate_dir / "release-candidate.json"
    rendered = tmp_path / "rendered-validation.yaml"
    release_evidence = tmp_path / "validation-release-evidence.json"

    def run_candidate_generator(rendered_manifest_path):
        monkeypatch.setattr(
            candidate_builder,
            "parse_args",
            lambda: _generator_args(
                source_revision=SOURCE_SHA,
                image_name=IMAGE_NAME,
                image_digest=IMAGE_DIGEST,
                manifest_path=current_manifest,
                rendered_manifest_path=rendered_manifest_path,
                output_dir=candidate_dir,
                candidate_target="validation",
            ),
        )
        candidate_builder.main()

    run_candidate_generator(None)
    rendered.write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        f"        - image: {IMAGE_REFERENCE}\n",
        encoding="utf-8",
    )
    run_candidate_generator(rendered)

    bundle = {
        "candidate_dir": candidate_dir,
        "candidate_manifest": candidate_manifest,
        "candidate_metadata": candidate_metadata,
        "current_manifest": current_manifest,
        "release_evidence": release_evidence,
        "sbom": sbom,
        "source_revision": SOURCE_SHA,
        "image_name": IMAGE_NAME,
        "candidate_target": "validation",
    }
    _write_evidence(bundle, monkeypatch)
    return bundle


def _validate(bundle, **overrides):
    values = {
        "candidate_dir": bundle["candidate_dir"],
        "release_evidence": bundle["release_evidence"],
        "sbom": bundle["sbom"],
        "current_manifest": bundle["current_manifest"],
        "source_revision": bundle["source_revision"],
        "image_name": bundle["image_name"],
        "candidate_target": bundle.get("candidate_target", "prod"),
    }
    values.update(overrides)
    return verifier.validate_bundle(**values)


def _edit_json(path, update):
    payload = json.loads(path.read_text(encoding="utf-8"))
    update(payload)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def test_real_generator_bundle_is_consistent(generated_bundle):
    summary = _validate(generated_bundle)

    assert summary["scope"] == "local_consistency"
    assert summary["attestation"] == "not_verified"
    assert summary["source_revision"] == SOURCE_SHA
    assert summary["image_name"] == IMAGE_NAME
    assert summary["image_digest"] == IMAGE_DIGEST
    assert summary["candidate_manifest_sha256"] == hashlib.sha256(
        generated_bundle["candidate_manifest"].read_bytes()
    ).hexdigest()
    assert summary["approved"] is False
    assert summary["deployed"] is False


def test_validation_target_candidate_bundle_is_consistent_and_source_bound(validation_bundle):
    summary = _validate(validation_bundle)
    metadata = json.loads(validation_bundle["candidate_metadata"].read_text(encoding="utf-8"))

    assert metadata["target"] == "validation"
    assert validation_bundle["candidate_manifest"].is_file()
    assert summary["candidate_target"] == "validation"
    assert summary["verification"] == {
        "local_bundle_hashes": "verified",
        "release_evidence_provenance": "not_verified",
        "oci_image_sbom_subject": "not_verified",
    }


def test_validation_target_rejects_stale_source_manifest(validation_bundle):
    with validation_bundle["current_manifest"].open("ab") as target:
        target.write(b"commonLabels:\n  changed: 'true'\n")

    with pytest.raises(ValueError, match="current manifest differs"):
        _validate(validation_bundle)


@pytest.mark.parametrize("bad_version", [True, 1.0, "1", 2])
def test_candidate_schema_version_requires_exact_integer_one(generated_bundle, bad_version):
    _edit_json(
        generated_bundle["candidate_metadata"],
        lambda payload: payload.update(schema_version=bad_version),
    )

    with pytest.raises(ValueError, match="schema_version"):
        _validate(generated_bundle)


def test_evidence_schema_version_requires_exact_integer_one(generated_bundle):
    _edit_json(
        generated_bundle["release_evidence"],
        lambda payload: payload.update(schema_version=True),
    )

    with pytest.raises(ValueError, match="schema_version"):
        _validate(generated_bundle)


def test_wrong_expected_source_sha_is_rejected(generated_bundle):
    with pytest.raises(ValueError, match="source revision"):
        _validate(generated_bundle, source_revision="f" * 40)


@pytest.mark.parametrize("source_sha", ["a" * 39, "g" * 40, "A" * 40])
def test_expected_source_sha_must_be_full_lowercase_git_sha(generated_bundle, source_sha):
    with pytest.raises(ValueError, match="full 40-character"):
        _validate(generated_bundle, source_revision=source_sha)


@pytest.mark.parametrize("target", ["candidate", "evidence"])
def test_bundle_source_revision_mismatch_is_rejected(generated_bundle, target):
    path = generated_bundle["candidate_metadata"] if target == "candidate" else generated_bundle[
        "release_evidence"
    ]

    def update(payload):
        payload["source"]["revision"] = "f" * 40

    _edit_json(path, update)

    with pytest.raises(ValueError, match="source revision"):
        _validate(generated_bundle)


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("candidate", "tag", "latest"),
        ("candidate", "reference", "example.com/other:tag@sha256:" + "b" * 64),
        ("candidate", "digest", "sha256:" + "b" * 64),
    ],
)
def test_candidate_image_tag_digest_and_reference_must_match(
    generated_bundle, target, field, value
):
    path = generated_bundle["candidate_metadata"] if target == "candidate" else generated_bundle[
        "release_evidence"
    ]
    _edit_json(path, lambda payload: payload["image"].update({field: value}))

    with pytest.raises(ValueError, match="candidate image"):
        _validate(generated_bundle)


def test_evidence_image_name_must_match_expected_image(generated_bundle):
    _edit_json(
        generated_bundle["release_evidence"],
        lambda payload: payload["image"].update(name="example.com/other"),
    )

    with pytest.raises(ValueError, match="image name"):
        _validate(generated_bundle)


def test_candidate_image_name_must_match_expected_image(generated_bundle):
    _edit_json(
        generated_bundle["candidate_metadata"],
        lambda payload: payload["image"].update(name="example.com/other"),
    )

    with pytest.raises(ValueError, match="candidate image"):
        _validate(generated_bundle)


def test_evidence_digest_must_match_candidate_digest(generated_bundle):
    _edit_json(
        generated_bundle["release_evidence"],
        lambda payload: payload["image"].update(digest="sha256:" + "b" * 64),
    )

    with pytest.raises(ValueError, match="candidate image"):
        _validate(generated_bundle)


def test_unrecognized_publication_mode_is_rejected(generated_bundle):
    _edit_json(
        generated_bundle["release_evidence"],
        lambda payload: payload["image"].update(publication_mode="manual-push"),
    )

    with pytest.raises(ValueError, match="publication mode"):
        _validate(generated_bundle)


def test_existing_immutable_tag_publication_mode_is_allowed(generated_bundle):
    _edit_json(
        generated_bundle["release_evidence"],
        lambda payload: payload["image"].update(publication_mode="reused-existing-tag"),
    )

    assert _validate(generated_bundle)["scope"] == "local_consistency"


@pytest.mark.parametrize("bad_mode", [[], {}])
def test_malformed_publication_mode_fails_cli_without_traceback(generated_bundle, bad_mode):
    _edit_json(
        generated_bundle["release_evidence"],
        lambda payload: payload["image"].update(publication_mode=bad_mode),
    )

    result = subprocess.run(
        _cli_command(generated_bundle), capture_output=True, text=True, check=False
    )

    assert result.returncode == 1
    assert "publication_mode" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("count", [0, -1, True, 1.0, "1", None])
def test_rendered_image_count_must_be_positive_non_boolean_integer(generated_bundle, count):
    _edit_json(
        generated_bundle["candidate_metadata"],
        lambda payload: payload["gitops_candidate"].update(rendered_image_count=count),
    )

    with pytest.raises(ValueError, match="rendered_image_count"):
        _validate(generated_bundle)


def test_release_review_flags_are_enforced(generated_bundle):
    _edit_json(
        generated_bundle["candidate_metadata"],
        lambda payload: payload["gitops_candidate"].update(direct_main_push=True),
    )

    with pytest.raises(ValueError, match="direct_main_push"):
        _validate(generated_bundle)


def test_operator_review_pr_is_required(generated_bundle):
    _edit_json(
        generated_bundle["candidate_metadata"],
        lambda payload: payload["gitops_candidate"].update(requires_operator_review_pr=False),
    )

    with pytest.raises(ValueError, match="requires_operator_review_pr"):
        _validate(generated_bundle)


def test_evidence_direct_main_push_must_be_false(generated_bundle):
    _edit_json(
        generated_bundle["release_evidence"],
        lambda payload: payload["gitops"].update(direct_main_push=True),
    )

    with pytest.raises(ValueError, match="direct_main_push"):
        _validate(generated_bundle)


def test_changed_sbom_bytes_are_rejected(generated_bundle):
    generated_bundle["sbom"].write_bytes(b'{"packages":[]}\n')

    with pytest.raises(ValueError, match="SBOM bytes"):
        _validate(generated_bundle)


def test_stale_candidate_is_rejected_when_current_manifest_changes(generated_bundle):
    with generated_bundle["current_manifest"].open("ab") as target:
        target.write(b"commonLabels:\n  changed: 'true'\n")

    with pytest.raises(ValueError, match="current manifest differs"):
        _validate(generated_bundle)


def test_candidate_manifest_tampering_is_rejected(generated_bundle):
    with generated_bundle["candidate_manifest"].open("ab") as target:
        target.write(b"commonLabels:\n  tampered: 'true'\n")

    with pytest.raises(ValueError, match="candidate manifest bytes"):
        _validate(generated_bundle)


def test_rehashed_extra_manifest_change_is_rejected(generated_bundle, monkeypatch):
    manifest = generated_bundle["candidate_manifest"]
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "  - ../../base\n", "  - ../../unreviewed-resource\n"
        ),
        encoding="utf-8",
    )
    _edit_json(
        generated_bundle["candidate_metadata"],
        lambda payload: payload["gitops_candidate"].update(
            candidate_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest()
        ),
    )
    _regenerate_evidence(generated_bundle, monkeypatch)

    with pytest.raises(ValueError, match="beyond the expected image transformation"):
        _validate(generated_bundle)


@pytest.mark.parametrize("target", ["candidate", "evidence"])
def test_duplicate_json_keys_are_rejected(generated_bundle, target):
    path = generated_bundle["candidate_metadata"] if target == "candidate" else generated_bundle[
        "release_evidence"
    ]
    path.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate object key"):
        _validate(generated_bundle)


def test_malformed_nested_shape_is_rejected(generated_bundle):
    _edit_json(
        generated_bundle["candidate_metadata"],
        lambda payload: payload.update(image=[]),
    )

    with pytest.raises(ValueError, match="candidate image"):
        _validate(generated_bundle)


def test_candidate_metadata_bytes_must_be_bound_by_evidence(generated_bundle):
    _edit_json(
        generated_bundle["candidate_metadata"],
        lambda payload: payload["gitops_candidate"].update(
            candidate_manifest_path="/unbound/metadata-only/path.yaml"
        ),
    )

    with pytest.raises(ValueError, match="candidate metadata bytes"):
        _validate(generated_bundle)


def test_json_paths_are_not_opened(generated_bundle, monkeypatch):
    _edit_json(
        generated_bundle["candidate_metadata"],
        lambda payload: payload["gitops_candidate"].update(
            source_manifest_path="/never/open/source.yaml",
            candidate_manifest_path="/never/open/candidate.yaml",
        ),
    )
    _regenerate_evidence(generated_bundle, monkeypatch)
    _edit_json(
        generated_bundle["release_evidence"],
        lambda payload: payload["gitops"].update(
            manifest_path="/never/open/manifest.yaml",
            candidate_metadata_path="/never/open/metadata.json",
        ),
    )
    _edit_json(
        generated_bundle["release_evidence"],
        lambda payload: payload["supply_chain"].update(sbom_path="/never/open/sbom.json"),
    )

    assert _validate(generated_bundle)["scope"] == "local_consistency"


def _verified_gh_output():
    return json.dumps(
        [
            {
                "attestation": {"bundle": {"mediaType": "application/vnd.dev.sigstore.bundle"}},
                "verificationResult": {
                    "signature": {"certificate": {"subjectAlternativeName": "workflow"}},
                    "verifiedTimestamps": [],
                    "statement": {"predicateType": "https://slsa.dev/provenance/v1"},
                },
            }
        ]
    ).encode("utf-8")


def test_attestation_uses_fixed_gh_identity_and_expected_source(
    generated_bundle, monkeypatch
):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=_verified_gh_output(), stderr=b"ignored")

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)

    assert verifier.verify_attestation(generated_bundle["release_evidence"], SOURCE_SHA) == 1
    command = captured["command"]
    assert command[:3] == ["gh", "attestation", "verify"]
    assert command[3] == str(generated_bundle["release_evidence"])
    assert command[command.index("--repo") + 1] == "masondev1024/aws-data-platform-gitops"
    assert command[command.index("--signer-workflow") + 1] == (
        "masondev1024/aws-data-platform-gitops/.github/workflows/cd.yaml"
    )
    assert command[command.index("--source-digest") + 1] == SOURCE_SHA
    assert command[command.index("--source-ref") + 1] == "refs/heads/main"
    assert "--deny-self-hosted-runners" in command
    assert command[command.index("--format") + 1] == "json"
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == verifier.ATTESTATION_TIMEOUT_SECONDS


def _verified_image_gh_output(*, image_name=IMAGE_NAME, digest=IMAGE_DIGEST, predicate=None):
    return json.dumps(
        [
            {
                "attestation": {"bundle": {"mediaType": "application/vnd.dev.sigstore.bundle"}},
                "verificationResult": {
                    "signature": {"certificate": {"subjectAlternativeName": "workflow"}},
                    "verifiedTimestamps": [],
                    "statement": {
                        "predicateType": predicate or verifier.EXPECTED_SPDX_PREDICATE,
                        "subject": [
                            {
                                "name": image_name,
                                "digest": {"sha256": digest.removeprefix("sha256:")},
                            }
                        ],
                    },
                },
            }
        ]
    ).encode("utf-8")


def test_oci_sbom_verification_uses_exact_digest_source_and_hosted_identity(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=_verified_image_gh_output(),
            stderr=b"ignored",
        )

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)

    assert verifier.verify_image_attestation(IMAGE_NAME, IMAGE_DIGEST, SOURCE_SHA) == 1
    command = captured["command"]
    assert command == [
        "gh",
        "attestation",
        "verify",
        f"oci://{IMAGE_NAME}@{IMAGE_DIGEST}",
        "--repo",
        verifier.EXPECTED_REPOSITORY,
        "--bundle-from-oci",
        "--signer-workflow",
        verifier.EXPECTED_SIGNER_WORKFLOW,
        "--source-digest",
        SOURCE_SHA,
        "--source-ref",
        "refs/heads/main",
        "--deny-self-hosted-runners",
        "--predicate-type",
        "https://spdx.dev/Document/v2.3",
        "--format",
        "json",
    ]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == verifier.ATTESTATION_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("predicate", "image_name", "digest", "message"),
    [
        ("https://slsa.dev/provenance/v1", IMAGE_NAME, IMAGE_DIGEST, "SPDX predicate"),
        (verifier.EXPECTED_SPDX_PREDICATE, "registry.example.test/other", IMAGE_DIGEST, "subject"),
        (verifier.EXPECTED_SPDX_PREDICATE, IMAGE_NAME, "sha256:" + "b" * 64, "subject"),
    ],
)
def test_oci_sbom_verification_rejects_wrong_predicate_or_subject(
    predicate, image_name, digest, message
):
    output = _verified_image_gh_output(
        image_name=image_name, digest=digest, predicate=predicate
    )

    with pytest.raises(ValueError, match=message):
        verifier._validate_spdx_image_attestation_result(output, IMAGE_NAME, IMAGE_DIGEST)


def test_oci_attestation_command_failure_does_not_leak_registry_auth_error(monkeypatch):
    secret_error = b"private-registry-token-and-response"
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=b"", stderr=secret_error),
    )

    with pytest.raises(ValueError, match="OCI image attestation verification failed") as error:
        verifier.verify_image_attestation(IMAGE_NAME, IMAGE_DIGEST, SOURCE_SHA)
    assert secret_error.decode() not in str(error.value)


def test_attestation_cli_reports_release_evidence_provenance_only(
    generated_bundle, monkeypatch, capsys
):
    monkeypatch.setattr(verifier, "verify_attestation", lambda *_args: 2)

    exit_code = verifier.main(_cli_command(generated_bundle)[2:] + ["--verify-attestation"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["scope"] == "local_consistency+release_evidence_provenance"
    assert output["attestation"] == "verified"
    assert output["attestation_target"] == "release_evidence"
    assert output["oci_image_subject_verified"] is False
    assert output["gitops_pr_approved"] is False
    assert output["approved"] is False
    assert output["deployed"] is False


def test_image_attestation_cli_reports_image_subject_without_claiming_release_provenance(
    generated_bundle, monkeypatch, capsys
):
    monkeypatch.setattr(verifier, "verify_image_attestation", lambda *_args: 1)

    exit_code = verifier.main(
        _cli_command(generated_bundle)[2:] + ["--verify-image-attestation"]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["scope"] == "local_consistency+oci_image_sbom_subject"
    assert output["verification"] == {
        "local_bundle_hashes": "verified",
        "release_evidence_provenance": "not_verified",
        "oci_image_sbom_subject": "verified",
    }
    assert output["attestation"] == "not_verified"
    assert output["oci_image_subject_verified"] is True


@pytest.mark.parametrize(
    "stdout",
    [
        b"[]",
        b"{}",
        b"not-json",
        b"[{}]",
        b"[{\"attestation\":{},\"verificationResult\":{}}]",
        None,
        "[]",
    ],
)
def test_empty_or_malformed_attestation_results_fail_closed(
    generated_bundle, monkeypatch, stdout
):
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=stdout, stderr=b""),
    )

    with pytest.raises(ValueError, match="attestation verifier"):
        verifier.verify_attestation(generated_bundle["release_evidence"], SOURCE_SHA)


def test_nonzero_attestation_error_does_not_leak_stderr(generated_bundle, monkeypatch):
    secret_error = b"private-token-and-server-response"
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=b"[]", stderr=secret_error),
    )

    with pytest.raises(ValueError, match="verification failed") as error:
        verifier.verify_attestation(generated_bundle["release_evidence"], SOURCE_SHA)
    assert secret_error.decode() not in str(error.value)


def test_attestation_timeout_fails_closed_without_stderr_leak(generated_bundle, monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("gh", verifier.ATTESTATION_TIMEOUT_SECONDS, stderr=b"secret")

    monkeypatch.setattr(verifier.subprocess, "run", timeout)

    with pytest.raises(ValueError, match="timed out") as error:
        verifier.verify_attestation(generated_bundle["release_evidence"], SOURCE_SHA)
    assert "secret" not in str(error.value)


def _cli_command(bundle):
    return [
        sys.executable,
        str(SCRIPT_PATH),
        "--candidate-dir",
        str(bundle["candidate_dir"]),
        "--release-evidence",
        str(bundle["release_evidence"]),
        "--sbom",
        str(bundle["sbom"]),
        "--current-manifest",
        str(bundle["current_manifest"]),
        "--source-revision",
        SOURCE_SHA,
        "--image-name",
        IMAGE_NAME,
    ]


def test_cli_success_emits_local_scope_and_failure_has_nonzero_exit(generated_bundle):
    success = subprocess.run(_cli_command(generated_bundle), capture_output=True, text=True, check=False)

    assert success.returncode == 0
    output = json.loads(success.stdout)
    assert output["scope"] == "local_consistency"
    assert output["attestation"] == "not_verified"
    assert output["approved"] is False
    assert output["deployed"] is False

    generated_bundle["sbom"].write_bytes(b"tampered")
    failure = subprocess.run(_cli_command(generated_bundle), capture_output=True, text=True, check=False)

    assert failure.returncode != 0
    assert "SBOM bytes" in failure.stderr


def test_attestation_cli_revalidates_bundle_and_detects_evidence_replacement(
    generated_bundle, monkeypatch, capsys
):
    def replace_evidence_during_verification(path, _source_revision):
        path.write_bytes(path.read_bytes() + b" ")
        return 1

    monkeypatch.setattr(verifier, "verify_attestation", replace_evidence_during_verification)

    exit_code = verifier.main(_cli_command(generated_bundle)[2:] + ["--verify-attestation"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "release evidence changed during attestation verification" in captured.err
    assert captured.out == ""
