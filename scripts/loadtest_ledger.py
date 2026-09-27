#!/usr/bin/env python3
"""Reserve and reconcile the live-lab's session-wide k6 HTTP request budget."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any


REQUEST_CEILING = 200_000
DEFAULT_READ_RATE = 20
DEFAULT_READ_SECONDS = 60 * 60
DEFAULT_BURST_RATE = 100
DEFAULT_BURST_SECONDS = 5 * 60
DEFAULT_APPLY_RATE = 5
DEFAULT_APPLY_SECONDS = 13 * 60
DEFAULT_SYNCHRONIZED_REFRESH_VUS = 10_000


class LedgerError(ValueError):
    """Safe request-budget or evidence-contract error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plan_requests(mode: str, *, apply_vus: int = 10, readiness_rate: int = 5, readiness_seconds: int = 30) -> int:
    if mode == "soak":
        return DEFAULT_READ_RATE * DEFAULT_READ_SECONDS + DEFAULT_BURST_RATE * DEFAULT_BURST_SECONDS
    if mode == "synchronized-refresh":
        return DEFAULT_SYNCHRONIZED_REFRESH_VUS
    if mode == "canary-apply":
        # Each unique iteration performs CSRF bootstrap, signup, login, and apply.
        return DEFAULT_APPLY_RATE * DEFAULT_APPLY_SECONDS * 4
    if mode == "apply":
        return apply_vus * 4
    if mode in {"readiness", "health"}:
        return readiness_rate * readiness_seconds
    raise LedgerError(f"unsupported k6 mode: {mode}")


def empty_ledger(session_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "session_id": session_id,
        "request_ceiling": REQUEST_CEILING,
        "created_at": utc_now(),
        "runs": [],
    }


