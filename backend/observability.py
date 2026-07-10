from __future__ import annotations

import json
import logging
import os
import resource
import threading
import time
import uuid
from collections import defaultdict
from typing import Any

from fastapi import Request


REQUEST_LOGGER = logging.getLogger("mc_seed_finder.requests")
if not logging.getLogger().handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

_LOCK = threading.Lock()
_REQUESTS: dict[tuple[str, str, int], int] = defaultdict(int)
_REQUEST_SECONDS: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
_SEARCH_STAGES: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
_STARTED_AT = time.time()


def observe_request(method: str, path: str, status: int, duration: float) -> None:
    with _LOCK:
        _REQUESTS[(method, path, status)] += 1
        values = _REQUEST_SECONDS[(method, path)]
        values[0] += 1
        values[1] += duration


def observe_search_stage(stage: str, outcome: str, duration: float) -> None:
    with _LOCK:
        values = _SEARCH_STAGES[(stage, outcome)]
        values[0] += 1
        values[1] += duration


async def request_metrics_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        duration = time.perf_counter() - started
        route = request.scope.get("route")
        path = getattr(route, "path", "__unmatched__")
        observe_request(request.method, path, status, duration)
        if path not in {"/health/live", "/health/ready", "/metrics"} or status >= 400:
            REQUEST_LOGGER.info(
                json.dumps(
                    {
                        "event": "http_request",
                        "request_id": request_id,
                        "method": request.method,
                        "path": path,
                        "status": status,
                        "duration_ms": round(duration * 1000, 2),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )


def _labels(**labels: Any) -> str:
    if not labels:
        return ""
    encoded = []
    for key, value in labels.items():
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        encoded.append(f'{key}="{escaped}"')
    return "{" + ",".join(encoded) + "}"


def render_metrics(
    *,
    active_searches: int,
    jobs: dict[str, int],
    map_cache: dict[str, Any],
) -> str:
    with _LOCK:
        requests = dict(_REQUESTS)
        request_seconds = {key: list(value) for key, value in _REQUEST_SECONDS.items()}
        search_stages = {key: list(value) for key, value in _SEARCH_STAGES.items()}

    lines = [
        "# HELP mc_uptime_seconds Process uptime in seconds.",
        "# TYPE mc_uptime_seconds gauge",
        f"mc_uptime_seconds {time.time() - _STARTED_AT:.3f}",
        "# HELP mc_active_searches Searches currently executing in this process.",
        "# TYPE mc_active_searches gauge",
        f"mc_active_searches {active_searches}",
        "# HELP mc_process_max_rss_bytes Maximum resident set size.",
        "# TYPE mc_process_max_rss_bytes gauge",
        f"mc_process_max_rss_bytes {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}",
        "# HELP mc_http_requests_total HTTP requests by route and status.",
        "# TYPE mc_http_requests_total counter",
    ]
    for (method, path, status), count in sorted(requests.items()):
        lines.append(f"mc_http_requests_total{_labels(method=method, path=path, status=status)} {count}")
    lines.extend(
        [
            "# HELP mc_http_request_duration_seconds_sum Total HTTP request time.",
            "# TYPE mc_http_request_duration_seconds_sum counter",
        ]
    )
    for (method, path), (count, seconds) in sorted(request_seconds.items()):
        labels = _labels(method=method, path=path)
        lines.append(f"mc_http_request_duration_seconds_count{labels} {int(count)}")
        lines.append(f"mc_http_request_duration_seconds_sum{labels} {seconds:.6f}")
    lines.extend(
        [
            "# HELP mc_search_stage_duration_seconds_sum Search stage time by outcome.",
            "# TYPE mc_search_stage_duration_seconds_sum counter",
        ]
    )
    for (stage, outcome), (count, seconds) in sorted(search_stages.items()):
        labels = _labels(stage=stage, outcome=outcome)
        lines.append(f"mc_search_stage_duration_seconds_count{labels} {int(count)}")
        lines.append(f"mc_search_stage_duration_seconds_sum{labels} {seconds:.6f}")
    lines.extend(
        [
            "# HELP mc_search_jobs Number of persisted search jobs by state.",
            "# TYPE mc_search_jobs gauge",
        ]
    )
    for status, count in sorted(jobs.items()):
        lines.append(f"mc_search_jobs{_labels(status=status)} {count}")
    for field in ("hits", "misses", "writes", "errors", "files", "bytes"):
        metric_type = "counter" if field in {"hits", "misses", "writes", "errors"} else "gauge"
        lines.append(f"# TYPE mc_map_cache_{field} {metric_type}")
        lines.append(f"mc_map_cache_{field} {int(map_cache.get(field, 0))}")
    return "\n".join(lines) + "\n"
