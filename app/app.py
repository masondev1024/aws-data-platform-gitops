"""D2C raffle application with transaction-safe event production."""

from datetime import datetime, timedelta, timezone
import json
import os
import re
from secrets import token_urlsafe
from time import perf_counter
from uuid import uuid4

import pymysql
from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFProtect, generate_csrf
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from werkzeug.security import check_password_hash, generate_password_hash

from db import db_tls_options
from catalog_cache import CatalogBusy, CatalogCache


PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod"})
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
OUTBOX_EVENT_TYPE = "raffle.entry.accepted.v1"
OUTBOX_EVENT_VERSION = 1


def _is_production() -> bool:
    return os.environ.get("FLASK_ENV", "").lower() in PRODUCTION_ENVIRONMENTS


def _trusted_hosts() -> list[str] | None:
    configured_hosts = os.environ.get("TRUSTED_HOSTS", "")
    hosts = [host.strip() for host in configured_hosts.split(",") if host.strip()]
    if _is_production() and not hosts:
        raise RuntimeError("TRUSTED_HOSTS must be configured in production")
    return hosts or None


def _host_matches(host: str, pattern: str) -> bool:
    """Match exact DNS hosts or an explicit left-most wildcard suffix."""
    normalized_host = host.partition(":")[0].lower().rstrip(".")
    normalized_pattern = pattern.lower().rstrip(".")
    if normalized_pattern.startswith("*."):
        suffix = normalized_pattern[1:]
        return normalized_host.endswith(suffix) and normalized_host != suffix[1:]
    return normalized_host == normalized_pattern


app = Flask(__name__)
configured_secret_key = os.environ.get("SECRET_KEY")
if _is_production() and not configured_secret_key:
    raise RuntimeError("SECRET_KEY must be configured in production")

app.config.from_mapping(
    SECRET_KEY=configured_secret_key or token_urlsafe(32),
    MAX_CONTENT_LENGTH=16 * 1024,
    MAX_FORM_MEMORY_SIZE=64 * 1024,
    MAX_FORM_PARTS=20,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_is_production(),
    # Health checks come from Pod/ALB IPs rather than the public ALB DNS name.
    # Enforce allowed application hosts below, while keeping these three
    # operator-only endpoints reachable for readiness and internal scraping.
    TRUSTED_HOSTS=None,
    LIVE_LAB_TRUSTED_HOSTS=_trusted_hosts(),
    CATALOG_CACHE_TTL_SECONDS=float(os.environ.get("CATALOG_CACHE_TTL_SECONDS", "0")),
)
csrf = CSRFProtect(app)

DB_WRITER_HOST = os.environ.get("DB_WRITER_HOST")
DB_READER_HOST = os.environ.get("DB_READER_HOST")
DB_NAME = os.environ.get("DB_NAME", "raffle_db")
DB_USER = os.environ.get("DB_APP_USER", os.environ.get("DB_USER", "admin"))
DB_PASSWORD = os.environ.get("DB_APP_PASSWORD", os.environ.get("DB_PASSWORD"))

HTTP_REQUESTS = Counter(
    "raffle_http_requests",
    "Total HTTP requests handled by the raffle application.",
    ("method", "route", "status"),
)
HTTP_REQUEST_DURATION = Histogram(
    "raffle_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "route"),
)
RAFFLE_APPLY_REQUESTS = Counter(
    "raffle_apply_requests",
    "Raffle application outcomes.",
    ("result",),
)
RAFFLE_OUTBOX_EVENTS = Counter(
    "raffle_outbox_events",
    "Transactional outbox event outcomes.",
    ("result",),
)
# Prometheus rate()/increase() cannot measure the first increment of a counter
# series unless an earlier zero sample exists. Seed the bounded canary-gate
# series so the first injected or real apply failure is observable immediately.
HTTP_REQUESTS.labels("POST", "/api/apply", "503").inc(0)
for apply_result in (
    "success",
    "unauthenticated",
    "integrity_protection_rejected",
    "duplicate",
    "database_error",
):
    RAFFLE_APPLY_REQUESTS.labels(apply_result).inc(0)
