import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schema
from test_app import FakeConnection, FakeCursor


def test_outbox_schema_has_one_event_per_raffle_entry_and_an_unpublished_index():
    schema_definition = "\n".join(schema.SCHEMA_STATEMENTS)

    assert "CREATE TABLE IF NOT EXISTS raffle_outbox_events" in schema_definition
    assert "UNIQUE KEY unique_raffle_entry_event (event_type, aggregate_id)" in schema_definition
    assert "KEY idx_raffle_outbox_unpublished (published_at, created_at)" in schema_definition
    assert "KEY idx_raffle_entries_entry_time (entry_time)" in schema_definition


def test_schema_migrations_are_additive_and_do_not_seed_production_rows():
    cursor = FakeCursor(fetchone_result={"cnt": 0})
    connection = FakeConnection(cursor)

    schema.apply_schema_migrations(connection, seed_sample_data=False)

    assert connection.commit_count == 1
    assert len(cursor.executed) == len(schema.SCHEMA_STATEMENTS) + 2
    assert any("information_schema.statistics" in statement for statement, _ in cursor.executed)
    assert any("ALTER TABLE raffle_entries ADD INDEX" in statement for statement, _ in cursor.executed)
    assert cursor.executemany_calls == []


def test_invalid_database_identifier_is_rejected_before_opening_a_connection():
    with patch("schema.pymysql.connect") as connect:
        with pytest.raises(ValueError, match="DB_NAME"):
            schema.ensure_database_exists(
                host="db.example",
                user="app",
                password="not-logged",
                database_name="raffle_db; DROP DATABASE raffle_db",
            )

    connect.assert_not_called()
