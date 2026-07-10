from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("MCFINDER_DB", DATA_DIR / "app.sqlite3"))


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                deepseek_api_key_enc TEXT,
                deepseek_base_url TEXT NOT NULL DEFAULT 'https://api.deepseek.com',
                deepseek_model TEXT NOT NULL DEFAULT 'deepseek-v4-flash',
                seed TEXT NOT NULL DEFAULT '0',
                version TEXT NOT NULL DEFAULT '26.2',
                center_x INTEGER NOT NULL DEFAULT 0,
                center_z INTEGER NOT NULL DEFAULT 0,
                search_radius INTEGER NOT NULL DEFAULT 30000000,
                max_results INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS unmet_request_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL,
                cluster_key TEXT NOT NULL,
                query_text TEXT NOT NULL,
                source TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                reason_detail TEXT,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                request_id TEXT,
                planner TEXT,
                plan_json TEXT,
                context_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unmet_event_request
            ON unmet_request_events(request_id, source, reason_code, fingerprint)
            WHERE request_id IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_unmet_event_fingerprint
            ON unmet_request_events(fingerprint, created_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_unmet_event_cluster
            ON unmet_request_events(cluster_key, created_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_unmet_event_created
            ON unmet_request_events(created_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evolution_backlog (
                cluster_key TEXT PRIMARY KEY,
                sample_fingerprint TEXT NOT NULL,
                sample_query TEXT NOT NULL,
                primary_reason TEXT NOT NULL,
                reason_detail TEXT,
                occurrence_count INTEGER NOT NULL,
                feedback_count INTEGER NOT NULL,
                priority_score REAL NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_evolution_backlog_priority
            ON evolution_backlog(status, priority_score DESC, last_seen_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evolution_runs (
                run_key TEXT PRIMARY KEY,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                event_count INTEGER NOT NULL DEFAULT 0,
                backlog_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running',
                report_path TEXT,
                error_detail TEXT
            )
            """
        )
        conn.execute(
            "UPDATE user_settings SET deepseek_model='deepseek-v4-flash' WHERE deepseek_model='deepseek-chat'"
        )
        conn.execute(
            "UPDATE user_settings SET deepseek_model='deepseek-v4-pro' WHERE deepseek_model='deepseek-pro'"
        )
        conn.execute("UPDATE user_settings SET max_results=1 WHERE max_results=5")
        conn.execute("UPDATE user_settings SET search_radius=30000000 WHERE search_radius < 30000000")


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()
