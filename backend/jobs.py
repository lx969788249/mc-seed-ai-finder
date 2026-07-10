from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import db
from .security import decrypt_secret, encrypt_secret


JobHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


JOB_WORKERS = _env_int("SEARCH_JOB_WORKERS", 1, 1, 8)
JOB_RETENTION_DAYS = _env_int("SEARCH_JOB_RETENTION_DAYS", 30, 1, 365)
JOB_STALE_SECONDS = _env_int("SEARCH_JOB_STALE_SECONDS", 900, 60, 86400)

_WORKER_TASKS: list[asyncio.Task] = []


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def create_job(kind: str, payload: dict[str, Any], user_id: int | None = None, secret: str | None = None) -> dict:
    if kind not in {"chat", "search"}:
        raise ValueError("unsupported job kind")
    job_id = secrets.token_urlsafe(24)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO search_jobs(id, kind, user_id, payload_json, secret_enc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, kind, user_id, _json(payload), encrypt_secret(secret)),
        )
    return {"id": job_id, "status": "queued", "kind": kind}


def _decode_row(row) -> dict[str, Any]:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json") or "{}")
    result["progress"] = json.loads(result.pop("progress_json") or "null")
    result["result"] = json.loads(result.pop("result_json") or "null")
    result["cancel_requested"] = bool(result["cancel_requested"])
    secret_enc = result.pop("secret_enc", None)
    result["secret"] = decrypt_secret(secret_enc) if secret_enc else None
    return result


def get_job(job_id: str, include_payload: bool = False) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM search_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    result = _decode_row(row)
    if not include_payload:
        result.pop("payload", None)
        result.pop("secret", None)
    return result


def list_jobs(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM search_jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, min(100, limit))),
        ).fetchall()
    jobs = []
    for row in rows:
        item = _decode_row(row)
        item.pop("payload", None)
        item.pop("secret", None)
        jobs.append(item)
    return jobs


def cancel_job(job_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT status FROM search_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        if row["status"] == "queued":
            conn.execute(
                """
                UPDATE search_jobs
                SET status='cancelled', cancel_requested=1, completed_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (job_id,),
            )
        elif row["status"] == "running":
            conn.execute(
                "UPDATE search_jobs SET cancel_requested=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_id,),
            )
    return get_job(job_id)


def recover_interrupted_jobs(stale_seconds: int = JOB_STALE_SECONDS) -> int:
    modifier = f"-{max(0, stale_seconds)} seconds"
    with db() as conn:
        cursor = conn.execute(
            """
            UPDATE search_jobs
            SET status='queued', started_at=NULL, updated_at=CURRENT_TIMESTAMP,
                error_detail='服务重启后已重新排队'
            WHERE status='running' AND cancel_requested=0
              AND updated_at <= datetime('now', ?)
            """,
            (modifier,),
        )
        conn.execute(
            """
            UPDATE search_jobs
            SET status='cancelled', completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE status='running' AND cancel_requested=1
              AND updated_at <= datetime('now', ?)
            """,
            (modifier,),
        )
        return int(cursor.rowcount)


def cleanup_jobs() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=JOB_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        cursor = conn.execute(
            "DELETE FROM search_jobs WHERE status IN ('completed','failed','cancelled') AND updated_at < ?",
            (cutoff,),
        )
        return int(cursor.rowcount)


def _claim_next_job() -> dict[str, Any] | None:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM search_jobs WHERE status='queued' AND cancel_requested=0 ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        cursor = conn.execute(
            """
            UPDATE search_jobs
            SET status='running', started_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='queued'
            """,
            (row["id"],),
        )
        if cursor.rowcount != 1:
            return None
    return get_job(row["id"], include_payload=True)


def _finish_job(job_id: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    with db() as conn:
        row = conn.execute("SELECT cancel_requested FROM search_jobs WHERE id=?", (job_id,)).fetchone()
        cancelled = bool(row and row["cancel_requested"])
        status = "cancelled" if cancelled else "failed" if error else "completed"
        conn.execute(
            """
            UPDATE search_jobs
            SET status=?, result_json=?, error_detail=?, completed_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (status, _json(result) if result is not None and not cancelled else None, error, job_id),
        )


async def _worker(handler: JobHandler, worker_index: int) -> None:
    last_recovery = 0.0
    while True:
        job = _claim_next_job()
        if not job:
            if worker_index == 0 and time.monotonic() - last_recovery >= 60:
                recover_interrupted_jobs()
                cleanup_jobs()
                last_recovery = time.monotonic()
            await asyncio.sleep(0.5)
            continue
        try:
            result = await handler(job)
            _finish_job(job["id"], result=result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _finish_job(job["id"], error=f"{type(exc).__name__}: {str(exc)[:1000]}")


def start_job_workers(handler: JobHandler) -> list[asyncio.Task]:
    if any(not task.done() for task in _WORKER_TASKS):
        return _WORKER_TASKS
    recover_interrupted_jobs()
    cleanup_jobs()
    _WORKER_TASKS[:] = [
        asyncio.create_task(_worker(handler, index), name=f"search-job-worker-{index}")
        for index in range(JOB_WORKERS)
    ]
    return _WORKER_TASKS


async def stop_job_workers() -> None:
    tasks = list(_WORKER_TASKS)
    _WORKER_TASKS.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def job_counts() -> dict[str, int]:
    with db() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM search_jobs GROUP BY status").fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}
