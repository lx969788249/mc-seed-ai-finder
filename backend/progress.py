from __future__ import annotations

import time
from threading import Lock
from typing import Any


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


def update_progress(request_id: str | None, **fields: Any) -> None:
    if not request_id:
        return
    with _LOCK:
        current = _PROGRESS.get(request_id)
        if not current:
            return
        current.update(fields)
        current["updated_at"] = _now()


def finish_progress(request_id: str | None, message: str = "搜索完成", status: str = "done") -> None:
    if not request_id:
        return
    update_progress(request_id, status=status, stage=status, message=message)


def get_progress(request_id: str) -> dict[str, Any]:
    with _LOCK:
        _clean_locked()
        current = _PROGRESS.get(request_id)
        if not current:
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
