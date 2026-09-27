import argparse
import sys
from pathlib import Path
from unittest.mock import Mock, call

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import replica_drill
from werkzeug.security import check_password_hash


def test_new_drill_users_get_unique_unpredictable_password_hashes(monkeypatch, capsys):
    generated_passwords = []
    token_urlsafe = replica_drill.secrets.token_urlsafe

    def capture_random_password(byte_count):
        password = token_urlsafe(byte_count)
        generated_passwords.append((byte_count, password))
        return password

    monkeypatch.setattr(replica_drill.secrets, "token_urlsafe", capture_random_password)
    stored_hashes = []
    for user_id in (41, 42):
        cursor = Mock()
        cursor.fetchone.return_value = None
        cursor.lastrowid = user_id

        assert replica_drill.select_or_create_user(cursor, f"drill-user-{user_id}") == user_id
        stored_hashes.append(cursor.execute.call_args.args[1][1])

    assert [size for size, _ in generated_passwords] == [32, 32]
    assert len({password for _, password in generated_passwords}) == 2
    assert len(set(stored_hashes)) == 2
    assert all(check_password_hash(hashed, password) for hashed, (_, password) in zip(stored_hashes, generated_passwords))
    assert all(hashed != password for hashed, (_, password) in zip(stored_hashes, generated_passwords))
    assert capsys.readouterr().out == ""


def test_existing_drill_user_is_selected_without_replacing_credentials(monkeypatch):
    token_urlsafe = Mock(side_effect=AssertionError("existing user must not be reseeded"))
    monkeypatch.setattr(replica_drill.secrets, "token_urlsafe", token_urlsafe)
    cursor = Mock()
    cursor.fetchone.return_value = {"id": 73}

    assert replica_drill.select_or_create_user(cursor, "existing-drill-user") == 73

    cursor.execute.assert_called_once_with("SELECT id FROM users WHERE username = %s", ("existing-drill-user",))
    token_urlsafe.assert_not_called()


def test_wait_for_marker_retries_until_async_replica_catches_up(monkeypatch):
    row = {
        "run_id": "rds-live-260923-02",
        "username": "rds-drill-rds-live-260923-02",
        "user_id": 10,
        "entry_id": 20,
        "event_id": "event-123",
    }
    find_marker = Mock(side_effect=[None, None, row])
    output = Mock()
    sleep = Mock()
    clock = Mock(side_effect=[10.0, 10.1, 10.3, 10.5])
    monkeypatch.setattr(replica_drill, "find_marker", find_marker)
    monkeypatch.setattr(replica_drill, "output", output)
    monkeypatch.setattr(replica_drill.time, "sleep", sleep)
    monkeypatch.setattr(replica_drill.time, "monotonic", clock)

    replica_drill.wait_for_marker(
        argparse.Namespace(
            role="reader",
            run_id=row["run_id"],
            username=row["username"],
            timeout_seconds=30,
            poll_interval_seconds=5,
        )
    )

    assert find_marker.call_count == 3
    assert sleep.call_args_list == [call(5), call(5)]
    assert output.call_args.args[0]["action"] == "wait-marker"
    assert output.call_args.args[0]["waited_seconds"] == 0.5


def test_wait_for_marker_fails_closed_after_bounded_timeout(monkeypatch):
    monkeypatch.setattr(replica_drill, "find_marker", Mock(return_value=None))
    monkeypatch.setattr(replica_drill.time, "monotonic", Mock(side_effect=[1.0, 2.1]))

    with pytest.raises(replica_drill.DrillError, match="within 1s"):
        replica_drill.wait_for_marker(
            argparse.Namespace(
                role="reader",
                run_id="rds-live-260923-02",
                username="rds-drill-rds-live-260923-02",
                timeout_seconds=1,
                poll_interval_seconds=5,
            )
        )


def test_wait_marker_cli_bounds_timeout_and_poll_interval():
    parser = replica_drill.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["wait-marker", "--role", "reader", "--run-id", "rds-live-260923-02",
             "--username", "rds-drill-user", "--timeout-seconds", "601"]
        )
