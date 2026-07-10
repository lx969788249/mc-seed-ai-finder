from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .database import DATA_DIR, db, init_db


EVOLUTION_DIR = Path(os.getenv("MCFINDER_EVOLUTION_DIR", DATA_DIR / "evolution"))
EVOLUTION_NIGHT_HOUR = max(0, min(23, int(os.getenv("EVOLUTION_NIGHT_HOUR", "3"))))
EVOLUTION_NIGHT_MINUTE = max(0, min(59, int(os.getenv("EVOLUTION_NIGHT_MINUTE", "30"))))
EVOLUTION_RETENTION_DAYS = max(7, int(os.getenv("EVOLUTION_RETENTION_DAYS", "90")))
EVOLUTION_REPORT_LIMIT = max(10, min(500, int(os.getenv("EVOLUTION_REPORT_LIMIT", "100"))))
EVOLUTION_MAX_EVENTS_PER_DAY = max(100, int(os.getenv("EVOLUTION_MAX_EVENTS_PER_DAY", "5000")))
EVOLUTION_MAX_CLUSTER_EVENTS_PER_DAY = max(
    10,
    int(os.getenv("EVOLUTION_MAX_CLUSTER_EVENTS_PER_DAY", "100")),
)

_ACTIVITY_LOCK = threading.Lock()
_ACTIVE_SEARCHES = 0
_SCHEDULER_TASK: asyncio.Task | None = None

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b", re.I),
    re.compile(r"\b(?:bearer|api[_ -]?key)\s*[:=]?\s*[A-Za-z0-9._-]{10,}\b", re.I),
)
_COORD_PATTERN = re.compile(r"-?\d{1,8}\s*[,，]\s*-?\d{1,8}")
_SPACE_PATTERN = re.compile(r"\s+")
_REASON_WEIGHTS = {
    "unsupported_capability": 10,
    "user_report": 8,
    "constraints_unsatisfied": 6,
    "no_verified_result": 5,
    "execution_error": 4,
}


