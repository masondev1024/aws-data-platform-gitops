"""Entrypoint for the Argo CD PreSync database migration Job."""

import os

from app import DB_NAME, DB_WRITER_HOST, get_db_connection
from schema import apply_schema_migrations, ensure_application_user, ensure_database_exists

DB_ADMIN_USER = os.environ.get("DB_ADMIN_USER", os.environ.get("DB_USER", "admin"))
DB_ADMIN_PASSWORD = os.environ.get("DB_ADMIN_PASSWORD", os.environ.get("DB_PASSWORD"))
DB_APP_USER = os.environ.get("DB_APP_USER", "")
DB_APP_PASSWORD = os.environ.get("DB_APP_PASSWORD", "")


def main() -> None:
    if not DB_WRITER_HOST or not DB_ADMIN_PASSWORD or not DB_APP_USER or not DB_APP_PASSWORD:
        raise RuntimeError(
            "DB_WRITER_HOST, DB_ADMIN_PASSWORD, DB_APP_USER, and DB_APP_PASSWORD "
            "must be configured for migrations"
        )
    seed_sample_data = os.environ.get("SEED_SAMPLE_DATA", "").lower() == "true"
    live_lab_seed = (
        os.environ.get("DEPLOYMENT_TIER") == "validation"
        and os.environ.get("LIVE_LAB_SYNTHETIC_SEED") == "true"
    )
    if seed_sample_data and os.environ.get("DEPLOYMENT_TIER") != "development" and not live_lab_seed:
        raise RuntimeError(
            "SEED_SAMPLE_DATA is allowed only in development or with the explicit live-lab validation gate"
        )

    ensure_database_exists(
        host=DB_WRITER_HOST,
        user=DB_ADMIN_USER,
        password=DB_ADMIN_PASSWORD,
        database_name=DB_NAME,
    )
    connection = get_db_connection(
        is_write=True,
        user=DB_ADMIN_USER,
        password=DB_ADMIN_PASSWORD,
    )
    try:
        apply_schema_migrations(connection, seed_sample_data=seed_sample_data)
        ensure_application_user(
            connection,
            username=DB_APP_USER,
            password=DB_APP_PASSWORD,
            database_name=DB_NAME,
        )
    finally:
        connection.close()
    print("Schema migrations completed successfully")


if __name__ == "__main__":
    main()