def _validate_session_id(session_id: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{5,40}", session_id):
        raise LedgerError("session_id must be a 6-41 character lowercase session identifier")


def _read_ledger(path: Path, session_id: str) -> dict[str, Any]:
    if not path.exists():
        return empty_ledger(session_id)
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError("request ledger is unreadable; do not run more load until it is inspected") from exc
    if (
        not isinstance(ledger, dict)
        or ledger.get("schema_version") != 1
        or ledger.get("session_id") != session_id
        or ledger.get("request_ceiling") != REQUEST_CEILING
        or not isinstance(ledger.get("runs"), list)
    ):
        raise LedgerError("request ledger schema/session mismatch; stop and inspect it")
    for row in ledger["runs"]:
        if not isinstance(row, dict) or row.get("status") not in {"reserved", "completed", "failed", "over_budget"}:
            raise LedgerError("request ledger contains an invalid run; stop and inspect it")
        if type(row.get("planned_requests")) is not int or row["planned_requests"] < 0:
            raise LedgerError("request ledger has an invalid planned request count")
        actual = row.get("actual_requests")
        if actual is not None and (type(actual) is not int or actual < 0):
            raise LedgerError("request ledger has an invalid measured request count")
        review = row.get("boundary_review")
        if review is not None and (
            not isinstance(review, dict) or row["status"] != "over_budget"
            or actual != row["planned_requests"] + 1
            or review.get("actual_requests") != actual
            or review.get("planned_requests") != row["planned_requests"]
            or not re.fullmatch(r"[a-f0-9]{64}", str(review.get("evidence_sha256", "")))
            or len(str(review.get("reason", "")).strip()) < 20
        ):
            raise LedgerError("request ledger has an invalid boundary review")
    return ledger


def _count_committed_and_reserved(ledger: dict[str, Any]) -> int:
    total = 0
    for row in ledger["runs"]:
        actual = row.get("actual_requests")
        total += actual if actual is not None else row["planned_requests"]
    return total


def _write_atomic(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(ledger, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def locked_ledger(path: Path, session_id: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.fchmod(lock_fd, stat.S_IRUSR | stat.S_IWUSR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        ledger = _read_ledger(path, session_id)
        yield ledger
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def reserve(path: Path, session_id: str, run_id: str, mode: str, planned: int) -> dict[str, Any]:
    _validate_session_id(session_id)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{6,64}", run_id):
        raise LedgerError("run_id must be 6-64 chars using letters, numbers, ., _, :, or -")
    if type(planned) is not int or planned <= 0:
        raise LedgerError("planned request count must be a positive integer")
    with locked_ledger(path, session_id) as ledger:
        if any(row.get("status") == "over_budget" and not row.get("boundary_review") for row in ledger["runs"]):
            raise LedgerError("a prior run exceeded its reservation; stop further traffic and inspect the ledger")
        if any(row.get("run_id") == run_id for row in ledger["runs"]):
            raise LedgerError("run_id already exists; never reuse a request-budget reservation")
        total = _count_committed_and_reserved(ledger)
        if total + planned > REQUEST_CEILING:
            raise LedgerError(
                f"session request budget exceeded: committed/reserved={total}, next={planned}, ceiling={REQUEST_CEILING}"
            )
        ledger["runs"].append({
            "run_id": run_id,
            "mode": mode,
            "planned_requests": planned,
            "actual_requests": None,
            "status": "reserved",
            "reserved_at": utc_now(),
        })
        _write_atomic(path, ledger)
        return ledger


def reconcile(path: Path, session_id: str, run_id: str, actual: int, exit_code: int) -> dict[str, Any]:
    _validate_session_id(session_id)
    if type(actual) is not int or actual < 0:
        raise LedgerError("actual request count must be a non-negative integer")
    with locked_ledger(path, session_id) as ledger:
        run = next((row for row in ledger["runs"] if row.get("run_id") == run_id), None)
        if run is None or run.get("status") != "reserved":
            raise LedgerError("run has no active reservation; inspect the ledger rather than retrying")
        run["actual_requests"] = actual
        run["exit_code"] = exit_code
        run["completed_at"] = utc_now()
        if actual > run["planned_requests"]:
            run["status"] = "over_budget"
        else:
            run["status"] = "completed" if exit_code == 0 else "failed"
        measured_total = sum(
            row["actual_requests"] if row.get("actual_requests") is not None else row["planned_requests"]
            for row in ledger["runs"]
        )
        if measured_total > REQUEST_CEILING:
            run["status"] = "over_budget"
        _write_atomic(path, ledger)
        if measured_total > REQUEST_CEILING:
            raise LedgerError("measured session requests exceeded 200,000; stop all further traffic")
        return ledger


def review_boundary_overrun(path: Path, session_id: str, run_id: str, reason: str, evidence: Path) -> dict[str, Any]:
    """Record a reviewed scheduler boundary defect without erasing the failure or raising the cap."""
    _validate_session_id(session_id)
    if len(reason.strip()) < 20 or not evidence.is_file() or evidence.stat().st_size == 0:
        raise LedgerError("boundary review requires a reason and a nonempty verification artifact")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    with locked_ledger(path, session_id) as ledger:
        run = next((row for row in ledger["runs"] if row.get("run_id") == run_id), None)
        if (run is None or run.get("status") != "over_budget" or run.get("boundary_review")
                or run.get("actual_requests") != run["planned_requests"] + 1
                or _count_committed_and_reserved(ledger) >= REQUEST_CEILING):
            raise LedgerError("only a single-request boundary overrun below the session ceiling can be reviewed once")
        run["boundary_review"] = {
            "reviewed_at": utc_now(), "reason": reason.strip(),
            "evidence_path": str(evidence.resolve()), "evidence_sha256": digest,
            "planned_requests": run["planned_requests"], "actual_requests": run["actual_requests"],
        }
        _write_atomic(path, ledger)
        return ledger


def measured_request_count(summary_path: Path) -> int:
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        count = summary["metrics"]["http_reqs"]["values"]["count"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LedgerError("k6 summary lacks a valid http_reqs count; reservation remains active") from exc
    if type(count) is not int or count < 0:
        raise LedgerError("k6 summary http_reqs count is not a non-negative integer")
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--mode", required=True)
    plan_parser.add_argument("--apply-vus", type=int, default=10)
    plan_parser.add_argument("--readiness-rate", type=int, default=5)
    plan_parser.add_argument("--readiness-seconds", type=int, default=30)

    reserve_parser = subparsers.add_parser("reserve")
    reserve_parser.add_argument("--ledger", type=Path, required=True)
    reserve_parser.add_argument("--session-id", required=True)
    reserve_parser.add_argument("--run-id", required=True)
    reserve_parser.add_argument("--mode", required=True)
    reserve_parser.add_argument("--planned", type=int, required=True)

    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--ledger", type=Path, required=True)
    complete_parser.add_argument("--session-id", required=True)
    complete_parser.add_argument("--run-id", required=True)
    complete_parser.add_argument("--summary", type=Path, required=True)
    complete_parser.add_argument("--exit-code", type=int, required=True)

    review_parser = subparsers.add_parser("review-boundary-overrun")
    review_parser.add_argument("--ledger", type=Path, required=True)
    review_parser.add_argument("--session-id", required=True)
    review_parser.add_argument("--run-id", required=True)
    review_parser.add_argument("--reason", required=True)
    review_parser.add_argument("--evidence", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            print(plan_requests(args.mode, apply_vus=args.apply_vus,
                                readiness_rate=args.readiness_rate,
                                readiness_seconds=args.readiness_seconds))
            return 0
        if args.command == "reserve":
            ledger = reserve(args.ledger, args.session_id, args.run_id, args.mode, args.planned)
            print(f"reserved session_requests={_count_committed_and_reserved(ledger)}/{REQUEST_CEILING}")
            return 0
        if args.command == "review-boundary-overrun":
            ledger = review_boundary_overrun(args.ledger, args.session_id, args.run_id, args.reason, args.evidence)
            print(f"boundary review recorded; original failure retained; session_requests={_count_committed_and_reserved(ledger)}/{REQUEST_CEILING}")
            return 0
        actual = measured_request_count(args.summary)
        ledger = reconcile(args.ledger, args.session_id, args.run_id, actual, args.exit_code)
        print(f"reconciled session_requests={_count_committed_and_reserved(ledger)}/{REQUEST_CEILING} actual={actual}")
        return 0
    except LedgerError as exc:
        print(f"BLOCKED: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
