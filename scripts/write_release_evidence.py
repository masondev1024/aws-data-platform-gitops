#!/usr/bin/env python3
"""Create the machine-readable evidence attached to a GitOps image release."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re


COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--gitops-revision")
    parser.add_argument("--image-name", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--sbom-path", type=Path, required=True)
    parser.add_argument("--manifest-path", type=Path, required=True)
    parser.add_argument("--candidate-metadata-path", type=Path)
    parser.add_argument("--publication-mode", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def validate_commit(value: str, argument_name: str) -> None:
    if not COMMIT_PATTERN.fullmatch(value):
        raise ValueError(f"{argument_name} must be a hexadecimal Git revision")


def main() -> None:
    args = parse_args()
    validate_commit(args.source_revision, "source revision")
    if args.gitops_revision is not None:
        validate_commit(args.gitops_revision, "GitOps revision")
    if not IMAGE_DIGEST_PATTERN.fullmatch(args.image_digest):
        raise ValueError("image digest must have the sha256:<64 hexadecimal characters> form")
    if not args.sbom_path.is_file():
        raise FileNotFoundError(f"SBOM is missing: {args.sbom_path}")
    if not args.manifest_path.is_file():
        raise FileNotFoundError(f"GitOps manifest is missing: {args.manifest_path}")
    if args.candidate_metadata_path is not None and not args.candidate_metadata_path.is_file():
        raise FileNotFoundError(f"release candidate metadata is missing: {args.candidate_metadata_path}")

    gitops = {
        "manifest_path": str(args.manifest_path),
        "manifest_sha256": sha256_file(args.manifest_path),
        "direct_main_push": False,
    }
    if args.gitops_revision is not None:
        gitops["revision"] = args.gitops_revision
    if args.candidate_metadata_path is not None:
        gitops["candidate_metadata_path"] = str(args.candidate_metadata_path)
        gitops["candidate_metadata_sha256"] = sha256_file(args.candidate_metadata_path)

    evidence = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {"revision": args.source_revision},
        "gitops": gitops,
        "image": {
            "name": args.image_name,
            "digest": args.image_digest,
            "publication_mode": args.publication_mode,
        },
        "supply_chain": {
            "sbom_path": str(args.sbom_path),
            "sbom_sha256": sha256_file(args.sbom_path),
            "vulnerability_policy": "Block fix-available HIGH/CRITICAL vulnerabilities except reviewed .trivyignore entries; unfixed vulnerabilities are not blocked.",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
