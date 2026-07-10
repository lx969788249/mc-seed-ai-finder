from __future__ import annotations

import json
import time
from threading import Lock
from typing import Any

from .database import db


_LOCK = Lock()
_PROGRESS: dict[str, dict[str, Any]] = {}
_TTL_SECONDS = 600


def _now() -> float:
    return time.time()


def _clean_locked() -> None:
    cutoff = _now() - _TTL_SECONDS
    stale = [key for key, value in _PROGRESS.items() if float(value.get("updated_at", 0)) < cutoff]
    for key in stale:
        _PROGRESS.pop(key, None)


def _persist(request_id: str, payload: dict[str, Any]) -> None:
    try:
        with db() as conn:
            conn.execute(
                """
                UPDATE search_jobs SET progress_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), request_id),
            )
    except Exception:
        # Progress must never make the actual search fail.
        return


def start_progress(request_id: str | None, message: str = "准备搜索") -> None:
    if not request_id:
        return
    with _LOCK:
        _clean_locked()
        _PROGRESS[request_id] = {
            "request_id": request_id,
            "status": "running",
            "stage": "prepare",
            "message": message,
            "radius": None,
            "checked": None,
            "total": None,
            "started_at": _now(),
            "updated_at": _now(),
        }
        _persist(request_id, _PROGRESS[request_id])


def update_progress(request_id: str | None, **fields: Any) -> None:
    if not request_id:
        return
    with _LOCK:
        current = _PROGRESS.get(request_id)
        if not current:
            return
        current.update(fields)
        current["updated_at"] = _now()
        _persist(request_id, current)


def finish_progress(request_id: str | None, message: str = "搜索完成", status: str = "done") -> None:
    if not request_id:
        return
    update_progress(request_id, status=status, stage=status, message=message)


def get_progress(request_id: str) -> dict[str, Any]:
    with _LOCK:
        _clean_locked()
        current = _PROGRESS.get(request_id)
        if not current:
            try:
                with db() as conn:
                    row = conn.execute("SELECT progress_json FROM search_jobs WHERE id=?", (request_id,)).fetchone()
                if row and row["progress_json"]:
                    return json.loads(row["progress_json"])
            except Exception:
                pass
            return {
                "request_id": request_id,
                "status": "missing",
                "stage": "missing",
                "message": "暂无进度",
                "radius": None,
                "checked": None,
                "total": None,
                "started_at": None,
                "updated_at": None,
            }
        return dict(current)