def redact_text(value: str | None, limit: int = 3000) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:limit]


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if any(part in lowered for part in ("api_key", "token", "password", "authorization", "cookie", "secret")):
                continue
            clean[key] = _sanitize_value(raw_value)
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value, 3000)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact_text(str(value), 1000)


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(_sanitize_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_query(query: str) -> str:
    normalized = unicodedata.normalize("NFKC", redact_text(query)).lower().strip()
    normalized = _COORD_PATTERN.sub("<coord>", normalized)
    return _SPACE_PATTERN.sub(" ", normalized)


def _exact_fingerprint(query: str) -> str:
    return hashlib.sha256(normalize_query(query).encode("utf-8")).hexdigest()


def _constraint_shape(plan: dict[str, Any]) -> list[str]:
    shape: list[str] = []
    for field in (
        "adjacency",
        "relative_layout",
        "exclude_biomes",
        "exclude_targets",
        "count_constraints",
        "biome_area_constraints",
    ):
        values = plan.get(field) or []
        if values:
            shape.append(f"{field}:{len(values)}")
    if plan.get("area"):
        shape.append("area")
    if plan.get("verify_point"):
        shape.append("verify_point")
    return shape


def _cluster_key(query: str, reason_code: str, plan: dict[str, Any] | None) -> str:
    if not plan or reason_code == "execution_error":
        source = f"query:{_exact_fingerprint(query)}"
    else:
        targets = sorted(
            f"{target.get('kind', '')}:{target.get('id', '')}"
            for target in (plan.get("targets") or [])
            if isinstance(target, dict)
        )
        signature = {
            "objective": plan.get("objective") or "unknown",
            "targets": targets,
            "shape": _constraint_shape(plan),
            "unsupported_reason": normalize_query(str(plan.get("unsupported_reason") or "")),
        }
        source = json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def search_activity_started() -> None:
    global _ACTIVE_SEARCHES
    with _ACTIVITY_LOCK:
        _ACTIVE_SEARCHES += 1


def search_activity_finished() -> None:
    global _ACTIVE_SEARCHES
    with _ACTIVITY_LOCK:
        _ACTIVE_SEARCHES = max(0, _ACTIVE_SEARCHES - 1)


def active_search_count() -> int:
    with _ACTIVITY_LOCK:
        return _ACTIVE_SEARCHES


def record_unmet_request(
    query: str,
    *,
    source: str,
    reason_code: str,
    reason_detail: str | None = None,
    user_id: int | None = None,
    request_id: str | None = None,
    planner: str | None = None,
    plan: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> bool:
    query_text = redact_text(query).strip()
    if not query_text:
        return False
    exact = _exact_fingerprint(query_text)
    cluster = _cluster_key(query_text, reason_code, plan)
    with db() as conn:
        daily_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM unmet_request_events WHERE created_at >= datetime('now', 'start of day')"
            ).fetchone()[0]
        )
        if daily_count >= EVOLUTION_MAX_EVENTS_PER_DAY:
            return False
        cluster_daily_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM unmet_request_events
                WHERE cluster_key=? AND created_at >= datetime('now', 'start of day')
                """,
                (cluster,),
            ).fetchone()[0]
        )
        if cluster_daily_count >= EVOLUTION_MAX_CLUSTER_EVENTS_PER_DAY:
            return False
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO unmet_request_events(
                fingerprint, cluster_key, query_text, source, reason_code,
                reason_detail, user_id, request_id, planner, plan_json, context_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exact,
                cluster,
                query_text,
                source[:40],
                reason_code[:80],
                redact_text(reason_detail, 1000) or None,
                user_id,
                request_id[:80] if request_id else None,
                planner[:80] if planner else None,
                _json_text(plan),
                _json_text(context),
            ),
        )
        return cur.rowcount > 0


def _parse_db_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.utcnow()


def _priority_score(occurrences: int, feedback_count: int, reason: str, last_seen: str) -> float:
    age_days = max(0.0, (datetime.utcnow() - _parse_db_time(last_seen)).total_seconds() / 86400)
    recency = max(0.0, 7.0 - age_days)
    return round(occurrences * 2 + feedback_count * 8 + _REASON_WEIGHTS.get(reason, 3) + recency, 2)


def _aggregate_backlog() -> tuple[int, list[dict[str, Any]]]:
    cutoff = f"-{EVOLUTION_RETENTION_DAYS} days"
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM unmet_request_events
            WHERE created_at >= datetime('now', ?)
            ORDER BY created_at ASC, id ASC
            """,
            (cutoff,),
        ).fetchall()

        grouped: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            grouped[row["cluster_key"]].append(row)

        for cluster_key, events in grouped.items():
            reasons = Counter(str(event["reason_code"]) for event in events)
            primary_reason = max(
                reasons,
                key=lambda reason: (reasons[reason], _REASON_WEIGHTS.get(reason, 0)),
            )
            feedback_count = sum(event["source"] == "user_feedback" for event in events)
            preferred = next((event for event in reversed(events) if event["source"] == "user_feedback"), events[-1])
            score = _priority_score(len(events), feedback_count, primary_reason, events[-1]["created_at"])
            conn.execute(
                """
                INSERT INTO evolution_backlog(
                    cluster_key, sample_fingerprint, sample_query, primary_reason,
                    reason_detail, occurrence_count, feedback_count, priority_score,
                    first_seen_at, last_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(cluster_key) DO UPDATE SET
                    sample_fingerprint=excluded.sample_fingerprint,
                    sample_query=excluded.sample_query,
                    primary_reason=excluded.primary_reason,
                    reason_detail=excluded.reason_detail,
                    occurrence_count=excluded.occurrence_count,
                    feedback_count=excluded.feedback_count,
                    priority_score=excluded.priority_score,
                    first_seen_at=excluded.first_seen_at,
                    last_seen_at=excluded.last_seen_at,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    cluster_key,
                    preferred["fingerprint"],
                    preferred["query_text"],
                    primary_reason,
                    preferred["reason_detail"],
                    len(events),
                    feedback_count,
                    score,
                    events[0]["created_at"],
                    events[-1]["created_at"],
                ),
            )

        backlog_rows = conn.execute(
            """
            SELECT * FROM evolution_backlog
            WHERE status='open'
            ORDER BY priority_score DESC, feedback_count DESC, occurrence_count DESC, last_seen_at DESC
            LIMIT ?
            """,
            (EVOLUTION_REPORT_LIMIT,),
        ).fetchall()
    return len(rows), [dict(row) for row in backlog_rows]


def _task_prompt(item: dict[str, Any]) -> str:
    return (
        "Analyze and implement support for this unmet Minecraft seed-search request: "
        f"{item['sample_query']} Current failure category: {item['primary_reason']}. "
        "Add a reproducible test, keep results auditable, preserve existing search and map behavior, "
        "and do not claim exactness when the generator can only provide an approximation."
    )


def _report_payload(run_key: str, event_count: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_key": run_key,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event_count": event_count,
        "backlog_count": len(items),
        "items": [
            {
                **item,
                "task_prompt": _task_prompt(item),
                "required_gates": [
                    "isolated_branch_or_worktree",
                    "automated_tests",
                    "behavioral_acceptance",
                    "human_approval_before_deploy",
                ],
            }
            for item in items
        ],
    }


def _markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Evolution Backlog",
        "",
        f"Generated: {payload['generated_at']}",
        f"Events: {payload['event_count']} | Open clusters: {payload['backlog_count']}",
        "",
    ]
    if not payload["items"]:
        lines.append("No unmet requests have been recorded yet.")
        return "\n".join(lines) + "\n"
    for index, item in enumerate(payload["items"], 1):
        query = str(item["sample_query"]).replace("\n", " ")
        lines.extend(
            [
                f"## {index}. {query}",
                "",
                f"- Priority: {item['priority_score']}",
                f"- Occurrences: {item['occurrence_count']}",
                f"- User feedback: {item['feedback_count']}",
                f"- Reason: {item['primary_reason']}",
                f"- Last seen: {item['last_seen_at']}",
                "",
                item["task_prompt"],
                "",
            ]
        )
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def run_evolution_cycle(run_key: str | None = None, *, force: bool = False) -> dict[str, Any]:
    init_db()
    local_now = datetime.now().astimezone()
    run_key = run_key or (f"manual-{local_now.strftime('%Y%m%d-%H%M%S')}" if force else local_now.date().isoformat())
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO evolution_runs(run_key) VALUES (?)",
            (run_key,),
        )
        if cur.rowcount == 0:
            row = conn.execute("SELECT * FROM evolution_runs WHERE run_key=?", (run_key,)).fetchone()
            return {"skipped": True, "run": dict(row) if row else {"run_key": run_key}}

    try:
        event_count, items = _aggregate_backlog()
        payload = _report_payload(run_key, event_count, items)
        latest_json = EVOLUTION_DIR / "latest.json"
        latest_markdown = EVOLUTION_DIR / "latest.md"
        archive_json = EVOLUTION_DIR / "runs" / f"{run_key.replace(':', '-')}.json"
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        _atomic_write(latest_json, serialized)
        _atomic_write(latest_markdown, _markdown_report(payload))
        _atomic_write(archive_json, serialized)
        with db() as conn:
            conn.execute(
                """
                UPDATE evolution_runs
                SET finished_at=CURRENT_TIMESTAMP, event_count=?, backlog_count=?,
                    status='completed', report_path=?, error_detail=NULL
                WHERE run_key=?
                """,
                (event_count, len(items), str(latest_json), run_key),
            )
        return payload
    except Exception as exc:
        with db() as conn:
            conn.execute(
                """
                UPDATE evolution_runs
                SET finished_at=CURRENT_TIMESTAMP, status='failed', error_detail=?
                WHERE run_key=?
                """,
                (redact_text(f"{type(exc).__name__}: {exc}", 1000), run_key),
            )
        raise


def evolution_status() -> dict[str, Any]:
    init_db()
    with db() as conn:
        event_count = int(conn.execute("SELECT COUNT(*) FROM unmet_request_events").fetchone()[0])
        open_count = int(conn.execute("SELECT COUNT(*) FROM evolution_backlog WHERE status='open'").fetchone()[0])
        last_run = conn.execute(
            "SELECT * FROM evolution_runs ORDER BY started_at DESC, run_key DESC LIMIT 1"
        ).fetchone()
    return {
        "active_searches": active_search_count(),
        "event_count": event_count,
        "open_backlog_count": open_count,
        "last_run": dict(last_run) if last_run else None,
        "next_window": f"{EVOLUTION_NIGHT_HOUR:02d}:{EVOLUTION_NIGHT_MINUTE:02d}",
    }


def _completed_today(day_key: str) -> bool:
    with db() as conn:
        row = conn.execute("SELECT status FROM evolution_runs WHERE run_key=?", (day_key,)).fetchone()
    return bool(row and row["status"] in {"running", "completed"})


async def evolution_scheduler() -> None:
    await asyncio.sleep(5)
    while True:
        now = datetime.now().astimezone()
        target = now.replace(
            hour=EVOLUTION_NIGHT_HOUR,
            minute=EVOLUTION_NIGHT_MINUTE,
            second=0,
            microsecond=0,
        )
        day_key = now.date().isoformat()
        if now >= target and not _completed_today(day_key):
            if active_search_count() == 0:
                run_evolution_cycle(day_key)
                continue
            await asyncio.sleep(300)
            continue

        next_target = target if now < target else target + timedelta(days=1)
        delay = max(5.0, min(3600.0, (next_target - now).total_seconds()))
        await asyncio.sleep(delay)


def start_evolution_scheduler() -> asyncio.Task:
    global _SCHEDULER_TASK
    if _SCHEDULER_TASK is None or _SCHEDULER_TASK.done():
        _SCHEDULER_TASK = asyncio.create_task(evolution_scheduler(), name="evolution-scheduler")
    return _SCHEDULER_TASK


async def stop_evolution_scheduler() -> None:
    global _SCHEDULER_TASK
    task = _SCHEDULER_TASK
    _SCHEDULER_TASK = None
    if not task:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    print(json.dumps(run_evolution_cycle(force=True), ensure_ascii=False, indent=2))
