"""In-cluster RDS recovery drill probes for the live-lab raffle app.

This helper is intended to run inside a short-lived Kubernetes Job that receives
database settings from raffle-config/raffle-secret and the mounted RDS CA bundle.
It never prints passwords and uses parameterized SQL for all user-controlled
values.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import secrets
import time
from typing import Any
from uuid import uuid4

import pymysql
from werkzeug.security import generate_password_hash

from db import db_tls_options


RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{6,64}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
ROLE_CHOICES = ("writer", "reader")
OUTBOX_EVENT_TYPE = "raffle.entry.accepted.v1"
OUTBOX_EVENT_VERSION = 1


class DrillError(RuntimeError):
    """A fail-closed drill validation error."""


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise DrillError("run_id must be 6-64 chars using letters, numbers, ., _, :, or -")
    return run_id


def validate_username(username: str) -> str:
    if not USERNAME_RE.fullmatch(username):
        raise DrillError("username must be 3-50 chars using letters, numbers, ., _, or -")
    return username


def output(payload: dict[str, Any]) -> None:
    payload.setdefault("observed_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print(json.dumps(payload, sort_keys=True))


def db_host(role: str) -> str:
    env_name = "DB_WRITER_HOST" if role == "writer" else "DB_READER_HOST"
    host = os.environ.get(env_name, "")
    if not host:
        raise DrillError(f"{env_name} is required")
    return host


def connect(role: str):
    password = os.environ.get("DB_APP_PASSWORD") or os.environ.get("DB_PASSWORD")
    user = os.environ.get("DB_APP_USER") or os.environ.get("DB_USER")
    database = os.environ.get("DB_NAME", "raffle_db")
    if not user or not password:
        raise DrillError("DB_APP_USER/DB_APP_PASSWORD are required")
    return pymysql.connect(
        host=db_host(role),
        user=user,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
        **db_tls_options(),
    )


def readiness(args: argparse.Namespace) -> None:
    with connect(args.role) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 AS ok")
            row = cursor.fetchone() or {}
    if row.get("ok") != 1:
        raise DrillError("readiness probe did not return ok=1")
    output({"action": "readiness", "role": args.role, "status": "ok"})


def select_or_create_user(cursor, username: str) -> int:
    cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
    row = cursor.fetchone()
    if row:
        return int(row["id"])
    unusable_password = generate_password_hash(secrets.token_urlsafe(32))
    cursor.execute(
        "INSERT INTO users (username, password) VALUES (%s, %s)",
        (username, unusable_password),
    )
    return int(cursor.lastrowid)


def select_raffle_item(cursor) -> int:
    cursor.execute("SELECT id FROM raffle_items ORDER BY id ASC LIMIT 1")
    row = cursor.fetchone()
    if not row:
        raise DrillError("at least one raffle_items row is required for a synthetic entry")
    return int(row["id"])


def select_or_create_entry(cursor, *, user_id: int, item_id: int) -> int:
    cursor.execute(
        "SELECT id FROM raffle_entries WHERE user_id = %s AND item_id = %s",
        (user_id, item_id),
    )
    row = cursor.fetchone()
    if row:
        return int(row["id"])
    cursor.execute(
        "INSERT INTO raffle_entries (user_id, item_id) VALUES (%s, %s)",
        (user_id, item_id),
    )
    return int(cursor.lastrowid)


def select_or_create_outbox_event(cursor, *, entry_id: int, user_id: int, item_id: int) -> str:
    cursor.execute(
        """
        SELECT event_id
        FROM raffle_outbox_events
        WHERE aggregate_type = %s
          AND aggregate_id = %s
          AND event_type = %s
        """,
        ("raffle_entry", entry_id, OUTBOX_EVENT_TYPE),
    )
    row = cursor.fetchone()
    if row:
        return str(row["event_id"])
    event_id = str(uuid4())
    event = {
        "event_id": event_id,
        "event_type": OUTBOX_EVENT_TYPE,
        "event_version": OUTBOX_EVENT_VERSION,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": {
            "entry_id": entry_id,
            "user_id": user_id,
            "item_id": item_id,
        },
    }
    cursor.execute(
        """
        INSERT INTO raffle_outbox_events (
            event_id, aggregate_type, aggregate_id, event_type, event_version, payload
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            event_id,
            "raffle_entry",
            entry_id,
            OUTBOX_EVENT_TYPE,
            OUTBOX_EVENT_VERSION,
            json.dumps(event, separators=(",", ":"), sort_keys=True),
        ),
    )
    return event_id


