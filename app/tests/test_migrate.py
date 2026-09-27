import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate


@pytest.fixture
def migration_dependencies(monkeypatch):
    monkeypatch.setattr(migrate, "DB_WRITER_HOST", "db.example.internal")
    monkeypatch.setattr(migrate, "DB_ADMIN_USER", "raffle_admin")
    monkeypatch.setattr(migrate, "DB_ADMIN_PASSWORD", "not-a-real-secret")
    monkeypatch.setattr(migrate, "DB_APP_USER", "raffle_app")
    monkeypatch.setattr(migrate, "DB_APP_PASSWORD", "not-a-real-secret")
    connection = Mock()
    connection.close = Mock()
    monkeypatch.setattr(migrate, "ensure_database_exists", Mock())
    monkeypatch.setattr(migrate, "get_db_connection", Mock(return_value=connection))
    monkeypatch.setattr(migrate, "apply_schema_migrations", Mock())
    monkeypatch.setattr(migrate, "ensure_application_user", Mock())
    return connection


def test_synthetic_seed_requires_explicit_live_lab_validation_gate(monkeypatch, migration_dependencies):
    monkeypatch.setenv("DEPLOYMENT_TIER", "validation")
    monkeypatch.setenv("SEED_SAMPLE_DATA", "true")
    monkeypatch.delenv("LIVE_LAB_SYNTHETIC_SEED", raising=False)

    with pytest.raises(RuntimeError, match="explicit live-lab validation gate"):
        migrate.main()

    migrate.apply_schema_migrations.assert_not_called()


def test_validation_seed_passes_only_with_explicit_live_lab_gate(monkeypatch, migration_dependencies):
    monkeypatch.setenv("DEPLOYMENT_TIER", "validation")
    monkeypatch.setenv("SEED_SAMPLE_DATA", "true")
    monkeypatch.setenv("LIVE_LAB_SYNTHETIC_SEED", "true")

    migrate.main()

    migrate.apply_schema_migrations.assert_called_once_with(migration_dependencies, seed_sample_data=True)
    migration_dependencies.close.assert_called_once()


def test_development_can_seed_without_live_lab_gate(monkeypatch, migration_dependencies):
    monkeypatch.setenv("DEPLOYMENT_TIER", "development")
    monkeypatch.setenv("SEED_SAMPLE_DATA", "true")
    monkeypatch.delenv("LIVE_LAB_SYNTHETIC_SEED", raising=False)

    migrate.main()

    assert migrate.apply_schema_migrations.call_args.kwargs["seed_sample_data"] is True
