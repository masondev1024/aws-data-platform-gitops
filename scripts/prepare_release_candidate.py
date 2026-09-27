#!/usr/bin/env python3
"""Create deterministic GitOps candidate artifacts without mutating prod manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_NAME_PATTERN = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$"
)
TARGET_IMAGE_KEY = "data-pipeline-app"
CANDIDATE_TARGETS = {
    "prod": "prod-kustomization.yaml",
    "validation": "validation-kustomization.yaml",
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def validate_source_revision(value: str) -> None:
    if not COMMIT_PATTERN.fullmatch(value):
        raise ValueError("source revision must be a full 40-character hexadecimal Git SHA")


def validate_image_digest(value: str) -> None:
    if not IMAGE_DIGEST_PATTERN.fullmatch(value):
        raise ValueError("image digest must have the sha256:<64 hexadecimal characters> form")


def validate_image_name(value: str) -> None:
    if "@" in value or ":" in value.rsplit("/", 1)[-1]:
        raise ValueError("image name must not include a tag or digest")
    if not IMAGE_NAME_PATTERN.fullmatch(value):
        raise ValueError("image name must be a registry/repository path")


def find_target_image_block(lines: list[str], image_key: str = TARGET_IMAGE_KEY) -> tuple[int, int]:
    images_line = next((index for index, line in enumerate(lines) if line.strip() == "images:"), None)
    if images_line is None:
        raise ValueError("kustomization.yaml must contain an images section")

    image_entries = [
        index
        for index in range(images_line + 1, len(lines))
        if lines[index].startswith("  - name:")
    ]
    if len(image_entries) != 1:
        raise ValueError("kustomization must contain exactly one image override entry")

    start = image_entries[0]
    if lines[start].strip() != f"- name: {image_key}":
        raise ValueError(f"image override must target {image_key}")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("  - name:") or (lines[index] and not lines[index].startswith(" ")):
            end = index
            break
    return start, end


def build_candidate_manifest(
    original_text: str,
    *,
    image_name: str,
    source_revision: str,
    image_digest: str,
) -> str:
    lines = original_text.splitlines()
    start, end = find_target_image_block(lines)
    block = lines[start:end]

    seen = {"newName": False, "newTag": False, "digest": False}
    replacement = {
        "newName": f"    newName: {image_name}",
        "newTag": f"    newTag: {source_revision}",
        "digest": f"    digest: {image_digest}",
    }
    candidate_block: list[str] = []
    for line in block:
        stripped = line.strip()
        key = stripped.split(":", 1)[0]
        if key in replacement:
            candidate_block.append(replacement[key])
            seen[key] = True
        else:
            candidate_block.append(line)

    if not seen["newName"]:
        candidate_block.append(replacement["newName"])
    if not seen["newTag"]:
        candidate_block.append(replacement["newTag"])
    if not seen["digest"]:
        candidate_block.append(replacement["digest"])

    candidate_lines = lines[:start] + candidate_block + lines[end:]
    return "\n".join(candidate_lines) + "\n"


def rendered_images(rendered_manifest: str) -> list[str]:
    images: list[str] = []
    for line in rendered_manifest.splitlines():
        stripped = line.strip()
        if stripped.startswith("image: "):
            images.append(stripped.split("image: ", 1)[1])
        elif stripped.startswith("- image: "):
            images.append(stripped.split("- image: ", 1)[1])
    return images


def validate_rendered_images(rendered_manifest: str, expected_reference: str) -> int:
    images = rendered_images(rendered_manifest)
    if not images:
        raise ValueError("rendered manifest does not contain any container images")
    unexpected = sorted({image for image in images if image != expected_reference})
    if unexpected:
        raise ValueError(f"rendered manifest contains unexpected image references: {unexpected}")
    return len(images)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--image-name", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--manifest-path", type=Path, required=True)
    parser.add_argument("--candidate-target", choices=tuple(CANDIDATE_TARGETS), default="prod")
    parser.add_argument("--rendered-manifest-path", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    candidate_target = getattr(args, "candidate_target", "prod")
    if candidate_target not in CANDIDATE_TARGETS:
        raise ValueError("candidate target must be prod or validation")
    validate_source_revision(args.source_revision)
    validate_image_name(args.image_name)
    validate_image_digest(args.image_digest)
    if not args.manifest_path.is_file():
        raise FileNotFoundError(f"{candidate_target} manifest is missing: {args.manifest_path}")

    original_text = args.manifest_path.read_text()
    candidate_text = build_candidate_manifest(
        original_text,
        image_name=args.image_name,
        source_revision=args.source_revision,
        image_digest=args.image_digest,
    )
    expected_reference = f"{args.image_name}:{args.source_revision}@{args.image_digest}"
    rendered_image_count = None
    if args.rendered_manifest_path is not None:
        if not args.rendered_manifest_path.is_file():
            raise FileNotFoundError(f"rendered manifest is missing: {args.rendered_manifest_path}")
        rendered_image_count = validate_rendered_images(
            args.rendered_manifest_path.read_text(),
            expected_reference,
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    candidate_manifest_path = args.output_dir / CANDIDATE_TARGETS[candidate_target]
    candidate_manifest_path.write_text(candidate_text)

    metadata = {
        "schema_version": 1,
        "target": candidate_target,
        "source": {"revision": args.source_revision},
        "image": {
            "name": args.image_name,
            "digest": args.image_digest,
            "tag": args.source_revision,
            "reference": expected_reference,
        },
        "gitops_candidate": {
            "source_manifest_path": str(args.manifest_path),
            "candidate_manifest_path": str(candidate_manifest_path),
            "source_manifest_sha256": sha256_text(original_text),
            "candidate_manifest_sha256": sha256_text(candidate_text),
            "rendered_image_count": rendered_image_count,
            "direct_main_push": False,
            "requires_operator_review_pr": True,
        },
    }
    write_json(args.output_dir / "release-candidate.json", metadata)


if __name__ == "__main__":
    main()
