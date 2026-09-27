import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import db_tls_options


def test_local_database_connection_keeps_development_flexible(monkeypatch):
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.delenv("DB_REQUIRE_TLS", raising=False)
    monkeypatch.delenv("DB_SSL_CA", raising=False)

    assert db_tls_options() == {}


def test_production_requires_an_existing_ca_bundle(monkeypatch, tmp_path):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.delenv("DB_SSL_CA", raising=False)

    with pytest.raises(RuntimeError, match="DB_SSL_CA"):
        db_tls_options()

    ca_bundle = tmp_path / "global-bundle.pem"
    ca_bundle.write_text("synthetic test certificate bundle", encoding="utf-8")
    monkeypatch.setenv("DB_SSL_CA", str(ca_bundle))

    assert db_tls_options() == {
        "ssl_ca": str(ca_bundle),
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
    }


def test_explicit_tls_requirement_applies_outside_production(monkeypatch, tmp_path):
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("DB_REQUIRE_TLS", "true")
    ca_bundle = tmp_path / "global-bundle.pem"
    ca_bundle.write_text("synthetic test certificate bundle", encoding="utf-8")
    monkeypatch.setenv("DB_SSL_CA", str(ca_bundle))

    options = db_tls_options()

    assert options["ssl_verify_cert"] is True
    assert options["ssl_verify_identity"] is True
