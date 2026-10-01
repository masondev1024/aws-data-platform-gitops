import json
import re
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text()


def test_cd_publish_waits_for_same_commit_platform_verification():
    workflow = read(".github/workflows/cd.yaml")

    assert "uses: ./.github/workflows/platform-verify.yaml" in workflow
    assert "publish:\n    name: Publish verified image and prepare GitOps candidate\n    needs: platform-verify" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow


def test_cd_does_not_push_or_mutate_the_production_manifest():
    workflow = read(".github/workflows/cd.yaml")

    assert "contents: write" not in workflow
    assert "git push" not in workflow
    assert "git commit" not in workflow
    assert "git diff --exit-code -- k8s/overlays/prod/kustomization.yaml" in workflow


def test_cd_scans_new_images_before_first_push_and_binds_registry_digest():
    workflow = read(".github/workflows/cd.yaml")
    build_index = workflow.index('docker build --pull --tag "$image_uri:$IMAGE_TAG" ./app')
    inspect_local_index = workflow.index('local_image_id="$(docker image inspect', build_index)
    scan_index = workflow.index(
        'trivy image --image-src docker --ignorefile .trivyignore --vex .security/vex/live-lab-openssl.openvex.json --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL',
        build_index,
    )
    push_index = workflow.index('docker push "$image_uri:$IMAGE_TAG"')
    describe_after_push_index = workflow.index('image_digest="$(aws ecr describe-images', push_index)
    pull_digest_index = workflow.index('docker pull "$image_uri@$image_digest"', push_index)
    inspect_registry_index = workflow.index('registry_image_id="$(docker image inspect', pull_digest_index)
    compare_index = workflow.index('test "$local_image_id" = "$registry_image_id"', inspect_registry_index)

    assert build_index < inspect_local_index < scan_index < push_index
    assert push_index < describe_after_push_index < pull_digest_index < inspect_registry_index < compare_index
    assert 'printf \'reference=%s@%s\\n\' "$image_uri" "$image_digest"' in workflow


def test_cd_requires_immutable_ecr_repository_before_publish():
    workflow = read(".github/workflows/cd.yaml")
    immutability_index = workflow.index("aws ecr describe-repositories")
    image_lookup_index = workflow.index("aws ecr describe-images")

    assert immutability_index < image_lookup_index
    assert "--repository-names \"$ECR_REPOSITORY\"" in workflow
    assert "--query 'repositories[0].imageTagMutability'" in workflow
    assert 'test "$repository_mutability" = "IMMUTABLE"' in workflow
    assert "ECR repository must enforce immutable tags" in workflow


def test_cd_scans_existing_digest_from_local_docker_source():
    workflow = read(".github/workflows/cd.yaml")
    existing_digest_index = workflow.index('if [ "$describe_status" -eq 0 ]; then')
    pull_existing_index = workflow.index('docker pull "$image_uri@$image_digest"', existing_digest_index)
    inspect_existing_index = workflow.index('registry_image_id="$(docker image inspect', pull_existing_index)
    scan_existing_index = workflow.index(
        'trivy image --image-src docker --ignorefile .trivyignore --vex .security/vex/live-lab-openssl.openvex.json --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL',
        inspect_existing_index,
    )

    assert pull_existing_index < inspect_existing_index < scan_existing_index


def test_cd_fail_closed_for_registry_lookup_errors():
    workflow = read(".github/workflows/cd.yaml")

    assert 'grep -q "ImageNotFoundException" "$describe_err"' in workflow
    assert 'echo "::error::Could not determine immutable tag state in ECR."' in workflow
    assert 'exit "$describe_status"' in workflow


def test_container_vex_is_package_and_version_scoped_and_expires_for_reassessment():
    vex = json.loads(read(".security/vex/live-lab-openssl.openvex.json"))
    statements = vex["statements"]

    assert {item["vulnerability"]["name"] for item in statements} == {
        "CVE-2026-75804",
        "CVE-2026-84782",
    }
    assert len(statements) == 2
    for statement in statements:
        assert statement["products"] == [
            {"@id": "pkg:deb/debian/libssl3t64@3.5.7-1~deb13u2"}
        ]
        assert statement["status"] == "not_affected"
        assert statement["justification"] == "vulnerable_code_not_in_execute_path"
        assert "reassess by" in statement["impact_statement"].lower()
        deadline = re.search(
            r"reassess by (\d{4}-\d{2}-\d{2})",
            statement["impact_statement"],
            re.IGNORECASE,
        )
        assert deadline is not None
        remaining_days = (
            datetime.fromisoformat(deadline.group(1)).date()
            - datetime.now(timezone.utc).date()
        ).days
        assert 0 < remaining_days <= 14

    security = read(".github/workflows/security.yaml")
    cd = read(".github/workflows/cd.yaml")
    vex_arg = "--vex .security/vex/live-lab-openssl.openvex.json"
    assert security.count(vex_arg) == 1
    assert cd.count(vex_arg) == 2
    assert "--ignore-unfixed --severity HIGH,CRITICAL" in security
    assert "--ignore-unfixed --severity HIGH,CRITICAL" in cd
    assert "CVE-2026-75804" not in read(".trivyignore")
    assert "CVE-2026-84782" not in read(".trivyignore")


def test_shared_verify_contract_has_jenkins_phases_without_docker_or_k6():
    script = read("scripts/verify_platform.sh")

    for phase in ("tests", "manifests", "terraform", "python-security"):
        assert phase in script
    assert "docker " not in script
    assert "k6" not in script


def test_platform_verify_reusable_workflow_uses_promised_phase_names():
    workflow = read(".github/workflows/platform-verify.yaml")

    assert "workflow_call:" in workflow
    assert "scripts/verify_platform.sh tests" in workflow
    assert "scripts/verify_platform.sh manifests" in workflow
    assert "scripts/verify_platform.sh terraform" in workflow
    assert "scripts/verify_platform.sh python-security" in workflow


def test_shared_workflow_blocks_publication_on_iac_and_secret_scan_failure():
    workflow = read(".github/workflows/platform-verify.yaml")
    assert "infrastructure-and-secrets:" in workflow
    assert "trivy config --exit-code 1 --severity HIGH,CRITICAL" in workflow
    assert "trivy fs --exit-code 1 --scanners secret --severity HIGH,CRITICAL" in workflow
    assert "continue-on-error" not in workflow
