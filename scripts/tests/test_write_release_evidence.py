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
