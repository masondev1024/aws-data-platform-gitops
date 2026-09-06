"""Entrypoint for the Argo CD PreSync database migration Job."""

import os

from app import DB_NAME, DB_PASSWORD, DB_USER, DB_WRITER_HOST, get_db_connection
from schema import apply_schema_migrations, ensure_database_exists


def main() -> None:
    if not DB_WRITER_HOST or not DB_PASSWORD:
        raise RuntimeError("DB_WRITER_HOST and DB_PASSWORD must be configured for migrations")
    seed_sample_data = os.environ.get("SEED_SAMPLE_DATA", "").lower() == "true"
    if seed_sample_data and os.environ.get("DEPLOYMENT_TIER") != "development":
        raise RuntimeError("SEED_SAMPLE_DATA is allowed only in the development tier")

    ensure_database_exists(
        host=DB_WRITER_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database_name=DB_NAME,
    )
    connection = get_db_connection(is_write=True)
    try:
        apply_schema_migrations(connection, seed_sample_data=seed_sample_data)
    finally:
        connection.close()
    print("Schema migrations completed successfully")


if __name__ == "__main__":
    main()
