import json
import re
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module


app = app_module.app


class FakeCursor:
    def __init__(self, fetchone_result=None, lastrowid=42):
        self.fetchone_result = fetchone_result
        self.lastrowid = lastrowid
        self.executed = []
        self.executemany_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, parameters=None):
        self.executed.append((" ".join(statement.split()), parameters))

    def executemany(self, statement, parameters):
        self.executemany_calls.append((" ".join(statement.split()), parameters))

    def fetchone(self):
        return self.fetchone_result


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.close_count += 1


@pytest.fixture()
def client():
    app.config.update(TESTING=True, SECRET_KEY="test-secret", WTF_CSRF_ENABLED=True)
    with app.test_client() as test_client:
        yield test_client


def csrf_headers(client, page="/login"):
    response = client.get(page)
    match = re.search(r'<meta name="csrf-token" content="([^"]+)">', response.get_data(as_text=True))
    assert match, "expected rendered page to include a CSRF token"
    return {"X-CSRFToken": match.group(1)}


def test_health_check_does_not_require_database(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_metrics_endpoint_exposes_bounded_http_metrics(client):
    client.get("/healthz")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.content_type.startswith("text/plain")
    body = response.get_data(as_text=True)
    assert "raffle_http_requests_total" in body
    assert 'route="/healthz"' in body
    assert "raffle_http_request_duration_seconds" in body
    assert "raffle_outbox_events_total" in body


def test_metrics_exposes_database_backed_outbox_parity(client, monkeypatch):
    monkeypatch.setattr(app_module, "DB_WRITER_HOST", "writer.internal")
    cursor = FakeCursor(fetchone_result={"missing_events": 0})
    connection = FakeConnection(cursor)

    with patch.object(app_module, "get_db_connection", return_value=connection):
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "raffle_apply_outbox_parity_gap 0.0" in response.get_data(as_text=True)
    assert any("LEFT JOIN raffle_outbox_events" in statement for statement, _ in cursor.executed)
    assert connection.close_count == 1


def test_metrics_fails_closed_when_outbox_parity_query_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(app_module, "DB_WRITER_HOST", "writer.internal")

    with patch.object(
        app_module,
        "get_db_connection",
        side_effect=app_module.pymysql.OperationalError("writer unavailable"),
    ):
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "raffle_apply_outbox_parity_gap -1.0" in response.get_data(as_text=True)


def test_metrics_fails_closed_when_production_writer_endpoint_is_missing(client, monkeypatch):
    monkeypatch.setattr(app_module, "DB_WRITER_HOST", None)
    monkeypatch.setenv("FLASK_ENV", "production")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "raffle_apply_outbox_parity_gap -1.0" in response.get_data(as_text=True)


def test_readiness_check_returns_service_unavailable_when_database_is_down(client):
    with patch("app.get_db_connection", side_effect=app_module.pymysql.OperationalError("down")):
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.get_json() == {"status": "not_ready"}


def test_apply_rejects_missing_csrf_token(client):
    with client.session_transaction() as current_session:
        current_session["user_id"] = "loadtest-user"

    response = client.post("/api/apply", json={"item_id": 1})

    assert response.status_code == 400


def test_apply_returns_service_unavailable_when_writer_database_is_down(client):
    headers = csrf_headers(client)
    with client.session_transaction() as current_session:
        current_session["user_id"] = "loadtest-user"

    with patch("app.get_db_connection", side_effect=app_module.pymysql.OperationalError("down")):
        response = client.post("/api/apply", json={"item_id": 1}, headers=headers)

    assert response.status_code == 503
    assert response.get_json()["status"] == "error"


def test_apply_persists_entry_and_outbox_event_in_one_transaction(client):
    headers = csrf_headers(client)
    with client.session_transaction() as current_session:
        current_session["user_id"] = "loadtest-user"
    cursor = FakeCursor(fetchone_result={"id": 7}, lastrowid=99)
    connection = FakeConnection(cursor)

    with patch("app.get_db_connection", return_value=connection) as get_connection:
        response = client.post("/api/apply", json={"item_id": 1}, headers=headers)

    assert response.status_code == 200
    assert response.get_json()["event_type"] == app_module.OUTBOX_EVENT_TYPE
    assert get_connection.call_args.kwargs == {"is_write": True}
    assert connection.commit_count == 1
    assert connection.rollback_count == 0
    assert connection.close_count == 1

    statements = [statement for statement, _ in cursor.executed]
    assert any("INSERT INTO raffle_entries" in statement for statement in statements)
    outbox_statement, outbox_parameters = next(
        (statement, parameters)
        for statement, parameters in cursor.executed
        if "INSERT INTO raffle_outbox_events" in statement
    )
    assert "event_version" in outbox_statement
    event = json.loads(outbox_parameters[-1])
    assert event["event_type"] == "raffle.entry.accepted.v1"
    assert event["event_version"] == 1
    assert event["data"] == {"entry_id": 99, "item_id": 1, "user_id": 7}
    UUID(event["event_id"])


def test_outbox_failure_drill_rolls_back_before_an_orphaned_entry_can_commit(client, monkeypatch):
    headers = csrf_headers(client)
    with client.session_transaction() as current_session:
        current_session["user_id"] = "loadtest-user"
    cursor = FakeCursor(fetchone_result={"id": 7}, lastrowid=99)
    connection = FakeConnection(cursor)
    monkeypatch.setenv("DEPLOYMENT_TIER", "validation")
    monkeypatch.setenv("ALLOW_FAILURE_DRILL", "true")
    monkeypatch.setenv("D2C_OUTBOX_FAILURE_INJECTION", "before_outbox_insert")

    with patch("app.get_db_connection", return_value=connection):
        response = client.post("/api/apply", json={"item_id": 1}, headers=headers)

    assert response.status_code == 503
    assert connection.commit_count == 0
    assert connection.rollback_count == 1
    assert not any("INSERT INTO raffle_outbox_events" in statement for statement, _ in cursor.executed)


def test_legacy_plaintext_password_is_rehashed_after_a_successful_login(client):
    headers = csrf_headers(client)
    cursor = FakeCursor(fetchone_result={"id": 7, "password": "legacy-password"})
    connection = FakeConnection(cursor)

    with patch("app.get_db_connection", return_value=connection):
        response = client.post(
            "/api/login",
            json={"username": "legacy-user", "password": "legacy-password"},
            headers=headers,
        )

    assert response.status_code == 200
    update_parameters = next(
        parameters
        for statement, parameters in cursor.executed
        if statement.startswith("UPDATE users SET password")
    )
    assert update_parameters[1] == 7
    assert update_parameters[0] != "legacy-password"
    assert connection.commit_count == 1


def test_hashed_password_does_not_need_a_migration_write(client):
    headers = csrf_headers(client)
    cursor = FakeCursor(fetchone_result={"id": 7, "password": generate_password_hash("secure-password")})
    connection = FakeConnection(cursor)

    with patch("app.get_db_connection", return_value=connection):
        response = client.post(
            "/api/login",
            json={"username": "secure-user", "password": "secure-password"},
            headers=headers,
        )

    assert response.status_code == 200
    assert connection.commit_count == 0
    assert not any("UPDATE users SET password" in statement for statement, _ in cursor.executed)


def test_login_page_is_available_without_database(client):
    response = client.get("/login")

    assert response.status_code == 200
    assert "로그인" in response.get_data(as_text=True)
    assert "csrf-token" in response.get_data(as_text=True)


def test_signup_page_is_available_without_database(client):
    response = client.get("/signup")

    assert response.status_code == 200
    assert "회원가입" in response.get_data(as_text=True)
    assert "csrf-token" in response.get_data(as_text=True)
