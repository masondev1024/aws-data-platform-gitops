import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "loadtest_ledger.py"
SPEC = importlib.util.spec_from_file_location("loadtest_ledger", SCRIPT_PATH)
ledger_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ledger_module)


def test_profiles_reserve_the_expected_maximum_request_counts():
    assert ledger_module.plan_requests("soak") == 102_000
    assert ledger_module.plan_requests("synchronized-refresh") == 10_000
    assert ledger_module.plan_requests("canary-apply") == 15_600
    assert ledger_module.plan_requests("apply") == 40
    assert ledger_module.plan_requests("readiness") == 150


def test_request_reservation_is_atomic_private_and_session_scoped(tmp_path):
    path = tmp_path / "evidence" / "requests.json"

    ledger = ledger_module.reserve(path, "live-2026-09-23", "run-000001", "readiness", 150)

    assert ledger_module._count_committed_and_reserved(ledger) == 150
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ledger_module.LedgerError, match="already exists"):
        ledger_module.reserve(path, "live-2026-09-23", "run-000001", "readiness", 150)
    with pytest.raises(ledger_module.LedgerError, match="schema/session mismatch"):
        ledger_module.reserve(path, "another-session", "run-000002", "readiness", 150)


def test_measured_counts_release_unused_reservation_and_are_reconciled(tmp_path):
    path = tmp_path / "ledger.json"
    ledger_module.reserve(path, "live-2026-09-23", "run-000001", "readiness", 150)

    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"metrics": {"http_reqs": {"values": {"count": 17}}}}))
    actual = ledger_module.measured_request_count(summary)
    ledger = ledger_module.reconcile(path, "live-2026-09-23", "run-000001", actual, 0)

    assert ledger["runs"][0]["status"] == "completed"
    assert ledger["runs"][0]["actual_requests"] == 17
    assert ledger_module._count_committed_and_reserved(ledger) == 17


def test_missing_summary_keeps_reservation_and_blocks_duplicate_run(tmp_path):
    path = tmp_path / "ledger.json"
    ledger_module.reserve(path, "live-2026-09-23", "run-000001", "soak", 102_000)

    with pytest.raises(ledger_module.LedgerError, match="lacks a valid http_reqs count"):
        ledger_module.measured_request_count(tmp_path / "missing.json")
    with pytest.raises(ledger_module.LedgerError, match="already exists"):
        ledger_module.reserve(path, "live-2026-09-23", "run-000001", "soak", 102_000)


def test_any_overrun_locks_out_followup_traffic_even_below_global_ceiling(tmp_path):
    path = tmp_path / "ledger.json"
    ledger_module.reserve(path, "live-2026-09-23", "run-000001", "readiness", 150)
    ledger_module.reconcile(path, "live-2026-09-23", "run-000001", 151, 0)

    with pytest.raises(ledger_module.LedgerError, match="prior run exceeded its reservation"):
        ledger_module.reserve(path, "live-2026-09-23", "run-000002", "readiness", 150)


def test_session_ceiling_is_enforced_across_runs(tmp_path):
    path = tmp_path / "ledger.json"
    ledger_module.reserve(path, "live-2026-09-23", "run-000001", "soak", 102_000)

    with pytest.raises(ledger_module.LedgerError, match="request budget exceeded"):
        ledger_module.reserve(path, "live-2026-09-23", "run-000002", "soak", 102_000)


def test_boundary_review_preserves_failure_and_counts_extra_request(tmp_path):
    path = tmp_path / "ledger.json"
    ledger_module.reserve(path, "live-2026-09-23", "run-000001", "catalog-capacity", 6000)
    ledger_module.reconcile(path, "live-2026-09-23", "run-000001", 6001, 0)
    proof = tmp_path / "guard-test.txt"
    proof.write_text("offline boundary test passed after hard cap")
    ledger_module.review_boundary_overrun(path, "live-2026-09-23", "run-000001",
                                         "End boundary sent one extra; hard cap regression passed.", proof)
    result = ledger_module.reserve(path, "live-2026-09-23", "run-000002", "soak", 102000)
    assert result["runs"][0]["status"] == "over_budget"
    assert result["runs"][0]["actual_requests"] == 6001
    assert result["runs"][0]["boundary_review"]["evidence_sha256"]
    assert ledger_module._count_committed_and_reserved(result) == 108001
    with pytest.raises(ledger_module.LedgerError, match="budget exceeded"):
        ledger_module.reserve(path, "live-2026-09-23", "run-000003", "soak", 102000)


@pytest.mark.parametrize("actual", [6002, 200001])
def test_boundary_review_cannot_release_larger_or_session_overruns(tmp_path, actual):
    path = tmp_path / "ledger.json"
    ledger_module.reserve(path, "live-2026-09-23", "run-000001", "catalog-capacity", 6000)
    try:
        ledger_module.reconcile(path, "live-2026-09-23", "run-000001", actual, 0)
    except ledger_module.LedgerError:
        pass
    proof = tmp_path / "test.txt"
    proof.write_text("proof")
    with pytest.raises(ledger_module.LedgerError, match="single-request"):
        ledger_module.review_boundary_overrun(path, "live-2026-09-23", "run-000001",
                                             "Reviewed boundary overflow with fixed guard.", proof)


def test_unreadable_ledger_fails_closed(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("not-json")

    with pytest.raises(ledger_module.LedgerError, match="unreadable"):
        ledger_module.reserve(path, "live-2026-09-23", "run-000001", "readiness", 150)


@pytest.mark.parametrize("count", [-1, 1.5, "10", True])
def test_summary_request_count_must_be_a_nonnegative_integer(tmp_path, count):
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"metrics": {"http_reqs": {"values": {"count": count}}}}))

    with pytest.raises(ledger_module.LedgerError, match="non-negative integer"):
        ledger_module.measured_request_count(summary)
