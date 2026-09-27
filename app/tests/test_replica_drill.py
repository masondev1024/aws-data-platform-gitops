import argparse
import sys
from pathlib import Path
from unittest.mock import Mock, call

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import replica_drill


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
