"""Shared database connection security settings."""

import os
from pathlib import Path


def db_tls_options() -> dict[str, object]:
    """Require RDS certificate and hostname verification in production."""
    production = os.environ.get("FLASK_ENV", "").lower() in {"production", "prod"}
    explicitly_required = os.environ.get("DB_REQUIRE_TLS", "").lower() == "true"
    if not production and not explicitly_required:
        return {}

    ca_path = os.environ.get("DB_SSL_CA", "")
    if not ca_path or not Path(ca_path).is_file():
        raise RuntimeError("DB_SSL_CA must point to the mounted RDS CA bundle when TLS is required")

    return {
        "ssl_ca": ca_path,
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
    }
