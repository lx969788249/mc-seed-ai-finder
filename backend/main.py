from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .auth import create_user, current_user, login_user, logout_user, optional_user
from .catalog import catalog_payload
from .database import init_db
from .evolution import (
    active_search_count,
    evolution_status,
    record_unmet_request,
    search_activity_finished,
    search_activity_started,
    start_evolution_scheduler,
    stop_evolution_scheduler,
)
from .jobs import cancel_job, create_job, get_job, job_counts, list_jobs, start_job_workers, stop_job_workers
from .map_cache import cache_stats, get_or_create as get_or_create_map
from .observability import observe_search_stage, render_metrics, request_metrics_middleware
from .mc_core import generate_map, search
from .models import ChatIn, ChatOut, Credentials, MapIn, ModelsIn, SearchIn, SettingsIn, UnmetFeedbackIn
from .planner import deepseek_plan, list_deepseek_models
from .progress import finish_progress, get_progress, start_progress, update_progress
from .security import SESSION_COOKIE
from .settings_store import get_settings, save_settings, settings_out


ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

app = FastAPI(title="Minecraft Seed AI Finder", version="0.1.0")
app.middleware("http")(request_metrics_middleware)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=4)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    init_db()
    start_evolution_scheduler()
    start_job_workers(_execute_persisted_job)


@app.on_event("shutdown")
async def shutdown() -> None:
    await stop_job_workers()
    await stop_evolution_scheduler()


@app.post("/auth/register")
def register(payload: Credentials, response: Response):
    create_user(payload.username, payload.password)
    return login_user(payload.username, payload.password, response)


@app.post("/auth/login")
def login(payload: Credentials, response: Response):
    return login_user(payload.username, payload.password, response)


