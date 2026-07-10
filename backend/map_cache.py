from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .database import DATA_DIR


CACHE_DIR = Path(os.getenv("MCFINDER_MAP_CACHE_DIR", DATA_DIR / "map-cache"))
CACHE_VERSION = os.getenv("MCFINDER_MAP_CACHE_VERSION", "surface-v2")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


CACHE_MAX_AGE_DAYS = _env_int("MCFINDER_MAP_CACHE_MAX_AGE_DAYS", 30, 1, 365)
CACHE_MAX_BYTES = _env_int("MCFINDER_MAP_CACHE_MAX_BYTES", 2 * 1024**3, 64 * 1024**2, 64 * 1024**3)

_KEY_LOCKS = tuple(threading.Lock() for _ in range(256))
_STATS_LOCK = threading.Lock()
_STATS = {"hits": 0, "misses": 0, "writes": 0, "errors": 0}
_WRITE_COUNT = 0


def _cache_key(params: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"cache_version": CACHE_VERSION, **params},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _path(key: str) -> Path:
    return CACHE_DIR / key[:2] / f"{key}.json.gz"


def _key_lock(key: str) -> threading.Lock:
    return _KEY_LOCKS[int(key[:2], 16)]


def _increment(name: str) -> None:
    with _STATS_LOCK:
        _STATS[name] += 1


def _read(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    max_age = CACHE_MAX_AGE_DAYS * 86400
    if time.time() - path.stat().st_mtime > max_age:
        path.unlink(missing_ok=True)
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or "map" not in payload:
            raise ValueError("invalid map cache payload")
        return payload
    except Exception:
        _increment("errors")
        path.unlink(missing_ok=True)
        return None


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=4) as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _trim() -> None:
    files = []
    total = 0
    for path in CACHE_DIR.glob("*/*.json.gz"):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        files.append((stat.st_mtime, stat.st_size, path))
        total += stat.st_size
    if total <= CACHE_MAX_BYTES:
        return
    for _mtime, size, path in sorted(files):
        path.unlink(missing_ok=True)
        total -= size
        if total <= CACHE_MAX_BYTES:
            break


def get_or_create(
    params: dict[str, Any],
    builder: Callable[[], tuple[dict[str, Any], list[str]]],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    global _WRITE_COUNT
    key = _cache_key(params)
    path = _path(key)
    payload = _read(path)
    if payload is not None:
        _increment("hits")
        return payload["map"], payload.get("warnings") or [], {"hit": True, "key": key[:16]}

    with _key_lock(key):
        payload = _read(path)
        if payload is not None:
            _increment("hits")
            return payload["map"], payload.get("warnings") or [], {"hit": True, "key": key[:16]}
        _increment("misses")
        data, warnings = builder()
        if data.get("ok"):
            try:
                _write(path, {"map": data, "warnings": warnings, "cached_at": int(time.time())})
                _increment("writes")
                _WRITE_COUNT += 1
                if _WRITE_COUNT % 100 == 0:
                    _trim()
            except Exception:
                _increment("errors")
        return data, warnings, {"hit": False, "key": key[:16]}


def cache_stats() -> dict[str, Any]:
    with _STATS_LOCK:
        stats = dict(_STATS)
    files = 0
    bytes_used = 0
    if CACHE_DIR.exists():
        for path in CACHE_DIR.glob("*/*.json.gz"):
            try:
                bytes_used += path.stat().st_size
                files += 1
            except FileNotFoundError:
                continue
    return {**stats, "files": files, "bytes": bytes_used, "version": CACHE_VERSION}
