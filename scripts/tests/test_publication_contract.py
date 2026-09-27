from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", path],
        cwd=ROOT,
        check=False,
    )
    return result.returncode == 0


def test_root_readme_is_the_only_tracked_markdown_file():
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--", "*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == ["README.md"]
    assert not ignored("README.md")


def test_root_readme_does_not_link_to_private_markdown():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    destinations = re.findall(r"\]\(([^)]+\.md(?:#[^)]+)?)\)", readme)
    local_destinations = [
        target for target in destinations
        if not target.startswith(("https://", "http://"))
    ]
    assert all(target.split("#", 1)[0] == "README.md" for target in local_destinations)


def test_machine_state_personal_docs_evidence_and_secrets_are_ignored():
    private_paths = (
        ".omc/project-memory.json",
        ".config/k6/installation-id",
        "docs/private-engineering-note.md",
        "evidence/new-run.json",
        "secrets/local-token",
        "platform/live-lab/evidence/session/output.json",
        "platform/live-lab/secrets/session-token",
        "platform/live-lab/README.md",
        "platform/live-lab/evidence/README.md",
        "platform/live-lab/manifests/README.md",
        "platform/live-lab/secrets/README.md",
        "platform/jenkins/README.md",
        "platform/governance/README.ko.md",
        "agentic_ops/README.ko.md",
        "agentic_ops/BEDROCK.md",
        "OPERATIONS.md",
    )
    assert all(ignored(path) for path in private_paths)
    assert not ignored("platform/live-lab/evidence/schema.json")