DB_READINESS = Gauge(
    "raffle_db_readiness",
    "Whether the configured read database passed the latest readiness check.",
)
RAFFLE_APPLY_OUTBOX_PARITY_GAP = Gauge(
    "raffle_apply_outbox_parity_gap",
    "Accepted raffle entries created in the last window without a matching transactional outbox event; -1 means the database check failed.",
)
DB_CONNECT_DURATION = Histogram(
    "raffle_db_connect_duration_seconds", "Database connection and TLS setup time.", ("role",),
)
CATALOG_LOAD_DURATION = Histogram(
    "raffle_catalog_load_duration_seconds", "Catalogue database read including connection setup.",
)
CATALOG_CACHE_EVENTS = Counter(
    "raffle_catalog_cache_events", "Public catalogue snapshot activity.", ("result",),
)
catalog_cache = CatalogCache(on_event=lambda result: CATALOG_CACHE_EVENTS.labels(result).inc())


class OutboxTransactionFailure(Exception):
    """Raised only by an explicitly enabled validation-only failure drill."""


def _metric_route() -> str:
    """Return a bounded route label instead of a user-controlled URL path."""
    if request.url_rule is not None:
        return request.url_rule.rule
    return "unmatched"


@app.before_request
def start_request_timer() -> None:
    g.request_started_at = perf_counter()


@app.before_request
def enforce_trusted_host() -> None:
    allowed_hosts = app.config.get("LIVE_LAB_TRUSTED_HOSTS") or []
    if not allowed_hosts or request.path in {"/healthz", "/readyz", "/metrics"}:
        return
    if not any(_host_matches(request.host, pattern) for pattern in allowed_hosts):
        abort(400)


@app.after_request
def record_request_metrics(response):
    # Scraping /metrics must not create an unbounded self-referential counter.
    if request.endpoint != "metrics":
        route = _metric_route()
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        HTTP_REQUEST_DURATION.labels(request.method, route).observe(
            perf_counter() - g.get("request_started_at", perf_counter())
        )
    return response


@app.after_request
def add_security_headers(response):
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' https://images.unsplash.com data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'self'; form-action 'self'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response


def get_db_connection(
    is_write: bool = False,
    *,
    user: str | None = None,
    password: str | None = None,
    connect_timeout: int = 5,
    read_timeout: int = 10,
    write_timeout: int = 10,
):
    """Route writes to the writer and reads to the replica endpoint."""
    host = DB_WRITER_HOST if is_write else DB_READER_HOST
    with DB_CONNECT_DURATION.labels("writer" if is_write else "reader").time():
        return pymysql.connect(
            host=host,
            user=user or DB_USER,
            password=password if password is not None else DB_PASSWORD,
            database=DB_NAME,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            cursorclass=pymysql.cursors.DictCursor,
            **db_tls_options(),
        )


def _json_object() -> dict | None:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _validated_credentials(data: dict | None) -> tuple[str | None, str | None, str | None]:
    if data is None:
        return None, None, "JSON object body is required"

    username = data.get("username")
    password = data.get("password")
    if not isinstance(username, str) or not USERNAME_PATTERN.fullmatch(username):
        return None, None, "username must be 3-50 characters using letters, numbers, ., _, or -"
    if not isinstance(password, str) or len(password) < 12 or len(password) > 128:
        return None, None, "password must be between 12 and 128 characters"
    return username, password, None


def _rollback_quietly(connection) -> None:
    if connection is None:
        return
    try:
        connection.rollback()
    except pymysql.MySQLError:
        app.logger.exception("Database rollback failed")


def _password_matches(stored_password: str, provided_password: str) -> bool:
    """Verify only Werkzeug password hashes; malformed and legacy plaintext values fail closed."""
    if not isinstance(stored_password, str) or not isinstance(provided_password, str):
        return False
    try:
        return check_password_hash(stored_password, provided_password)
    except (TypeError, ValueError):
        return False