@app.post("/auth/logout")
def logout(response: Response, token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    logout_user(token, response)
    return {"ok": True}


@app.get("/me")
def me(user=Depends(current_user)):
    return user


@app.get("/settings")
def get_user_settings(user=Depends(current_user)):
    return settings_out(user["id"])


@app.put("/settings")
def put_user_settings(payload: SettingsIn, user=Depends(current_user)):
    return save_settings(user["id"], payload)


@app.get("/catalog")
def catalog():
    return catalog_payload()


@app.get("/health/live")
def health_live():
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready():
    checks = {"database": False, "native": False}
    try:
        from .database import db

        with db() as conn:
            conn.execute("SELECT 1").fetchone()
        checks["database"] = True
    except Exception:
        pass
    checks["native"] = (ROOT / "native" / "mc_query").is_file()
    ready = all(checks.values())
    return JSONResponse({"status": "ok" if ready else "not_ready", "checks": checks}, status_code=200 if ready else 503)


@app.get("/metrics")
def metrics():
    return PlainTextResponse(
        render_metrics(active_searches=active_search_count(), jobs=job_counts(), map_cache=cache_stats()),
        media_type="text/plain; version=0.0.4",
    )


@app.get("/backends")
def backends():
    return {
        "minecraft": [
            {
                "id": "cubiomes_native",
                "name": "cubiomes/native",
                "mode": "exact_limited",
                "description": "使用 cubiomes 进行真实生物群系和结构候选校验；支持到 1.21 系列，不支持 26.2。",
            },
            {
                "id": "disabled_simulator",
                "name": "旧模拟坐标生成器",
                "mode": "disabled",
                "description": "已禁用。它不使用 Minecraft 世界生成算法，会给出错误坐标。",
            },
        ],
        "ai": {"default_base_url": "https://api.deepseek.com", "default_model": "deepseek-v4-flash"},
    }


@app.post("/ai/models")
async def ai_models(payload: ModelsIn, token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    api_key = payload.deepseek_api_key
    user = optional_user(token)
    if not api_key and user:
        api_key = get_settings(user["id"], include_secret=True).get("deepseek_api_key")
    models, warnings = await list_deepseek_models(api_key, payload.deepseek_base_url)
    return {"models": models, "warnings": warnings}


@app.post("/map")
def map_view(payload: MapIn, response: Response):
    params = payload.model_dump(mode="json")
    data, warnings, cache = get_or_create_map(
        params,
        lambda: generate_map(
            payload.seed,
            payload.version,
            payload.center_x,
            payload.center_z,
            payload.radius,
            payload.size,
        ),
    )
    response.headers["X-Map-Cache"] = "HIT" if cache["hit"] else "MISS"
    return {"map": data, "warnings": warnings, "cache": cache}


@app.get("/progress/{request_id}")
def progress(request_id: str):
    return get_progress(request_id)


def _coords_from_message(message: str) -> tuple[Optional[int], Optional[int]]:
    m = re.search(r"(-?\d{1,7})\s*[,，]\s*(-?\d{1,7})", message)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _limit_from_message(message: str) -> Optional[int]:
    patterns = [
        r"(?:前|显示|返回|列出|给我|最多)\s*(\d{1,2})\s*(?:个|条|项|处|组)?",
        r"(?:top|first)\s*(\d{1,2})",
        r"(\d{1,2})\s*(?:个|条|项|处|组)\s*(?:结果|候选)",
    ]
    for pattern in patterns:
        m = re.search(pattern, message, re.I)
        if m:
            return max(1, min(20, int(m.group(1))))
    return None


def _area_summary(result: dict) -> str:
    checks = result.get("biome_area_checks") or []
    if not checks:
        return ""
    check = checks[0]
    prefix = "至少 " if check.get("truncated") else "约 "
    return f"{check.get('label') or check.get('target')}连通面积{prefix}{int(check.get('area') or 0):,} 平方格"


def _unsupported_plan_reply(plan) -> Optional[str]:
    if plan.capability != "unsupported":
        return None
    if plan.objective == "ai_required":
        reason = plan.unsupported_reason or "需要 DeepSeek 先解析用户需求。"
        suggestion = plan.fallback_suggestion or "请先在左侧 DeepSeek 设置里填写并保存 API Key。"
        return f"{reason} {suggestion}"
    target_text = "、".join(t.label for t in plan.targets) or "相关目标"
    reason = plan.unsupported_reason or "当前后端没有实现这个指标的可验证搜索。"
    suggestion = plan.fallback_suggestion or f"可以先改问“找最近的{target_text}”。"
    return f"这个需求我已经理解为「{target_text} / {plan.objective}」，但当前不能精确执行：{reason} {suggestion}"


async def _capture_unmet(
    message: str,
    settings: dict,
    *,
    source: str,
    reason_code: str,
    reason_detail: str | None,
    request_id: str | None,
    user_id: int | None,
    planner: str | None,
    plan=None,
) -> None:
    plan_data = plan.model_dump() if hasattr(plan, "model_dump") else plan
    context = {
        "seed": settings.get("seed"),
        "version": settings.get("version"),
        "center_x": settings.get("center_x"),
        "center_z": settings.get("center_z"),
        "max_results": settings.get("max_results"),
    }
    try:
        record_unmet_request(
            message,
            source=source,
            reason_code=reason_code,
            reason_detail=reason_detail,
            user_id=user_id,
            request_id=request_id,
            planner=planner,
            plan=plan_data,
            context=context,
        )
    except Exception:
        return


async def _run_query(
    message: str,
    settings: dict,
    request_id: Optional[str] = None,
    user_id: int | None = None,
) -> ChatOut:
    start_progress(request_id, "正在准备搜索")
    cx, cz = _coords_from_message(message)
    if cx is not None and cz is not None:
        settings = {**settings, "center_x": cx, "center_z": cz}
    requested_limit = _limit_from_message(message)
    settings = {**settings, "max_results": requested_limit or 1}
    context = {
        "seed": settings["seed"],
        "version": settings["version"],
        "center": {"x": settings["center_x"], "z": settings["center_z"]},
        "search_policy": "不使用用户配置的最大搜索半径；后端会自动扩大到 Minecraft Java 可玩世界范围，用户明确写出的“附近 N 格内”只作为目标之间的距离约束。",
        "max_results": settings["max_results"],
    }
    planner: str | None = None
    plan = None
    search_activity_started()
    try:
        update_progress(
            request_id,
            stage="ai",
            message="DeepSeek 正在解析",
            checked=None,
            total=None,
            radius=None,
        )

        def report_progress(**fields):
            update_progress(request_id, **fields)

        ai_started = time.perf_counter()
        planner, plan, warnings = await deepseek_plan(
            message,
            context,
            settings.get("deepseek_api_key"),
            settings["deepseek_base_url"],
            settings["deepseek_model"],
            progress=report_progress,
        )
        observe_search_stage("deepseek", planner, time.perf_counter() - ai_started)
        unsupported_reply = _unsupported_plan_reply(plan)
        if unsupported_reply:
            if plan.objective != "ai_required":
                await _capture_unmet(
                    message,
                    settings,
                    source="automatic",
                    reason_code="unsupported_capability",
                    reason_detail=plan.unsupported_reason or unsupported_reply,
                    request_id=request_id,
                    user_id=user_id,
                    planner=planner,
                    plan=plan,
                )
            finish_progress(request_id, "搜索未运行", "done")
            return ChatOut(
                reply=unsupported_reply,
                planner=planner,
                plan=plan,
                results=[],
                warnings=list(dict.fromkeys(warnings + [plan.unsupported_reason or "当前需求超出已接入的 Minecraft 搜索核心能力。"])),
            )

        update_progress(
            request_id,
            stage="search",
            message="正在启动 Minecraft 搜索核心",
            checked=None,
            total=None,
            radius=None,
        )

        native_started = time.perf_counter()
        results, search_warnings = await asyncio.to_thread(
            search,
            settings["seed"],
            settings["version"],
            settings["center_x"],
            settings["center_z"],
            settings["search_radius"],
            settings["max_results"],
            plan,
            report_progress,
        )
        observe_search_stage("native", "results" if results else "empty", time.perf_counter() - native_started)
        targets = "、".join(t.label for t in plan.targets)
        if results:
            best = results[0]
            area_text = _area_summary(best)
            if plan.objective == "largest_area" and area_text:
                samples = int((best.get("area_search") or {}).get("samples_checked") or 0)
                reply = (
                    f"已在 Minecraft 可玩范围内抽样 {samples:,} 个位置并复测候选。"
                    f"当前发现最大的{plan.targets[0].label}位于 X={best['suggested']['x']}，Z={best['suggested']['z']}；{area_text}。"
                )
                all_warnings = warnings + search_warnings + [w for r in results for w in r.get("warnings", [])]
                finish_progress(request_id, "搜索完成", "done")
                return ChatOut(reply=reply, planner=planner, plan=plan, results=results, warnings=list(dict.fromkeys(all_warnings)))
            layout_text = ""
            if best.get("layout_checks"):
                check = best["layout_checks"][0]
                layout_text = (
                    f"；前后布局：{check['back_label']}与{check['front_label']}以{check['center_label']}为中心的夹角 "
                    f"{check['angle']}°（要求至少 {check['min_angle']}°）"
                )
            if best["satisfied"]:
                area_suffix = f"；{area_text}" if area_text else ""
                reply = f"已按「{targets}」搜索。推荐先核对 X={best['suggested']['x']}，Z={best['suggested']['z']}；距离当前位置约 {best['distance']} 格{layout_text}{area_suffix}，约束审核：满足约束。"
            else:
                reason = "；".join(best.get("failure_reasons") or ["未满足全部约束"])
                reply = f"已按「{targets}」搜索，但没有找到完全满足约束的区域。当前显示的是最接近候选：X={best['suggested']['x']}，Z={best['suggested']['z']}；失败原因：{reason}。"
                await _capture_unmet(
                    message,
                    settings,
                    source="automatic",
                    reason_code="constraints_unsatisfied",
                    reason_detail=reason,
                    request_id=request_id,
                    user_id=user_id,
                    planner=planner,
                    plan=plan,
                )
        else:
            reason = next(
                (
                    w
                    for w in reversed(search_warnings)
                    if not w.startswith("Java 26.2")
                ),
                "",
            )
            reply = f"没有找到满足「{targets}」全部约束的可验证结果。{reason or '请减少目标、放宽距离/紧邻约束，或改用受支持版本。'}"
            await _capture_unmet(
                message,
                settings,
                source="automatic",
                reason_code="no_verified_result",
                reason_detail=reason or "没有找到满足全部约束的可验证结果。",
                request_id=request_id,
                user_id=user_id,
                planner=planner,
                plan=plan,
            )
        all_warnings = warnings + search_warnings + [w for r in results for w in r.get("warnings", [])]
        finish_progress(request_id, "搜索完成", "done")
        return ChatOut(reply=reply, planner=planner, plan=plan, results=results, warnings=list(dict.fromkeys(all_warnings)))
    except Exception as exc:
        await _capture_unmet(
            message,
            settings,
            source="automatic",
            reason_code="execution_error",
            reason_detail=f"{type(exc).__name__}: {exc}",
            request_id=request_id,
            user_id=user_id,
            planner=planner,
            plan=plan,
        )
        finish_progress(request_id, "搜索失败", "error")
        raise
    finally:
        search_activity_finished()


async def _execute_persisted_job(job: dict) -> dict:
    payload = dict(job["payload"])
    if job["kind"] == "chat":
        if not job.get("user_id"):
            raise ValueError("chat job requires a user")
        message = str(payload["message"])
        settings = dict(payload["settings"])
        settings["deepseek_api_key"] = job.get("secret")
        settings["deepseek_api_key_set"] = bool(job.get("secret"))
    else:
        message = str(payload.pop("query"))
        payload.pop("request_id", None)
        settings = payload
        settings["deepseek_api_key"] = job.get("secret")
        settings["deepseek_api_key_set"] = bool(job.get("secret"))
    response = await _run_query(message, settings, job["id"], user_id=job.get("user_id"))
    return response.model_dump(mode="json")


def _visible_job(job_id: str, token: str | None) -> dict:
    item = get_job(job_id)
    if not item:
        raise HTTPException(status_code=404, detail="任务不存在")
    if item.get("user_id"):
        user = optional_user(token)
        if not user or int(user["id"]) != int(item["user_id"]):
            raise HTTPException(status_code=404, detail="任务不存在")
    return item


@app.post("/jobs/chat", status_code=202)
async def enqueue_chat(payload: ChatIn, user=Depends(current_user)):
    settings = get_settings(user["id"], include_secret=True)
    secret = settings.pop("deepseek_api_key", None)
    return create_job(
        "chat",
        {"message": payload.message, "settings": settings},
        user_id=user["id"],
        secret=secret,
    )


@app.post("/jobs/search", status_code=202)
async def enqueue_search(payload: SearchIn, token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    data = payload.model_dump()
    secret = data.pop("deepseek_api_key", None)
    user = optional_user(token)
    return create_job("search", data, user_id=user["id"] if user else None, secret=secret)


@app.get("/jobs/{job_id}")
async def job_status(job_id: str, token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    return _visible_job(job_id, token)


@app.post("/jobs/{job_id}/cancel")
async def job_cancel(job_id: str, token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    _visible_job(job_id, token)
    return cancel_job(job_id)


@app.get("/jobs")
async def user_jobs(limit: int = 20, user=Depends(current_user)):
    return {"jobs": list_jobs(user["id"], limit)}


@app.post("/chat", response_model=ChatOut)
async def chat(payload: ChatIn, user=Depends(current_user)):
    settings = get_settings(user["id"], include_secret=True)
    return await _run_query(payload.message, settings, payload.request_id, user_id=user["id"])


@app.post("/search", response_model=ChatOut)
async def direct_search(payload: SearchIn, token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    settings = payload.model_dump()
    settings["deepseek_api_key_set"] = bool(payload.deepseek_api_key)
    user = optional_user(token)
    return await _run_query(payload.query, settings, payload.request_id, user_id=user["id"] if user else None)


@app.post("/feedback/unmet")
async def unmet_feedback(
    payload: UnmetFeedbackIn,
    token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    user = optional_user(token)
    recorded = record_unmet_request(
        payload.query,
        source="user_feedback",
        reason_code="user_report",
        reason_detail=f"{payload.reason}: {payload.detail or '用户标记结果未解决问题。'}",
        user_id=user["id"] if user else None,
        request_id=payload.request_id,
        planner=payload.planner,
        plan=payload.plan,
        context={"feedback_reason": payload.reason},
    )
    return {"ok": True, "recorded": recorded}


@app.get("/evolution/status")
async def get_evolution_status():
    return evolution_status()


app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
