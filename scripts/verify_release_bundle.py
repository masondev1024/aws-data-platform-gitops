#!/usr/bin/env python3
"""Verify local release-artifact consistency and, optionally, GitHub attestation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


if __package__:
    from . import prepare_release_candidate as candidate_builder
else:
    import prepare_release_candidate as candidate_builder


RELEASE_CANDIDATE_NAME = "release-candidate.json"
CANDIDATE_MANIFEST_NAMES = {
    "prod": "prod-kustomization.yaml",
    "validation": "validation-kustomization.yaml",
}
ATTESTATION_TIMEOUT_SECONDS = 45
EXPECTED_PUBLICATION_MODES = frozenset({"reused-existing-tag", "scanned-before-push"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_REPOSITORY = "masondev1024/aws-data-platform-gitops"
EXPECTED_SIGNER_WORKFLOW = (
    "masondev1024/aws-data-platform-gitops/.github/workflows/cd.yaml"
)
EXPECTED_SPDX_PREDICATE = "https://spdx.dev/Document/v2.3"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON contains a duplicate object key")
        result[key] = value
    return result


def _reject_nonstandard_constant(_value: str) -> None:
    raise ValueError("JSON contains a nonstandard numeric constant")


def _parse_json(raw: bytes, label: str) -> Any:
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc


def _require_object(
    value: Any,
    label: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    allowed = required | (optional or set())
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise ValueError(f"{label} has an unexpected shape")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_sha256(value: Any, label: str) -> str:
    digest = _require_string(value, label)
    if not SHA256_PATTERN.fullmatch(digest):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
    return digest


def _require_schema_version(value: Any, label: str) -> None:
    if type(value) is not int or value != 1:
        raise ValueError(f"{label} schema_version must be integer 1")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_required(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} could not be read") from exc


def validate_bundle(
    *,
    candidate_dir: Path,
    release_evidence: Path,
    sbom: Path,
    current_manifest: Path,
    source_revision: str,
    image_name: str,
    candidate_target: str = "prod",
) -> dict[str, Any]:
    """Validate the explicitly supplied artifacts without following JSON paths."""
    _require_string(source_revision, "expected source revision")
    _require_string(image_name, "expected image name")
    candidate_builder.validate_source_revision(source_revision)
    candidate_builder.validate_image_name(image_name)
    if candidate_target not in CANDIDATE_MANIFEST_NAMES:
        raise ValueError("candidate target must be prod or validation")

    if not candidate_dir.is_dir():
        raise ValueError("candidate directory is missing")

    candidate_metadata_bytes = _read_required(
        candidate_dir / RELEASE_CANDIDATE_NAME, "release candidate metadata"
    )
    candidate_manifest_bytes = _read_required(
        candidate_dir / CANDIDATE_MANIFEST_NAMES[candidate_target], "candidate manifest"
    )
    evidence_bytes = _read_required(release_evidence, "release evidence")
    sbom_bytes = _read_required(sbom, "SBOM")
    current_manifest_bytes = _read_required(current_manifest, "current manifest")

    metadata = _require_object(
        _parse_json(candidate_metadata_bytes, "release candidate metadata"),
        "release candidate metadata",
        required={"schema_version", "source", "image", "gitops_candidate"},
        optional={"target"},
    )
    evidence = _require_object(
        _parse_json(evidence_bytes, "release evidence"),
        "release evidence",
        required={"schema_version", "generated_at", "source", "gitops", "image", "supply_chain"},
    )
    _require_schema_version(metadata["schema_version"], "release candidate")
    _require_schema_version(evidence["schema_version"], "release evidence")
    _require_string(evidence["generated_at"], "release evidence generated_at")
    if metadata.get("target", "prod") != candidate_target:
        raise ValueError("candidate target does not match the requested target")

    candidate_source = _require_object(
        metadata["source"], "candidate source", required={"revision"}
    )
    candidate_image = _require_object(
        metadata["image"],
        "candidate image",
        required={"name", "digest", "tag", "reference"},
    )
    candidate_gitops = _require_object(
        metadata["gitops_candidate"],
        "candidate GitOps metadata",
        required={
            "source_manifest_path",
            "candidate_manifest_path",
            "source_manifest_sha256",
            "candidate_manifest_sha256",
            "rendered_image_count",
            "direct_main_push",
            "requires_operator_review_pr",
        },
    )

    evidence_source = _require_object(
        evidence["source"], "evidence source", required={"revision"}
    )
    evidence_gitops = _require_object(
        evidence["gitops"],
        "evidence GitOps metadata",
        required={
            "manifest_path",
            "manifest_sha256",
            "direct_main_push",
            "candidate_metadata_path",
            "candidate_metadata_sha256",
        },
        optional={"revision"},
    )
    if "revision" in evidence_gitops:
        revision = _require_string(evidence_gitops["revision"], "evidence GitOps revision")
        if not re.fullmatch(r"[0-9a-f]{7,64}", revision):
            raise ValueError("evidence GitOps revision must be a hexadecimal Git revision")
    evidence_image = _require_object(
        evidence["image"],
        "evidence image",
        required={"name", "digest", "publication_mode"},
    )
    supply_chain = _require_object(
        evidence["supply_chain"],
        "evidence supply_chain",
        required={"sbom_path", "sbom_sha256", "vulnerability_policy"},
    )

    for label, path_value in (
        ("candidate source_manifest_path", candidate_gitops["source_manifest_path"]),
        ("candidate candidate_manifest_path", candidate_gitops["candidate_manifest_path"]),
        ("evidence manifest_path", evidence_gitops["manifest_path"]),
        ("evidence candidate_metadata_path", evidence_gitops["candidate_metadata_path"]),
        ("evidence sbom_path", supply_chain["sbom_path"]),
    ):
        _require_string(path_value, label)

    candidate_source_revision = _require_string(
        candidate_source["revision"], "candidate source revision"
    )
    evidence_source_revision = _require_string(
        evidence_source["revision"], "evidence source revision"
    )
    candidate_image_name = _require_string(candidate_image["name"], "candidate image name")
    evidence_image_name = _require_string(evidence_image["name"], "evidence image name")
    raw_image_digest = evidence_image["digest"]
    if not isinstance(raw_image_digest, str) or not raw_image_digest.startswith("sha256:"):
        raise ValueError("evidence image digest must have the sha256:<64 lowercase hex> form")
    image_digest_hex = _require_sha256(raw_image_digest[len("sha256:") :], "image digest")
    image_digest = f"sha256:{image_digest_hex}"
    candidate_builder.validate_image_digest(image_digest)
    expected_reference = f"{image_name}:{source_revision}@{image_digest}"

    if candidate_source_revision != source_revision or evidence_source_revision != source_revision:
        raise ValueError("source revision does not match the expected full source SHA")
    if candidate_image_name != image_name:
        raise ValueError("candidate image name does not match the expected image")
    if evidence_image_name != image_name:
        raise ValueError("evidence image name does not match the expected image")
    candidate_tag = _require_string(candidate_image["tag"], "candidate image tag")
    candidate_reference = _require_string(candidate_image["reference"], "candidate image reference")
    if (
        candidate_image["digest"] != image_digest
        or candidate_tag != source_revision
        or candidate_reference != expected_reference
    ):
        raise ValueError("candidate image digest, tag, or reference is inconsistent")
    publication_mode = _require_string(
        evidence_image["publication_mode"], "evidence publication_mode"
    )
    if publication_mode not in EXPECTED_PUBLICATION_MODES:
        raise ValueError("release evidence publication mode is not allowed")

    rendered_image_count = candidate_gitops["rendered_image_count"]
    if type(rendered_image_count) is not int or rendered_image_count <= 0:
        raise ValueError("candidate rendered_image_count must be a positive integer")
    if candidate_gitops["direct_main_push"] is not False:
        raise ValueError("candidate direct_main_push must be false")
    if candidate_gitops["requires_operator_review_pr"] is not True:
        raise ValueError("candidate requires_operator_review_pr must be true")
    if evidence_gitops["direct_main_push"] is not False:
        raise ValueError("evidence direct_main_push must be false")

    if not isinstance(supply_chain["vulnerability_policy"], str) or not supply_chain[
        "vulnerability_policy"
    ]:
        raise ValueError("evidence vulnerability_policy must be a non-empty string")

    current_manifest_sha256 = _sha256_bytes(current_manifest_bytes)
    evidence_manifest_sha256 = _require_sha256(
        evidence_gitops["manifest_sha256"], "evidence manifest_sha256"
    )
    if evidence_manifest_sha256 != current_manifest_sha256:
        raise ValueError("current manifest differs from the manifest bound by release evidence")

    candidate_metadata_sha256 = _sha256_bytes(candidate_metadata_bytes)
    if (
        _require_sha256(
            evidence_gitops["candidate_metadata_sha256"],
            "evidence candidate_metadata_sha256",
        )
        != candidate_metadata_sha256
    ):
        raise ValueError("candidate metadata bytes do not match release evidence")

    if (
        _require_sha256(supply_chain["sbom_sha256"], "evidence sbom_sha256")
        != _sha256_bytes(sbom_bytes)
    ):
        raise ValueError("SBOM bytes do not match release evidence")

    candidate_manifest_sha256 = _sha256_bytes(candidate_manifest_bytes)
    if (
        _require_sha256(
            candidate_gitops["candidate_manifest_sha256"],
            "candidate candidate_manifest_sha256",
        )
        != candidate_manifest_sha256
    ):
        raise ValueError("candidate manifest bytes do not match candidate metadata")

    try:
        current_manifest_text = current_manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("current manifest must be UTF-8 YAML") from exc
    # Path.read_text() in the candidate generator applies universal newline handling.
    current_manifest_text = current_manifest_text.replace("\r\n", "\n").replace("\r", "\n")
    source_manifest_sha256 = candidate_builder.sha256_text(current_manifest_text)
    if (
        _require_sha256(
            candidate_gitops["source_manifest_sha256"],
            "candidate source_manifest_sha256",
        )
        != source_manifest_sha256
    ):
        raise ValueError("candidate source manifest hash does not match the current manifest")

    try:
        expected_candidate_text = candidate_builder.build_candidate_manifest(
            current_manifest_text,
            image_name=image_name,
            source_revision=source_revision,
            image_digest=image_digest,
        )
    except ValueError as exc:
        raise ValueError("current manifest cannot produce a valid release candidate") from exc
    if candidate_manifest_bytes != expected_candidate_text.encode("utf-8"):
        raise ValueError(
            "candidate manifest contains changes beyond the expected image transformation"
        )

    return {
        "schema_version": 1,
        "scope": "local_consistency",
        "attestation": "not_verified",
        "attestation_target": "release_evidence",
        "candidate_target": candidate_target,
        "verification": {
            "local_bundle_hashes": "verified",
            "release_evidence_provenance": "not_verified",
            "oci_image_sbom_subject": "not_verified",
        },
        "source_revision": source_revision,
        "image_name": image_name,
        "image_digest": image_digest,
        "image_reference": expected_reference,
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "release_evidence_sha256": _sha256_bytes(evidence_bytes),
        "approved": False,
        "oci_image_subject_verified": False,
        "gitops_pr_approved": False,
        "deployed": False,
    }


def _validate_attestation_result(raw_output: bytes) -> int:
    if not isinstance(raw_output, bytes):
        raise ValueError("GitHub attestation verifier returned malformed output")
    try:
        result = _parse_json(raw_output, "GitHub attestation verifier output")
    except ValueError as exc:
        raise ValueError("GitHub attestation verifier returned malformed output") from exc
    if not isinstance(result, list) or not result:
        raise ValueError("GitHub attestation verifier returned no verified attestations")

    for entry in result:
        if not isinstance(entry, dict):
            raise ValueError("GitHub attestation verifier returned malformed output")
        attestation = entry.get("attestation")
        verification = entry.get("verificationResult")
        if (
            not isinstance(attestation, dict)
            or not attestation
            or not isinstance(verification, dict)
        ):
            raise ValueError("GitHub attestation verifier returned malformed output")
        signature = verification.get("signature")
        certificate = signature.get("certificate") if isinstance(signature, dict) else None
        statement = verification.get("statement")
        timestamps = verification.get("verifiedTimestamps")
        if (
            not isinstance(certificate, dict)
            or not certificate
            or not isinstance(statement, dict)
            or not statement
            or not isinstance(timestamps, list)
        ):
            raise ValueError("GitHub attestation verifier returned malformed output")
    return len(result)


def verify_attestation(release_evidence: Path, source_revision: str) -> int:
    """Run GitHub's verifier with fixed identity/source policy; never expose stderr."""
    command = [
        "gh",
        "attestation",
        "verify",
        str(release_evidence),
        "--repo",
        EXPECTED_REPOSITORY,
        "--signer-workflow",
        EXPECTED_SIGNER_WORKFLOW,
        "--source-digest",
        source_revision,
        "--source-ref",
        "refs/heads/main",
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=ATTESTATION_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("GitHub attestation verification timed out") from exc
    except OSError as exc:
        raise ValueError("GitHub attestation verifier could not be started") from exc

    if completed.returncode != 0:
        raise ValueError("GitHub attestation verification failed")
    return _validate_attestation_result(completed.stdout)


def _validate_spdx_image_attestation_result(
    raw_output: bytes, image_name: str, image_digest: str
) -> int:
    count = _validate_attestation_result(raw_output)
    result = _parse_json(raw_output, "GitHub attestation verifier output")
    expected_digest = image_digest.removeprefix("sha256:")
    for entry in result:
        statement = entry["verificationResult"]["statement"]
        if statement.get("predicateType") != EXPECTED_SPDX_PREDICATE:
            raise ValueError("verified OCI image attestation does not use the SPDX predicate")
        subjects = statement.get("subject")
        if not isinstance(subjects, list) or not any(
            isinstance(subject, dict)
            and subject.get("name") == image_name
            and isinstance(subject.get("digest"), dict)
            and subject["digest"].get("sha256") == expected_digest
            for subject in subjects
        ):
            raise ValueError("verified OCI image subject does not match the image name and digest")
    return count


def verify_image_attestation(image_name: str, image_digest: str, source_revision: str) -> int:
    """Verify the signed SPDX SBOM attached to the exact OCI image digest."""
    candidate_builder.validate_image_name(image_name)
    candidate_builder.validate_image_digest(image_digest)
    candidate_builder.validate_source_revision(source_revision)
    command = [
        "gh",
        "attestation",
        "verify",
        f"oci://{image_name}@{image_digest}",
        "--repo",
        EXPECTED_REPOSITORY,
        "--bundle-from-oci",
        "--signer-workflow",
        EXPECTED_SIGNER_WORKFLOW,
        "--source-digest",
        source_revision,
        "--source-ref",
        "refs/heads/main",
        "--deny-self-hosted-runners",
        "--predicate-type",
        EXPECTED_SPDX_PREDICATE,
        "--format",
        "json",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=ATTESTATION_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("OCI image attestation verification timed out") from exc
    except OSError as exc:
        raise ValueError("GitHub attestation verifier could not be started") from exc

    if completed.returncode != 0:
        raise ValueError("OCI image attestation verification failed")
    return _validate_spdx_image_attestation_result(
        completed.stdout, image_name, image_digest
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--release-evidence", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--current-manifest", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--image-name", required=True)
    parser.add_argument(
        "--candidate-target", choices=tuple(CANDIDATE_MANIFEST_NAMES), default="prod"
    )
    parser.add_argument("--verify-attestation", action="store_true")
    parser.add_argument("--verify-image-attestation", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = validate_bundle(
            candidate_dir=args.candidate_dir,
            release_evidence=args.release_evidence,
            sbom=args.sbom,
            current_manifest=args.current_manifest,
            source_revision=args.source_revision,
            image_name=args.image_name,
            candidate_target=args.candidate_target,
        )
        if args.verify_attestation or args.verify_image_attestation:
            before_verification_sha256 = _sha256_bytes(
                _read_required(args.release_evidence, "release evidence")
            )
            if before_verification_sha256 != summary["release_evidence_sha256"]:
                raise ValueError("release evidence changed before attestation verification")
            before_sbom_sha256 = _sha256_bytes(_read_required(args.sbom, "SBOM"))
            if args.verify_attestation:
                release_count = verify_attestation(args.release_evidence, args.source_revision)
            if args.verify_image_attestation:
                image_count = verify_image_attestation(
                    summary["image_name"], summary["image_digest"], args.source_revision
                )
            summary_after_attestation = validate_bundle(
                candidate_dir=args.candidate_dir,
                release_evidence=args.release_evidence,
                sbom=args.sbom,
                current_manifest=args.current_manifest,
                source_revision=args.source_revision,
                image_name=args.image_name,
                candidate_target=args.candidate_target,
            )
            if summary_after_attestation["release_evidence_sha256"] != before_verification_sha256:
                raise ValueError("release evidence changed during attestation verification")
            if _sha256_bytes(_read_required(args.sbom, "SBOM")) != before_sbom_sha256:
                raise ValueError("SBOM changed during attestation verification")
            summary = summary_after_attestation
            if args.verify_attestation:
                summary["scope"] += "+release_evidence_provenance"
                summary["attestation"] = "verified"
                summary["verification"]["release_evidence_provenance"] = "verified"
                summary["verified_attestation_count"] = release_count
            if args.verify_image_attestation:
                summary["scope"] += "+oci_image_sbom_subject"
                summary["verification"]["oci_image_sbom_subject"] = "verified"
                summary["oci_image_subject_verified"] = True
                summary["verified_image_attestation_count"] = image_count
    except (OSError, ValueError) as exc:
        print(f"release bundle verification failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