def _is_duplicate_key_error(error: pymysql.err.IntegrityError) -> bool:
    return bool(error.args and error.args[0] == 1062)


def _is_validation_outbox_failure_enabled() -> bool:
    return (
        os.environ.get("DEPLOYMENT_TIER") == "validation"
        and os.environ.get("ALLOW_FAILURE_DRILL", "").lower() == "true"
        and os.environ.get("D2C_OUTBOX_FAILURE_INJECTION") == "before_outbox_insert"
    )


def build_raffle_entry_event(*, entry_id: int, user_id: int, item_id: int) -> dict:
    """Build the versioned contract persisted in the same transaction as the entry."""
    return {
        "event_id": str(uuid4()),
        "event_type": OUTBOX_EVENT_TYPE,
        "event_version": OUTBOX_EVENT_VERSION,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": {
            "entry_id": entry_id,
            "user_id": user_id,
            "item_id": item_id,
        },
    }


def refresh_apply_outbox_parity_gap() -> None:
    """Measure the invariant from database state, not from paired app counters."""
    connection = None
    try:
        connection = get_db_connection(
            is_write=True,
            connect_timeout=2,
            read_timeout=3,
            write_timeout=3,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS missing_events
                FROM raffle_entries AS entries
                LEFT JOIN raffle_outbox_events AS events
                  ON events.aggregate_type = 'raffle_entry'
                 AND events.aggregate_id = entries.id
                 AND events.event_type = %s
                WHERE entries.entry_time >= UTC_TIMESTAMP() - INTERVAL 10 MINUTE
                  AND events.event_id IS NULL
                """,
                (OUTBOX_EVENT_TYPE,),
            )
            result = cursor.fetchone() or {}
        RAFFLE_APPLY_OUTBOX_PARITY_GAP.set(int(result.get("missing_events", -1)))
    except (pymysql.MySQLError, OSError, TypeError, ValueError) as error:
        # A missing measurement must fail closed in the canary gate instead of
        # looking like a healthy zero.
        RAFFLE_APPLY_OUTBOX_PARITY_GAP.set(-1)
        app.logger.warning("Transactional outbox parity check failed: %s", error)
    finally:
        if connection is not None:
            connection.close()


@app.route("/healthz")
def healthz():
    """Process health endpoint used by Kubernetes probes."""
    return jsonify({"status": "ok"})


@app.route("/readyz")
def readyz():
    """Dependency readiness endpoint used before sending user traffic."""
    connection = None
    DB_READINESS.set(0)
    try:
        # A read replica can be healthy while writes to the primary are not.
        # Readiness gates user traffic on the writer, not only the replica.
        connection = get_db_connection(is_write=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except (pymysql.MySQLError, OSError) as error:
        app.logger.warning("Database readiness check failed: %s", error)
        return jsonify({"status": "not_ready"}), 503
    finally:
        if connection is not None:
            connection.close()
    DB_READINESS.set(1)
    return jsonify({"status": "ready"})


@app.route("/metrics")
def metrics():
    """Expose Prometheus metrics for an internal scraper."""
    if DB_WRITER_HOST:
        refresh_apply_outbox_parity_gap()
    elif _is_production():
        # A production deployment without a writer endpoint cannot prove the
        # invariant. Keep the canary gate fail-closed instead of exposing the
        # Gauge's initial zero as a false healthy result.
        RAFFLE_APPLY_OUTBOX_PARITY_GAP.set(-1)
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)


@CATALOG_LOAD_DURATION.time()
def load_catalog_items():
    """Load public rows; return request-owned data, never a session or response."""
    connection = get_db_connection(is_write=False)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM raffle_items ORDER BY end_time ASC")
            items = cursor.fetchall()
    finally:
        connection.close()

    for item in items:
        if isinstance(item["end_time"], datetime):
            item["end_time"] = item["end_time"].strftime("%Y-%m-%dT%H:%M:%S")
    return items


@app.route("/")
def index():
    try:
        items = catalog_cache.get(
            load_catalog_items, ttl=app.config["CATALOG_CACHE_TTL_SECONDS"],
        )
    except (CatalogBusy, pymysql.MySQLError, OSError) as error:
        app.logger.warning("Catalogue temporarily unavailable: %s", type(error).__name__)
        return jsonify({"status": "unavailable"}), 503
    response = app.make_response(render_template(
        "index.html", items=items, is_logged_in="user_id" in session,
    ))
    # CSRF and authentication are rendered for each request, not shared by the cache.
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route("/login")
def login_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/signup")
def signup_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template("signup.html")


@app.route("/mypage")
def mypage():
    if "user_id" not in session:
        return redirect(url_for("login_page"))

    current_username = session["user_id"]
    connection = get_db_connection(is_write=False)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.title, r.end_time, e.entry_time, r.winner_id,
                       u_winner.username AS winner_name, u_me.id AS my_id
                FROM raffle_entries e
                JOIN users u_me ON e.user_id = u_me.id
                JOIN raffle_items r ON e.item_id = r.id
                LEFT JOIN users u_winner ON r.winner_id = u_winner.id
                WHERE u_me.username = %s
                ORDER BY e.entry_time DESC
                """,
                (current_username,),
            )
            history_data = cursor.fetchall()
    finally:
        connection.close()

    my_history = []
    now = datetime.now()
    for row in history_data:
        if row["winner_id"] is None:
            status = "당첨 대기중 ⏳" if now < row["end_time"] else "추첨 진행 중... ⚙️"
        elif row["winner_id"] == row["my_id"]:
            status = "축하합니다! 당첨되었습니다! 🎉"
        else:
            status = "다음 기회에..."
        my_history.append(
            {
                "title": row["title"],
                "apply_date": row["entry_time"].strftime("%Y-%m-%d %H:%M"),
                "status": status,
            }
        )
    return render_template("mypage.html", user_id=current_username, history=my_history)


