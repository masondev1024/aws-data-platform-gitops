import json
import re
import subprocess
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


def test_catalogue_cache_does_not_share_csrf_tokens_or_authentication(monkeypatch):
    monkeypatch.setitem(app.config, "CATALOG_CACHE_TTL_SECONDS", 1.0)
    monkeypatch.setattr(app_module, "catalog_cache", app_module.CatalogCache())
    calls = []
    monkeypatch.setattr(app_module, "load_catalog_items", lambda: calls.append(True) or [])
    app.config.update(TESTING=True, SECRET_KEY="test-secret", WTF_CSRF_ENABLED=True)
    anonymous = app.test_client()
    authenticated = app.test_client()
    with authenticated.session_transaction() as session:
        session["user_id"] = "member"
    first = anonymous.get("/")
    second = authenticated.get("/")
    token = r'<meta name="csrf-token" content="([^"]+)">'
    assert re.search(token, first.text)[1] != re.search(token, second.text)[1]
    assert "MYPAGE" not in first.text
    assert "MYPAGE" in second.text
    assert first.headers["Cache-Control"] == "private, no-store"
    assert len(calls) == 1


def test_catalogue_database_failure_returns_unavailable(client, monkeypatch):
    monkeypatch.setitem(app.config, "CATALOG_CACHE_TTL_SECONDS", 0)
    def fail():
        raise app_module.pymysql.OperationalError("database unavailable")
    monkeypatch.setattr(app_module, "load_catalog_items", fail)
    response = client.get("/")
    assert response.status_code == 503
    assert "database unavailable" not in response.text


def test_health_check_is_reachable_with_load_balancer_probe_host(client, monkeypatch):
    monkeypatch.setitem(app.config, "LIVE_LAB_TRUSTED_HOSTS", ["*.ap-northeast-2.elb.amazonaws.com"])
    response = client.get("/healthz", headers={"Host": "10.72.1.20:8080"})
    assert response.status_code == 200


def test_trusted_host_allows_only_the_configured_regional_load_balancer(client, monkeypatch):
    monkeypatch.setitem(app.config, "LIVE_LAB_TRUSTED_HOSTS", ["*.ap-northeast-2.elb.amazonaws.com"])
    allowed = client.get("/signup", headers={"Host": "lab-123.ap-northeast-2.elb.amazonaws.com"})
    wrong_region_order = client.get("/signup", headers={"Host": "lab-123.elb.ap-northeast-2.amazonaws.com"})
    denied = client.get("/signup", headers={"Host": "attacker.example.net"})
    assert allowed.status_code == 200
    assert wrong_region_order.status_code == 400
    assert denied.status_code == 400


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


def test_new_process_preinitializes_canary_failure_and_apply_counter_series():
    code = (
        "import sys; sys.path.insert(0, 'app'); import app; "
        "from prometheus_client import generate_latest; "
        "print(generate_latest().decode())"
    )
    output = subprocess.check_output(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
    )
    metrics = set(output.splitlines())

    assert 'raffle_http_requests_total{method="POST",route="/api/apply",status="503"} 0.0' in metrics
    assert 'raffle_apply_requests_total{result="success"} 0.0' in metrics
    assert 'raffle_apply_requests_total{result="integrity_protection_rejected"} 0.0' in metrics


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
    with patch("app.get_db_connection", side_effect=app_module.pymysql.OperationalError("down")) as connect:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.get_json() == {"status": "not_ready"}
    assert connect.call_args.kwargs == {"is_write": True}


def test_readiness_requires_writer_endpoint_not_only_reader(client):
    cursor = FakeCursor(fetchone_result={"ready": 1})
    connection = FakeConnection(cursor)

    with patch("app.get_db_connection", return_value=connection) as connect:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert connect.call_args.kwargs == {"is_write": True}
    assert connection.close_count == 1


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


@pytest.mark.parametrize("stored_password", ["legacy-password", "not-a-valid-werkzeug-hash"])
def test_plaintext_or_invalid_password_hash_is_rejected_without_migration_write(client, stored_password):
    headers = csrf_headers(client)
    cursor = FakeCursor(fetchone_result={"id": 7, "password": stored_password})
    connection = FakeConnection(cursor)

    with patch("app.get_db_connection", return_value=connection):
        response = client.post(
            "/api/login",
            json={"username": "legacy-user", "password": "legacy-password"},
            headers=headers,
        )

    assert response.status_code == 401
    assert connection.commit_count == 0
    assert not any("UPDATE users SET password" in statement for statement, _ in cursor.executed)


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


def test_login_returns_fresh_csrf_token_for_cleared_session(client, monkeypatch):
    headers = csrf_headers(client)
    cursor = FakeCursor(fetchone_result={"id": 7, "password": generate_password_hash("test-password")})
    connection = FakeConnection(cursor)
    monkeypatch.setattr(app_module, "get_db_connection", lambda **kwargs: connection)

    response = client.post(
        "/api/login",
        json={"username": "member01", "password": "test-password"},
        headers=headers,
    )

    assert response.status_code == 200
    fresh_token = response.get_json()["csrf_token"]
    assert fresh_token
    apply_response = client.post(
        "/api/apply",
        json={"item_id": 1},
        headers={"X-CSRFToken": fresh_token},
    )
    assert apply_response.status_code == 200
