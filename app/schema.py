"""Idempotent schema migrations for the raffle application."""

import re

import pymysql


DATABASE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(50) UNIQUE NOT NULL,
        password VARCHAR(255) NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raffle_items (
        id INT AUTO_INCREMENT PRIMARY KEY,
        title VARCHAR(100) NOT NULL,
        description TEXT,
        end_time DATETIME NOT NULL,
        image_url VARCHAR(255),
        winner_id INT DEFAULT NULL,
        is_drawn BOOLEAN DEFAULT FALSE,
        FOREIGN KEY (winner_id) REFERENCES users(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raffle_entries (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        item_id INT NOT NULL,
        entry_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (item_id) REFERENCES raffle_items(id),
        UNIQUE KEY unique_entry (user_id, item_id),
        KEY idx_raffle_entries_entry_time (entry_time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raffle_outbox_events (
        event_id CHAR(36) PRIMARY KEY,
        aggregate_type VARCHAR(64) NOT NULL,
        aggregate_id BIGINT UNSIGNED NOT NULL,
        event_type VARCHAR(128) NOT NULL,
        event_version SMALLINT UNSIGNED NOT NULL,
        payload JSON NOT NULL,
        created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        published_at DATETIME(6) NULL,
        publish_attempts INT UNSIGNED NOT NULL DEFAULT 0,
        last_error VARCHAR(1024) NULL,
        UNIQUE KEY unique_raffle_entry_event (event_type, aggregate_id),
        KEY idx_raffle_outbox_unpublished (published_at, created_at)
    )
    """,
)

SAMPLE_ITEMS = (
    (
        "나이키 덩크 로우 범고래",
        "국민 신발, 마지막 기회!",
        "2026-04-30 18:00:00",
        "https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?w=500&q=80",
    ),
    (
        "애플 에어팟 맥스 실버",
        "노이즈 캔슬링 끝판왕",
        "2026-05-05 12:00:00",
        "https://images.unsplash.com/photo-1613040809024-b4ef7ba99bc3?w=500&q=80",
    ),
)


def ensure_database_exists(*, host: str, user: str, password: str, database_name: str) -> None:
    """Create the operator-configured database after validating its identifier."""
    if not DATABASE_IDENTIFIER.fullmatch(database_name):
        raise ValueError("DB_NAME must use only letters, numbers, and underscores")
    connection = pymysql.connect(host=host, user=user, password=password, connect_timeout=5)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database_name}`")
        connection.commit()
    finally:
        connection.close()


def apply_schema_migrations(connection, *, seed_sample_data: bool = False) -> None:
    """Apply additive, repeatable DDL. No production rows are seeded by default."""
    with connection.cursor() as cursor:
        for statement in SCHEMA_STATEMENTS:
            cursor.execute(statement)
        cursor.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 'raffle_entries'
              AND index_name = 'idx_raffle_entries_entry_time'
            """
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                "ALTER TABLE raffle_entries ADD INDEX idx_raffle_entries_entry_time (entry_time)"
            )
        if seed_sample_data:
            cursor.execute("SELECT COUNT(*) AS cnt FROM raffle_items")
            if cursor.fetchone()["cnt"] == 0:
                cursor.executemany(
                    """
                    INSERT INTO raffle_items (title, description, end_time, image_url)
                    VALUES (%s, %s, %s, %s)
                    """,
                    SAMPLE_ITEMS,
                )
    connection.commit()