@app.route("/api/signup", methods=["POST"])
def api_signup():
    username, password, validation_error = _validated_credentials(_json_object())
    if validation_error:
        return jsonify({"status": "error", "message": validation_error}), 400

    connection = None
    try:
        connection = get_db_connection(is_write=True)
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (username, password) VALUES (%s, %s)",
                (username, generate_password_hash(password)),
            )
        connection.commit()
    except pymysql.err.IntegrityError as error:
        _rollback_quietly(connection)
        if _is_duplicate_key_error(error):
            return jsonify({"status": "error", "message": "이미 존재하는 아이디입니다."}), 400
        app.logger.exception("User signup failed because of an integrity error")
        return jsonify({"status": "error", "message": "잠시 후 다시 시도해주세요."}), 503
    except pymysql.MySQLError:
        _rollback_quietly(connection)
        app.logger.exception("User signup failed because of a database error")
        return jsonify({"status": "error", "message": "잠시 후 다시 시도해주세요."}), 503
    finally:
        if connection is not None:
            connection.close()
    return jsonify({"status": "success", "message": "회원가입 완료! 로그인 페이지로 이동합니다."})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = _json_object()
    if data is None or not isinstance(data.get("username"), str) or not isinstance(data.get("password"), str):
        return jsonify({"status": "error", "message": "아이디와 비밀번호를 확인해주세요."}), 400

    username = data["username"]
    password = data["password"]
    connection = None
    try:
        # Authentication uses the writer to avoid a read-after-write replica lag.
        connection = get_db_connection(is_write=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, password FROM users WHERE username = %s", (username,))
            user = cursor.fetchone()

            if not user:
                return jsonify({"status": "error", "message": "아이디와 비밀번호를 확인해주세요."}), 401
            if not _password_matches(user["password"], password):
                return jsonify({"status": "error", "message": "아이디와 비밀번호를 확인해주세요."}), 401
    except pymysql.MySQLError:
        _rollback_quietly(connection)
        app.logger.exception("User login failed because of a database error")
        return jsonify({"status": "error", "message": "잠시 후 다시 시도해주세요."}), 503
    finally:
        if connection is not None:
            connection.close()

    session.clear()
    session.permanent = True
    session["user_id"] = username
    # Clearing the session invalidates the pre-login CSRF token. Return a token
    # bound to the authenticated session so clients can safely continue.
    return jsonify({"status": "success", "csrf_token": generate_csrf()})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/api/apply", methods=["POST"])
def api_apply():
    if "user_id" not in session:
        RAFFLE_APPLY_REQUESTS.labels("unauthenticated").inc()
        return jsonify({"status": "error", "message": "login_required"}), 401

    data = _json_object()
    item_id = data.get("item_id") if data else None
    if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0:
        return jsonify({"status": "error", "message": "valid item_id is required"}), 400

    connection = None
    try:
        connection = get_db_connection(is_write=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE username = %s", (session["user_id"],))
            user = cursor.fetchone()
            if not user:
                session.clear()
                RAFFLE_APPLY_REQUESTS.labels("unauthenticated").inc()
                return jsonify({"status": "error", "message": "login_required"}), 401

            cursor.execute(
                "INSERT INTO raffle_entries (user_id, item_id) VALUES (%s, %s)",
                (user["id"], item_id),
            )
            event = build_raffle_entry_event(
                entry_id=cursor.lastrowid,
                user_id=user["id"],
                item_id=item_id,
            )
            if _is_validation_outbox_failure_enabled():
                raise OutboxTransactionFailure("validation-only outbox failure drill")
            cursor.execute(
                """
                INSERT INTO raffle_outbox_events (
                    event_id, aggregate_type, aggregate_id, event_type, event_version, payload
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    event["event_id"],
                    "raffle_entry",
                    event["data"]["entry_id"],
                    event["event_type"],
                    event["event_version"],
                    json.dumps(event, separators=(",", ":"), sort_keys=True),
                ),
            )
        connection.commit()
    except OutboxTransactionFailure:
        _rollback_quietly(connection)
        RAFFLE_APPLY_REQUESTS.labels("integrity_protection_rejected").inc()
        RAFFLE_OUTBOX_EVENTS.labels("transaction_rolled_back").inc()
        app.logger.warning("Validation-only outbox failure drill rejected a raffle application")
        return jsonify({"status": "error", "message": "잠시 후 다시 시도해주세요."}), 503
    except pymysql.err.IntegrityError as error:
        _rollback_quietly(connection)
        if _is_duplicate_key_error(error):
            RAFFLE_APPLY_REQUESTS.labels("duplicate").inc()
            return jsonify({"status": "error", "message": "이미 응모하신 상품입니다!"}), 400
        RAFFLE_APPLY_REQUESTS.labels("database_error").inc()
        app.logger.exception("Raffle application failed because of an integrity error")
        return jsonify({"status": "error", "message": "잠시 후 다시 시도해주세요."}), 503
    except pymysql.MySQLError:
        _rollback_quietly(connection)
        RAFFLE_APPLY_REQUESTS.labels("database_error").inc()
        app.logger.exception("Raffle application failed because of a database error")
        return jsonify({"status": "error", "message": "잠시 후 다시 시도해주세요."}), 503
    finally:
        if connection is not None:
            connection.close()

    RAFFLE_APPLY_REQUESTS.labels("success").inc()
    RAFFLE_OUTBOX_EVENTS.labels("persisted").inc()
    return jsonify(
        {
            "status": "success",
            "message": "성공적으로 응모되었습니다! 마이페이지에서 확인하세요.",
            "event_type": OUTBOX_EVENT_TYPE,
        }
    )


if __name__ == "__main__":
    if _is_production():
        raise RuntimeError("Use gunicorn to run the application in production")
    app.run(host="0.0.0.0", port=8080, debug=os.environ.get("FLASK_DEBUG", "").lower() == "true")
