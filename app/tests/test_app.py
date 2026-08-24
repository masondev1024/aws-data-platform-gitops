import sys
from pathlib import Path

import pytest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module

app = app_module.app


@pytest.fixture()
def client():
    app.config.update(TESTING=True, SECRET_KEY="test-secret")
    with app.test_client() as test_client:
        yield test_client


def test_health_check_does_not_require_database(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_metrics_endpoint_exposes_bounded_http_metrics(client):
    client.get("/healthz")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.content_type.startswith("text/plain")
    body = response.get_data(as_text=True)
    assert "raffle_http_requests_total" in body
    assert 'route="/healthz"' in body
    assert "raffle_http_request_duration_seconds" in body


def test_readiness_check_returns_service_unavailable_when_database_is_down(client):
    with patch("app.get_db_connection", side_effect=app_module.pymysql.OperationalError("down")):
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.get_json() == {"status": "not_ready"}


def test_apply_returns_service_unavailable_when_writer_database_is_down(client):
    with client.session_transaction() as session:
        session["user_id"] = "loadtest-user"

    with patch("app.get_db_connection", side_effect=app_module.pymysql.OperationalError("down")):
        response = client.post("/api/apply", json={"item_id": 1})

    assert response.status_code == 503
    assert response.get_json()["status"] == "error"


def test_login_page_is_available_without_database(client):
    response = client.get("/login")

    assert response.status_code == 200
    assert "로그인" in response.get_data(as_text=True)


def test_signup_page_is_available_without_database(client):
    response = client.get("/signup")

    assert response.status_code == 200
    assert "회원가입" in response.get_data(as_text=True)