def record_marker(args: argparse.Namespace) -> None:
    run_id = validate_run_id(args.run_id)
    username = validate_username(args.username)
    with connect("writer") as connection:
        try:
            with connection.cursor() as cursor:
                user_id = select_or_create_user(cursor, username)
                item_id = select_raffle_item(cursor)
                entry_id = select_or_create_entry(cursor, user_id=user_id, item_id=item_id)
                event_id = select_or_create_outbox_event(
                    cursor,
                    entry_id=entry_id,
                    user_id=user_id,
                    item_id=item_id,
                )
                cursor.execute(
                    """
                    INSERT INTO live_lab_cohort_markers (
                        run_id, username, user_id, entry_id, event_id, recorded_at
                    ) VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(6))
                    ON DUPLICATE KEY UPDATE
                        username = VALUES(username),
                        user_id = VALUES(user_id),
                        entry_id = VALUES(entry_id),
                        event_id = VALUES(event_id),
                        recorded_at = VALUES(recorded_at)
                    """,
                    (run_id, username, user_id, entry_id, event_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    output(
        {
            "action": "record-marker",
            "status": "ok",
            "run_id": run_id,
            "username": username,
            "user_id": user_id,
            "entry_id": entry_id,
            "event_id": event_id,
        }
    )


def find_marker(role: str, run_id: str, username: str) -> dict[str, Any] | None:
    with connect(role) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    markers.run_id,
                    markers.username,
                    markers.user_id,
                    markers.entry_id,
                    markers.event_id,
                    entries.id AS entry_exists,
                    users.id AS user_exists,
                    events.event_id AS outbox_event_exists,
                    events.event_type
                FROM live_lab_cohort_markers AS markers
                JOIN users ON users.id = markers.user_id
                JOIN raffle_entries AS entries ON entries.id = markers.entry_id
                JOIN raffle_outbox_events AS events ON events.event_id = markers.event_id
                WHERE markers.run_id = %s
                  AND markers.username = %s
                  AND users.username = markers.username
                  AND entries.user_id = markers.user_id
                  AND events.aggregate_type = %s
                  AND events.aggregate_id = markers.entry_id
                  AND events.event_type = %s
                """,
                (run_id, username, "raffle_entry", OUTBOX_EVENT_TYPE),
            )
            return cursor.fetchone()


def emit_marker(action: str, role: str, row: dict[str, Any], waited_seconds: float = 0) -> None:
    payload = {
        "action": action,
        "role": role,
        "status": "ok",
        "run_id": row["run_id"],
        "username": row["username"],
        "user_id": int(row["user_id"]),
        "entry_id": int(row["entry_id"]),
        "event_id": row["event_id"],
    }
    if action == "wait-marker":
        payload["waited_seconds"] = round(waited_seconds, 3)
    output(payload)


def verify_marker(args: argparse.Namespace) -> None:
    run_id = validate_run_id(args.run_id)
    username = validate_username(args.username)
    row = find_marker(args.role, run_id, username)
    if not row:
        raise DrillError(f"marker parity not found on {args.role}")
    emit_marker("verify-marker", args.role, row)


def wait_for_marker(args: argparse.Namespace) -> None:
    run_id = validate_run_id(args.run_id)
    username = validate_username(args.username)
    deadline = time.monotonic() + args.timeout_seconds
    while True:
        row = find_marker(args.role, run_id, username)
        if row:
            emit_marker("wait-marker", args.role, row, args.timeout_seconds - max(0, deadline - time.monotonic()))
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DrillError(f"marker parity not found on {args.role} within {args.timeout_seconds}s")
        time.sleep(min(args.poll_interval_seconds, remaining))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    readiness_parser = subparsers.add_parser("readiness")
    readiness_parser.add_argument("--role", choices=ROLE_CHOICES, required=True)
    readiness_parser.set_defaults(func=readiness)

    record_parser = subparsers.add_parser("record-marker")
    record_parser.add_argument("--run-id", required=True)
    record_parser.add_argument("--username", required=True)
    record_parser.set_defaults(func=record_marker)

    verify_parser = subparsers.add_parser("verify-marker")
    verify_parser.add_argument("--role", choices=ROLE_CHOICES, required=True)
    verify_parser.add_argument("--run-id", required=True)
    verify_parser.add_argument("--username", required=True)
    verify_parser.set_defaults(func=verify_marker)

    wait_parser = subparsers.add_parser("wait-marker")
    wait_parser.add_argument("--role", choices=ROLE_CHOICES, required=True)
    wait_parser.add_argument("--run-id", required=True)
    wait_parser.add_argument("--username", required=True)
    wait_parser.add_argument("--timeout-seconds", type=int, choices=range(1, 601), default=300)
    wait_parser.add_argument("--poll-interval-seconds", type=int, choices=range(1, 31), default=5)
    wait_parser.set_defaults(func=wait_for_marker)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except DrillError as error:
        output({"action": args.action, "status": "failed", "error": str(error)})
        return 2
    except pymysql.MySQLError as error:
        output({"action": args.action, "status": "failed", "error": error.__class__.__name__})
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
