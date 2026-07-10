from __future__ import annotations

import copy
import os
import json
import math
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .catalog import BIOMES, STRUCTURES
from .models import SearchPlan, Target


ROOT = Path(__file__).resolve().parent.parent
MC_QUERY = ROOT / "native" / "mc_query"

SUPPORTED_CUBIOMES_VERSIONS = {
    "1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9",
    "1.10", "1.11", "1.12", "1.13", "1.14", "1.15", "1.16", "1.16.1", "1.16.5",
    "1.17", "1.17.1", "1.18", "1.18.2", "1.19", "1.19.2", "1.19.4",
    "1.20", "1.20.6", "1.21", "1.21.1", "1.21.2", "1.21.3", "1.21 WD",
}

STRUCTURE_IDS = {
    "village",
    "witch_hut",
    "pillager_outpost",
    "desert_pyramid",
    "jungle_pyramid",
    "igloo",
    "ocean_monument",
    "woodland_mansion",
    "ruined_portal",
    "ancient_city",
    "trial_chambers",
    "shipwreck",
    "nether_fortress",
    "bastion_remnant",
    "end_city",
}

BIOME_IDS = {
    "plains",
    "sunflower_plains",
    "cherry_grove",
    "swamp",
    "mangrove_swamp",
    "forest",
    "flower_forest",
    "dark_forest",
    "desert",
    "jungle",
    "badlands",
    "savanna",
    "snowy_plains",
    "meadow",
    "grove",
    "snowy_slopes",
    "jagged_peaks",
    "frozen_peaks",
    "stony_peaks",
    "mushroom_fields",
    "ocean",
    "warm_ocean",
    "lukewarm_ocean",
    "deep_ocean",
    "river",
    "beach",
}

TERRAIN_LIMITED_STRUCTURES = {"desert_pyramid", "jungle_pyramid", "woodland_mansion"}
TARGET_DIMENSIONS = {
    "nether_fortress": "nether",
    "bastion_remnant": "nether",
    "end_city": "end",
}
SEARCH_START_RADIUS = 4096
WORLD_SEARCH_RADIUS = 30_000_000
MAX_NATIVE_CANDIDATES = 32768
ANCHOR_SCAN_LIMITS = [128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768]
NEAR_TARGET_LIMIT = 24
HUB_CANDIDATE_LIMIT = 8
LOCAL_BIOME_NEAR_RADIUS = 4096
NATIVE_CACHE_LIMIT = 4096
ANCHOR_NEIGHBOR_BATCH_SIZE = 512
ANCHOR_TILE_SIZE = 65_536
BIOME_AREA_GLOBAL_SAMPLES = 80_000
BIOME_AREA_GLOBAL_CANDIDATES = 48
BIOME_AREA_COARSE_STEP = 64
BIOME_AREA_FINE_STEP = 4
BIOME_AREA_DEFAULT_RADIUS = 4096
_NATIVE_SEARCH_CACHE: dict[tuple[Any, ...], tuple[list[dict[str, Any]], str, tuple[str, ...]]] = {}
_BIOME_AT_CACHE: dict[tuple[Any, ...], tuple[str | None, tuple[str, ...]]] = {}
_BIOME_AREA_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_BIOME_AREA_SPATIAL_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_CACHE_LOCK = threading.RLock()
ProgressCallback = Callable[..., None]

ANCHOR_RARITY_SCORE = {
    "woodland_mansion": 110,
    "ancient_city": 95,
    "witch_hut": 90,
    "trial_chambers": 80,
    "ocean_monument": 70,
    "desert_pyramid": 60,
    "jungle_pyramid": 60,
    "igloo": 55,
    "pillager_outpost": 45,
    "bastion_remnant": 45,
    "nether_fortress": 40,
    "end_city": 40,
    "ruined_portal": 30,
    "shipwreck": 30,
    "village": 20,
}


def _java_seed(seed: str) -> int:
    try:
        return int(seed)
    except ValueError:
        h = 0
        for ch in seed:
            h = (31 * h + ord(ch)) & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
        return h


def map_url(seed: str, version: str, x: int, z: int) -> str:
    return f"https://www.chunkbase.com/apps/seed-map#seed={seed}&platform=java_{version}&x={x}&z={z}&zoom=0.5"


def _resolve_version(version: str) -> tuple[str, str, list[str]]:
    if version in SUPPORTED_CUBIOMES_VERSIONS:
        return version, "exact", []
    if version == "26.2" or version.startswith("26.2"):
        return "1.21.3", "compatibility", []
    return version, "unsupported", []


def _cache_trim(cache: dict) -> None:
    if len(cache) > NATIVE_CACHE_LIMIT:
        cache.clear()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _native_query_workers() -> int:
    cpu_default = max(1, min(8, os.cpu_count() or 2))
    return _env_int("MC_QUERY_WORKERS", cpu_default, 1, 64)


def _native_batch_size() -> int:
    return _env_int("MC_QUERY_BATCH_SIZE", 64, 1, 512)


def _clone_search_response(response: tuple[list[dict[str, Any]], str, tuple[str, ...]]) -> tuple[list[dict[str, Any]], str, list[str]]:
    results, mode, warnings = response
    return [dict(item) for item in results], mode, list(warnings)


def _search_cache_key(
    seed: str,
    mapped_version: str,
    center_x: int,
    center_z: int,
    radius: int,
    limit: int,
    target: Target,
) -> tuple[Any, ...]:
    native_kind = _native_query_kind(target, radius)
    return (
        str(_java_seed(seed)),
        mapped_version,
        native_kind,
        target.id,
        int(center_x),
        int(center_z),
        max(1, int(radius)),
        max(1, min(MAX_NATIVE_CANDIDATES, int(limit))),
    )


def _target_preflight(target: Target, mode: str, version: str) -> list[str]:
    if mode == "unsupported":
        return [f"当前精确后端 cubiomes 不支持 Java {version}。已停止返回模拟坐标；请改用 1.21.3/1.21/1.20.6 等受支持版本，或接入支持该版本的世界生成核心。"]
    if target.kind == "structure" and target.id not in STRUCTURE_IDS:
        return [f"精确后端暂不支持结构：{target.label}。"]
    if target.kind == "biome" and target.id not in BIOME_IDS:
        return [f"精确后端暂不支持生物群系：{target.label}。"]
    if target.kind == "structure" and target.id in TERRAIN_LIMITED_STRUCTURES:
        return [f"{target.label} 在新版 Minecraft 中还可能受地形高度影响；cubiomes 只能做结构位置和生物群系校验，仍建议进游戏或用地图工具复核。"]
    return []


def _native_query_kind(target: Target, radius: int) -> str:
    if target.kind == "biome" and int(radius) <= LOCAL_BIOME_NEAR_RADIUS:
        return "biome_near"
    return target.kind


def _parse_candidate_results(items: list[dict[str, Any]], target: Target, center_x: int, center_z: int) -> list[dict[str, Any]]:
    results = []
    for item in items:
        x = int(item["x"])
        z = int(item["z"])
        results.append(
            {
                "id": target.id,
                "kind": target.kind,
                "label": target.label,
                "x": x,
                "z": z,
                "distance_to_center": round(float(item.get("distance", math.dist((center_x, center_z), (x, z)))), 1),
            }
        )
    return results


def _call_cubiomes_batch(
    seed: str,
    version: str,
    queries: list[tuple[int, int, int, int, Target]],
) -> list[tuple[list[dict[str, Any]], str, list[str]]]:
    if not queries:
        return []

    warnings: list[str] = []
    mapped_version, mode, version_warnings = _resolve_version(version)
    warnings.extend(version_warnings)
    if mode == "unsupported":
        warning = f"当前精确后端 cubiomes 不支持 Java {version}。已停止返回模拟坐标；请改用 1.21.3/1.21/1.20.6 等受支持版本，或接入支持该版本的世界生成核心。"
        return [([], "unsupported", [warning]) for _query in queries]
    if not MC_QUERY.exists():
        return [([], mode, ["cubiomes 查询工具尚未编译，已停止返回模拟坐标。请运行 README 中的 native 编译步骤。"]) for _query in queries]

    outputs: list[tuple[list[dict[str, Any]], str, list[str]] | None] = [None] * len(queries)
    native_queries: list[tuple[int, int, int, int, Target, int, tuple[Any, ...], list[str]]] = []
    for idx, (center_x, center_z, radius, limit, target) in enumerate(queries):
        target_warnings = warnings + _target_preflight(target, mode, version)
        if any("暂不支持" in warning for warning in target_warnings):
            outputs[idx] = ([], mode, target_warnings)
            continue
        cache_key = _search_cache_key(seed, mapped_version, center_x, center_z, radius, limit, target)
        with _CACHE_LOCK:
            cached = _NATIVE_SEARCH_CACHE.get(cache_key)
        if cached is not None:
            cached_results, cached_mode, cached_warnings = _clone_search_response(cached)
            outputs[idx] = (cached_results, cached_mode, list(dict.fromkeys(target_warnings + cached_warnings)))
            continue
        native_queries.append((center_x, center_z, radius, limit, target, idx, cache_key, target_warnings))

    if not native_queries:
        return [output if output is not None else ([], mode, warnings) for output in outputs]

    def run_query_chunk(
        query_chunk: list[tuple[int, int, int, int, Target, int, tuple[Any, ...], list[str]]],
    ) -> list[tuple[int, tuple[list[dict[str, Any]], str, list[str]], tuple[Any, ...] | None, tuple[list[dict[str, Any]], str, tuple[str, ...]] | None]]:
        cmd = [str(MC_QUERY), "batch", mapped_version, str(_java_seed(seed)), str(len(query_chunk))]
        for center_x, center_z, radius, limit, target, _idx, _cache_key, _target_warnings in query_chunk:
            native_kind = _native_query_kind(target, radius)
            cmd.extend(
                [
                    native_kind,
                    target.id,
                    str(center_x),
                    str(center_z),
                    str(max(1, radius)),
                    str(max(1, min(MAX_NATIVE_CANDIDATES, limit))),
                ]
            )
        try:
            timeout = max(45, min(180, 10 + len(query_chunk) * 8))
            proc = subprocess.run(cmd, cwd=ROOT, check=False, text=True, capture_output=True, timeout=timeout)
            payload = json.loads(proc.stdout or "{}")
        except Exception as exc:
            failure = f"cubiomes 查询失败：{type(exc).__name__}。"
            return [
                (idx, ([], mode, target_warnings + [failure]), None, None)
                for _center_x, _center_z, _radius, _limit, _target, idx, _cache_key, target_warnings in query_chunk
            ]

        if not payload.get("ok"):
            error = payload.get("error", "unknown_error")
            return [
                (idx, ([], mode, target_warnings + [f"cubiomes 不支持此查询：{error}。"]), None, None)
                for _center_x, _center_z, _radius, _limit, _target, idx, _cache_key, target_warnings in query_chunk
            ]

        chunk_outputs: list[tuple[int, tuple[list[dict[str, Any]], str, list[str]], tuple[Any, ...] | None, tuple[list[dict[str, Any]], str, tuple[str, ...]] | None]] = []
        items = payload.get("results", [])
        for item, (center_x, center_z, _radius, _limit, target, idx, cache_key, target_warnings) in zip(items, query_chunk):
            if not item.get("ok"):
                error = item.get("error", "unknown_error")
                chunk_outputs.append((idx, ([], mode, target_warnings + [f"cubiomes 不支持此查询：{error}。"]), None, None))
                continue
            results = _parse_candidate_results(item.get("results", []), target, center_x, center_z)
            stored = ([dict(candidate) for candidate in results], mode, tuple(target_warnings))
            chunk_outputs.append((idx, ([dict(candidate) for candidate in results], mode, target_warnings), cache_key, stored))

        for _center_x, _center_z, _radius, _limit, _target, idx, _cache_key, target_warnings in query_chunk[len(items):]:
            chunk_outputs.append((idx, ([], mode, target_warnings + ["cubiomes 批量查询返回数量不足。"]), None, None))
        return chunk_outputs

    chunks = [native_queries[start : start + _native_batch_size()] for start in range(0, len(native_queries), _native_batch_size())]
    workers = min(_native_query_workers(), len(chunks))
    chunk_results = []
    if workers > 1 and len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mc-query") as executor:
            futures = [executor.submit(run_query_chunk, chunk) for chunk in chunks]
            for future in as_completed(futures):
                chunk_results.extend(future.result())
    else:
        for chunk in chunks:
            chunk_results.extend(run_query_chunk(chunk))

    with _CACHE_LOCK:
        for idx, output, cache_key, stored in chunk_results:
            outputs[idx] = output
            if cache_key is not None and stored is not None:
                _NATIVE_SEARCH_CACHE[cache_key] = stored
        _cache_trim(_NATIVE_SEARCH_CACHE)

    return [output if output is not None else ([], mode, warnings) for output in outputs]


def _call_cubiomes(seed: str, version: str, center_x: int, center_z: int, radius: int, limit: int, target: Target) -> tuple[list[dict[str, Any]], str, list[str]]:
    return _call_cubiomes_batch(seed, version, [(center_x, center_z, radius, limit, target)])[0]


def _call_anchor_combo_batch(
    seed: str,
    version: str,
    anchors: list[dict[str, Any]],
    target_specs: list[tuple[int, Target, int, int]],
) -> tuple[list[dict[str, Any]], list[str], bool]:
    if not anchors or not target_specs:
        return [], [], False

    mapped_version, mode, version_warnings = _resolve_version(version)
    warnings = list(version_warnings)
    if mode == "unsupported" or not MC_QUERY.exists():
        return [], warnings, False

    for _idx, target, _radius, _limit in target_specs:
        preflight = _target_preflight(target, mode, version)
        warnings.extend(preflight)
        if any("暂不支持" in warning for warning in preflight):
            return [], list(dict.fromkeys(warnings)), False

    def run_anchor_chunk(chunk_start: int, chunk: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]], list[str], bool]:
        cmd = [
            str(MC_QUERY),
            "anchor_combo",
            mapped_version,
            str(_java_seed(seed)),
            str(len(target_specs)),
            str(len(chunk)),
        ]
        for _idx, target, radius, limit in target_specs:
            cmd.extend([
                target.kind,
                target.id,
                str(max(1, radius)),
                str(max(1, min(MAX_NATIVE_CANDIDATES, limit))),
            ])
        for anchor in chunk:
            cmd.extend([str(int(anchor["x"])), str(int(anchor["z"]))])

        try:
            timeout = max(45, min(300, 10 + len(chunk) * len(target_specs) * 4))
            proc = subprocess.run(cmd, cwd=ROOT, check=False, text=True, capture_output=True, timeout=timeout)
            payload = json.loads(proc.stdout or "{}")
        except Exception as exc:
            return chunk_start, [], [f"native 组合搜索失败：{type(exc).__name__}。"], False

        if not payload.get("ok"):
            return chunk_start, [], [f"native 组合搜索失败：{payload.get('error', 'unknown_error')}。"], False

        parsed_items: list[dict[str, Any]] = []
        raw_items = payload.get("results", [])
        if len(raw_items) < len(chunk):
            return chunk_start, [], ["native 组合搜索返回数量不足。"], False

        for anchor_pos, (raw, anchor) in enumerate(zip(raw_items, chunk)):
            targets_by_index: dict[int, list[dict[str, Any]]] = {}
            item_warnings: list[str] = []
            for spec_pos, target_payload in enumerate(raw.get("targets", [])):
                if spec_pos >= len(target_specs):
                    break
                target_idx, target, _radius, _limit = target_specs[spec_pos]
                if not target_payload.get("ok"):
                    item_warnings.append(
                        f"native 组合搜索目标 {target.label} 失败：{target_payload.get('error', 'unknown_error')}。"
                    )
                    continue
                targets_by_index[target_idx] = _parse_candidate_results(
                    target_payload.get("results", []),
                    target,
                    int(anchor["x"]),
                    int(anchor["z"]),
                )
            parsed_items.append(
                {
                    "anchor_pos": chunk_start + anchor_pos,
                    "complete": bool(raw.get("complete")),
                    "targets": targets_by_index,
                    "warnings": item_warnings,
                }
            )
        return chunk_start, parsed_items, [], True

    chunks = [
        (start, anchors[start : start + _native_batch_size()])
        for start in range(0, len(anchors), _native_batch_size())
    ]
    workers = min(_native_query_workers(), len(chunks))
    parsed: list[dict[str, Any] | None] = [None] * len(anchors)
    if workers > 1 and len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mc-combo") as executor:
            futures = [executor.submit(run_anchor_chunk, start, chunk) for start, chunk in chunks]
            for future in as_completed(futures):
                _start, items, chunk_warnings, ok = future.result()
                warnings.extend(chunk_warnings)
                if not ok:
                    return [], list(dict.fromkeys(warnings)), False
                for item in items:
                    parsed[int(item["anchor_pos"])] = item
                    warnings.extend(item.get("warnings", []))
    else:
        for start, chunk in chunks:
            _start, items, chunk_warnings, ok = run_anchor_chunk(start, chunk)
            warnings.extend(chunk_warnings)
            if not ok:
                return [], list(dict.fromkeys(warnings)), False
            for item in items:
                parsed[int(item["anchor_pos"])] = item
                warnings.extend(item.get("warnings", []))

    if any(item is None for item in parsed):
        return [], list(dict.fromkeys(warnings + ["native 组合搜索结果缺失。"])), False
    return [dict(item) for item in parsed if item is not None], list(dict.fromkeys(warnings)), True


def _biome_name_matches(name: str | None, target_id: str) -> bool:
    if not name:
        return False
    if name == target_id:
        return True
    if target_id == "ocean":
        return "ocean" in name
    if target_id == "forest":
        return "forest" in name
    if target_id == "jungle":
        return "jungle" in name
    if target_id == "badlands":
        return "badlands" in name
    if target_id == "swamp":
        return "swamp" in name
    if target_id == "plains":
        return "plains" in name
    return False


def _call_biome_at(seed: str, version: str, x: int, z: int) -> tuple[str | None, list[str]]:
    results, warnings = _call_biome_at_many(seed, version, [(x, z)])
    return results.get((x, z)), warnings


def _call_biome_at_many(seed: str, version: str, points: list[tuple[int, int]]) -> tuple[dict[tuple[int, int], str | None], list[str]]:
    unique_points = list(dict.fromkeys((int(x), int(z)) for x, z in points))
    if not unique_points:
        return {}, []

    mapped_version, mode, version_warnings = _resolve_version(version)
    warnings = list(version_warnings)
    if mode == "unsupported":
        return {}, [f"当前精确后端 cubiomes 不支持 Java {version}，无法校验坐标生物群系。"]
    if not MC_QUERY.exists():
        return {}, ["cubiomes 查询工具尚未编译，无法校验坐标生物群系。"]

    found: dict[tuple[int, int], str | None] = {}
    pending: list[tuple[int, int]] = []
    for x, z in unique_points:
        cache_key = (str(_java_seed(seed)), mapped_version, x, z)
        with _CACHE_LOCK:
            cached = _BIOME_AT_CACHE.get(cache_key)
        if cached is not None:
            found[(x, z)] = cached[0]
            warnings.extend(cached[1])
        else:
            pending.append((x, z))

    for start in range(0, len(pending), 512):
        chunk = pending[start : start + 512]
        cmd = [str(MC_QUERY), "batch", mapped_version, str(_java_seed(seed)), str(len(chunk))]
        for x, z in chunk:
            cmd.extend(["biome_at", "point", str(x), str(z), "1", "1"])
        try:
            timeout = max(15, min(120, 5 + len(chunk) // 4))
            proc = subprocess.run(cmd, cwd=ROOT, check=False, text=True, capture_output=True, timeout=timeout)
            payload = json.loads(proc.stdout or "{}")
        except Exception as exc:
            warnings.append(f"坐标生物群系校验失败：{type(exc).__name__}。")
            continue
        if not payload.get("ok"):
            warnings.append(f"坐标生物群系校验失败：{payload.get('error', 'unknown_error')}。")
            continue
        for item, (x, z) in zip(payload.get("results", []), chunk):
            if not item.get("ok"):
                warnings.append(f"坐标生物群系校验失败：{item.get('error', 'unknown_error')}。")
                continue
            name = item.get("biome", {}).get("name")
            found[(x, z)] = name
            with _CACHE_LOCK:
                _BIOME_AT_CACHE[(str(_java_seed(seed)), mapped_version, x, z)] = (name, tuple(version_warnings))
                _cache_trim(_BIOME_AT_CACHE)
    return found, list(dict.fromkeys(warnings))



def generate_map(seed: str, version: str, center_x: int, center_z: int, radius: int, size: int) -> tuple[dict[str, Any], list[str]]:
    mapped_version, mode, warnings = _resolve_version(version)
    if mode == "unsupported":
        return {"ok": False, "results": []}, [f"当前地图后端 cubiomes 不支持 Java {version}，无法渲染真实地图。"]
    if not MC_QUERY.exists():
        return {"ok": False, "results": []}, ["cubiomes 查询工具尚未编译，无法渲染地图。"]
    cmd = [
        str(MC_QUERY),
        "map",
        mapped_version,
        str(_java_seed(seed)),
        str(center_x),
        str(center_z),
        str(max(1, radius)),
        str(max(16, min(256, size))),
    ]
    try:
        proc = subprocess.run(cmd, cwd=ROOT, check=False, text=True, capture_output=True, timeout=45)
        payload = json.loads(proc.stdout or "{}")
    except Exception as exc:
        return {"ok": False, "results": []}, [f"地图渲染失败：{type(exc).__name__}。"]
    if not payload.get("ok"):
        return payload, warnings + [f"地图渲染失败：{payload.get('error', 'unknown_error')}。"]
    payload["mode"] = mode
    payload["version"] = version
    payload["mapped_version"] = mapped_version
    payload["seed"] = seed
    payload["center"] = {"x": center_x, "z": center_z}
    payload["radius"] = radius
    return payload, warnings


def _call_biome_samples(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    radius: int,
    samples: int,
    limit: int,
    target: Target,
) -> tuple[list[dict[str, Any]], str, list[str], dict[str, Any]]:
    mapped_version, mode, warnings = _resolve_version(version)
    if mode == "unsupported" or target.kind != "biome":
        return [], mode, [f"当前版本无法执行{target.label}面积候选抽样。"], {}
    if not MC_QUERY.exists():
        return [], mode, ["cubiomes 查询工具尚未编译，无法执行群系面积候选抽样。"], {}
    cmd = [
        str(MC_QUERY),
        "biome_samples",
        mapped_version,
        str(_java_seed(seed)),
        target.id,
        str(center_x),
        str(center_z),
        str(max(1, radius)),
        str(max(1, min(1_000_000, samples))),
        str(max(1, min(512, limit))),
    ]
    try:
        proc = subprocess.run(cmd, cwd=ROOT, check=False, text=True, capture_output=True, timeout=45)
        payload = json.loads(proc.stdout or "{}")
    except Exception as exc:
        return [], mode, [f"群系面积候选抽样失败：{type(exc).__name__}。"], {}
    if not payload.get("ok"):
        return [], mode, [f"群系面积候选抽样失败：{payload.get('error', 'unknown_error')}。"], {}
    candidates = _parse_candidate_results(payload.get("results", []), target, center_x, center_z)
    metadata = {
        "method": "deterministic_world_sampling",
        "samples_checked": int(payload.get("samples_checked") or samples),
        "radius": int(payload.get("radius") or radius),
        "candidate_count": len(candidates),
        "exact_global": False,
    }
    return candidates, mode, warnings, metadata


def _call_biome_area(
    seed: str,
    version: str,
    target: Target,
    x: int,
    z: int,
    radius: int = BIOME_AREA_DEFAULT_RADIUS,
    step: int = BIOME_AREA_COARSE_STEP,
) -> tuple[dict[str, Any] | None, list[str]]:
    mapped_version, mode, warnings = _resolve_version(version)
    if mode == "unsupported" or target.kind != "biome":
        return None, [f"当前版本无法测量{target.label}面积。"]
    if not MC_QUERY.exists():
        return None, ["cubiomes 查询工具尚未编译，无法测量群系面积。"]
    radius = max(step * 4, min(16384, int(radius)))
    cache_key = (str(_java_seed(seed)), mapped_version, target.id, int(x), int(z), radius, step)
    spatial_key = None
    if target.id == "ocean" and step >= 16:
        spatial_key = (
            str(_java_seed(seed)),
            mapped_version,
            target.id,
            int(x) // 2048,
            int(z) // 2048,
            radius,
            step,
        )
    with _CACHE_LOCK:
        cached = _BIOME_AREA_CACHE.get(cache_key)
        if cached is None and spatial_key is not None:
            cached = _BIOME_AREA_SPATIAL_CACHE.get(spatial_key)
    if cached is not None:
        return copy.deepcopy(cached), list(warnings)

    cmd = [
        str(MC_QUERY),
        "biome_area",
        mapped_version,
        str(_java_seed(seed)),
        target.id,
        str(int(x)),
        str(int(z)),
        str(radius),
        str(step),
    ]
    try:
        proc = subprocess.run(cmd, cwd=ROOT, check=False, text=True, capture_output=True, timeout=45)
        payload = json.loads(proc.stdout or "{}")
    except Exception as exc:
        return None, warnings + [f"{target.label}面积测量失败：{type(exc).__name__}。"]
    if not payload.get("ok"):
        return None, warnings + [f"{target.label}面积测量失败：{payload.get('error', 'unknown_error')}。"]
    payload["mode"] = mode
    payload["version"] = version
    payload["mapped_version"] = mapped_version
    payload["label"] = target.label
    with _CACHE_LOCK:
        _BIOME_AREA_CACHE[cache_key] = copy.deepcopy(payload)
        if spatial_key is not None:
            _BIOME_AREA_SPATIAL_CACHE[spatial_key] = copy.deepcopy(payload)
        _cache_trim(_BIOME_AREA_CACHE)
        _cache_trim(_BIOME_AREA_SPATIAL_CACHE)
    return payload, list(warnings)


def _biome_area_constraint_items(plan: SearchPlan) -> list[tuple[Target, int | None, str, int]]:
    target_by_id = {target.id: target for target in plan.targets}
    items: list[tuple[Target, int | None, str, int]] = []
    for raw in plan.biome_area_constraints:
        if not isinstance(raw, dict):
            continue
        target = target_by_id.get(str(raw.get("target") or raw.get("id") or ""))
        if not target or target.kind != "biome":
            continue
        min_area = _coerce_int(raw.get("min_area"), None)
        preference = str(raw.get("preference") or "larger")
        measurement_radius = max(512, min(16384, int(_coerce_int(raw.get("measurement_radius"), BIOME_AREA_DEFAULT_RADIUS) or BIOME_AREA_DEFAULT_RADIUS)))
        items.append((target, max(0, min_area) if min_area is not None else None, preference, measurement_radius))
    return items


def _measure_biome_point(
    seed: str,
    version: str,
    target: Target,
    point: dict[str, Any],
    min_area: int | None,
    measurement_radius: int,
    step: int = BIOME_AREA_COARSE_STEP,
) -> tuple[dict[str, Any] | None, list[str]]:
    measurement, warnings = _call_biome_area(
        seed,
        version,
        target,
        int(point["x"]),
        int(point["z"]),
        measurement_radius,
        step,
    )
    while (
        measurement
        and measurement.get("truncated")
        and measurement_radius < 16384
        and (min_area is None or int(measurement.get("area") or 0) < min_area)
    ):
        measurement_radius = min(16384, measurement_radius * 2)
        measurement, extra_warnings = _call_biome_area(
            seed,
            version,
            target,
            int(point["x"]),
            int(point["z"]),
            measurement_radius,
            step,
        )
        warnings.extend(extra_warnings)
    return measurement, list(dict.fromkeys(warnings))


def _evaluate_biome_area_constraints(
    seed: str,
    version: str,
    plan: SearchPlan,
    points: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    warnings: list[str] = []
    for target, min_area, preference, measurement_radius in _biome_area_constraint_items(plan):
        point = next((item for item in points if item.get("id") == target.id), None)
        if not point:
            failures.append(f"缺少可测量面积的{target.label}候选。")
            continue
        measurement, measure_warnings = _measure_biome_point(
            seed,
            version,
            target,
            point,
            min_area,
            measurement_radius,
        )
        warnings.extend(measure_warnings)
        if not measurement:
            failures.append(f"无法测量{target.label}的连通面积。")
            continue
        area = int(measurement.get("area") or 0)
        passed = min_area is None or area >= min_area
        check = {
            "target": target.id,
            "label": target.label,
            "area": area,
            "min_area": min_area,
            "preference": preference,
            "passed": passed,
            "step": int(measurement.get("step") or BIOME_AREA_COARSE_STEP),
            "radius": int(measurement.get("radius") or measurement_radius),
            "closed": bool(measurement.get("closed")),
            "truncated": bool(measurement.get("truncated")),
            "bounds": measurement.get("bounds"),
            "center": measurement.get("center"),
            "width": measurement.get("width"),
            "height": measurement.get("height"),
            "perimeter": measurement.get("perimeter"),
            "sample_y": int(measurement.get("sample_y") or 63),
        }
        checks.append(check)
        point["biome_area"] = area
        point["biome_area_closed"] = check["closed"]
        point["biome_area_step"] = check["step"]
        if not passed:
            failures.append(f"{target.label}连通面积约 {area:,} 平方格，小于要求的 {min_area:,} 平方格。")
    return checks, failures, list(dict.fromkeys(warnings))


def _site_from_points(points: list[dict[str, Any]]) -> tuple[int, int]:
    return (
        int(sum(p["x"] for p in points) / len(points)),
        int(sum(p["z"] for p in points) / len(points)),
    )


def _max_pairwise(points: list[dict[str, Any]]) -> float:
    if len(points) < 2:
        return 0.0
    max_d = 0.0
    for i, a in enumerate(points):
        for b in points[i + 1 :]:
            max_d = max(max_d, math.dist((a["x"], a["z"]), (b["x"], b["z"])))
    return round(max_d, 1)


def _search_budget(requested_radius: int) -> int:
    return max(WORLD_SEARCH_RADIUS, requested_radius, SEARCH_START_RADIUS)


def _radius_sequence(requested_radius: int) -> list[int]:
    budget = _search_budget(requested_radius)
    radii = []
    r = SEARCH_START_RADIUS
    while r < budget:
        radii.append(r)
        r *= 2
    radii.append(budget)
    return sorted(set(radii))


def _format_blocks(value: int) -> str:
    n = max(0, int(value))
    if n >= 10_000:
        wan = n / 10_000
        if wan >= 100:
            text = f"{wan:.0f}"
        elif wan >= 10:
            text = f"{wan:.1f}"
        else:
            text = f"{wan:.2f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{text}万格"
    return f"{n:,}格"


def _coerce_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _norm_lookup(value: Any) -> str:
    return "".join(ch for ch in str(value or "").strip().lower() if ch not in " _-/")


def _biome_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for biome_id, info in BIOMES.items():
        for raw in [biome_id, info.get("label"), *(info.get("aliases") or [])]:
            aliases[_norm_lookup(raw)] = biome_id
    return aliases


BIOME_ALIASES = _biome_aliases()


def _resolve_biome_id(value: Any) -> str | None:
    key = _norm_lookup(value)
    if not key:
        return None
    if key in BIOME_IDS:
        return key
    return BIOME_ALIASES.get(key)


def _target_from_id(target_id: str) -> Target | None:
    if target_id in STRUCTURES:
        return Target(kind="structure", id=target_id, label=str(STRUCTURES[target_id]["label"]))
    if target_id in BIOMES:
        return Target(kind="biome", id=target_id, label=str(BIOMES[target_id]["label"]))
    return None


def _target_dimension(target_or_id: Target | str | None) -> str:
    target_id = target_or_id.id if isinstance(target_or_id, Target) else str(target_or_id or "")
    return TARGET_DIMENSIONS.get(target_id, "overworld")


def _plan_dimensions(plan: SearchPlan) -> set[str]:
    dimensions = {_target_dimension(target) for target in plan.targets}
    for target, _min_count, _within in _count_constraint_items(plan):
        dimensions.add(_target_dimension(target))
    for a_id, b_id, _min_distance in _exclude_target_constraints(plan):
        dimensions.add(_target_dimension(a_id))
        dimensions.add(_target_dimension(b_id))
    return dimensions or {"overworld"}


def _dimension_search_center(dimension: str, center_x: int, center_z: int) -> tuple[int, int, list[str]]:
    if dimension == "nether":
        return (
            int(round(center_x / 8)),
            int(round(center_z / 8)),
            ["下界结构搜索已将当前位置按主世界坐标 / 8 转换为下界搜索中心；结果坐标为下界坐标，并附带主世界约等效坐标。"],
        )
    return center_x, center_z, []


def _attach_dimension_metadata(results: list[dict[str, Any]], dimension: str) -> list[dict[str, Any]]:
    if dimension == "overworld":
        return results
    enriched: list[dict[str, Any]] = []
    for result in results:
        item = dict(result)
        item["dimension"] = dimension
        if dimension == "nether" and item.get("suggested"):
            item["suggested_overworld"] = {"x": int(item["suggested"]["x"]) * 8, "z": int(item["suggested"]["z"]) * 8}
            item["coordinate_note"] = "结果为下界坐标；suggested_overworld 是主世界约等效坐标。"
        else:
            item["coordinate_note"] = "结果为末地坐标。"
        targets = []
        for target in item.get("targets", []):
            target_item = dict(target)
            if dimension == "nether" and _target_dimension(target_item.get("id")) == "nether":
                target_item["overworld_equivalent"] = {"x": int(target_item["x"]) * 8, "z": int(target_item["z"]) * 8}
            targets.append(target_item)
        item["targets"] = targets
        enriched.append(item)
    return enriched


def _adj_max_distance(adj: dict[str, Any], default: int | None = 512) -> int | None:
    return _coerce_int(
        adj.get("threshold")
        if adj.get("threshold") is not None
        else adj.get("max_threshold")
        if adj.get("max_threshold") is not None
        else adj.get("max_distance")
        if adj.get("max_distance") is not None
        else adj.get("distance")
        if adj.get("distance") is not None
        else adj.get("within")
        if adj.get("within") is not None
        else adj.get("range"),
        default,
    )


def _adj_min_distance(adj: dict[str, Any]) -> int | None:
    return _coerce_int(
        adj.get("min_threshold")
        if adj.get("min_threshold") is not None
        else adj.get("min_distance")
        if adj.get("min_distance") is not None
        else adj.get("at_least"),
        None,
    )


def _pair_matches(a_id: str, b_id: str, raw_a: Any, raw_b: Any) -> bool:
    return {str(raw_a), str(raw_b)} == {a_id, b_id}


def _relative_layout_constraints(plan: SearchPlan) -> list[dict[str, Any]]:
    target_by_id = {target.id: target for target in plan.targets}
    constraints: list[dict[str, Any]] = []
    for raw in plan.relative_layout:
        if not isinstance(raw, dict):
            continue
        center_id = str(raw.get("center") or raw.get("pivot") or raw.get("anchor") or "")
        back_id = str(raw.get("back") or raw.get("behind") or raw.get("rear") or raw.get("a") or "")
        front_id = str(raw.get("front") or raw.get("facing") or raw.get("face") or raw.get("b") or "")
        if len({center_id, back_id, front_id}) < 3 or any(item not in target_by_id for item in (center_id, back_id, front_id)):
            continue
        min_angle = max(90, min(180, int(_coerce_int(raw.get("min_angle") or raw.get("angle"), 120) or 120)))
        shared_distance = max(64, int(_coerce_int(raw.get("max_distance") or raw.get("threshold"), 512) or 512))
        constraints.append(
            {
                "center": center_id,
                "back": back_id,
                "front": front_id,
                "min_angle": min_angle,
                "back_max_distance": max(64, int(_coerce_int(raw.get("back_max_distance"), shared_distance) or shared_distance)),
                "front_max_distance": max(64, int(_coerce_int(raw.get("front_max_distance"), shared_distance) or shared_distance)),
            }
        )
    return constraints


def _relative_layout_angle(center: dict[str, Any], back: dict[str, Any], front: dict[str, Any]) -> float | None:
    back_x = float(back["x"]) - float(center["x"])
    back_z = float(back["z"]) - float(center["z"])
    front_x = float(front["x"]) - float(center["x"])
    front_z = float(front["z"]) - float(center["z"])
    back_length = math.hypot(back_x, back_z)
    front_length = math.hypot(front_x, front_z)
    if back_length <= 0 or front_length <= 0:
        return None
    cosine = (back_x * front_x + back_z * front_z) / (back_length * front_length)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _relative_layout_checks(plan: SearchPlan, points: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    point_by_id = {str(point.get("id")): point for point in points}
    target_by_id = {target.id: target for target in plan.targets}
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    for constraint in _relative_layout_constraints(plan):
        center = point_by_id.get(constraint["center"])
        back = point_by_id.get(constraint["back"])
        front = point_by_id.get(constraint["front"])
        center_label = target_by_id[constraint["center"]].label
        back_label = target_by_id[constraint["back"]].label
        front_label = target_by_id[constraint["front"]].label
        if not center or not back or not front:
            failures.append(f"缺少{center_label}、{back_label}或{front_label}坐标，无法审核前后布局。")
            continue
        angle = _relative_layout_angle(center, back, front)
        back_distance = round(math.dist((center["x"], center["z"]), (back["x"], back["z"])), 1)
        front_distance = round(math.dist((center["x"], center["z"]), (front["x"], front["z"])), 1)
        angle_passed = angle is not None and angle + 1e-9 >= constraint["min_angle"]
        back_distance_passed = back_distance <= constraint["back_max_distance"]
        front_distance_passed = front_distance <= constraint["front_max_distance"]
        passed = angle_passed and back_distance_passed and front_distance_passed
        check = {
            **constraint,
            "relation": "opposite_sides",
            "center_label": center_label,
            "back_label": back_label,
            "front_label": front_label,
            "angle": round(angle, 1) if angle is not None else None,
            "back_distance": back_distance,
            "front_distance": front_distance,
            "angle_passed": angle_passed,
            "back_distance_passed": back_distance_passed,
            "front_distance_passed": front_distance_passed,
            "passed": passed,
        }
        checks.append(check)
        if not angle_passed:
            angle_text = "无法计算" if angle is None else f"{round(angle, 1)}°"
            failures.append(
                f"以{center_label}为中心，{back_label}与{front_label}夹角为 {angle_text}，"
                f"小于两侧布局要求的 {constraint['min_angle']}°。"
            )
        if not back_distance_passed:
            failures.append(f"{back_label}距{center_label} {back_distance} 格，超过背靠距离 {constraint['back_max_distance']} 格。")
        if not front_distance_passed:
            failures.append(f"{front_label}距{center_label} {front_distance} 格，超过面朝距离 {constraint['front_max_distance']} 格。")
    return checks, failures


def _relative_layout_fits_points(plan: SearchPlan, points: list[dict[str, Any]]) -> bool:
    point_by_id = {str(point.get("id")): point for point in points}
    for constraint in _relative_layout_constraints(plan):
        center = point_by_id.get(constraint["center"])
        back = point_by_id.get(constraint["back"])
        front = point_by_id.get(constraint["front"])
        if not center or not back or not front:
            continue
        angle = _relative_layout_angle(center, back, front)
        if angle is None or angle + 1e-9 < constraint["min_angle"]:
            return False
    return True


def _excluded_biomes_for_target(plan: SearchPlan, target_id: str) -> set[str]:
    excluded: set[str] = set()
    for item in plan.exclude_biomes:
        if not isinstance(item, dict):
            continue
        raw_target = item.get("target") or item.get("structure") or item.get("id") or item.get("a")
        if str(raw_target) != target_id:
            continue
        raw_biomes = item.get("biomes")
        if raw_biomes is None:
            raw_biomes = item.get("exclude") or item.get("not_in") or item.get("forbidden") or item.get("b")
        if raw_biomes is None:
            continue
        values = raw_biomes if isinstance(raw_biomes, list) else [raw_biomes]
        for value in values:
            biome_id = _resolve_biome_id(value)
            if biome_id:
                excluded.add(biome_id)
    return excluded


def _biome_matches_any(name: str | None, biome_ids: set[str]) -> bool:
    return any(_biome_name_matches(name, biome_id) for biome_id in biome_ids)


def _exclude_target_constraints(plan: SearchPlan) -> list[tuple[str, str, int]]:
    constraints: list[tuple[str, str, int]] = []
    for item in plan.exclude_targets:
        if not isinstance(item, dict):
            continue
        a_id = str(item.get("a") or item.get("from") or item.get("source") or "")
        b_id = str(item.get("b") or item.get("target") or item.get("to") or "")
        if not a_id or not b_id:
            continue
        min_distance = _coerce_int(
            item.get("min_distance")
            if item.get("min_distance") is not None
            else item.get("threshold")
            if item.get("threshold") is not None
            else item.get("distance")
            if item.get("distance") is not None
            else item.get("radius"),
            512,
        )
        constraints.append((a_id, b_id, int(min_distance or 512)))
    return constraints


def _pair_min_distance(plan: SearchPlan, a_id: str, b_id: str) -> int | None:
    minimums: list[int] = []
    for adj in plan.adjacency:
        if adj.get("relation") == "same_biome":
            continue
        if _pair_matches(a_id, b_id, adj.get("a"), adj.get("b")):
            value = _adj_min_distance(adj)
            if value is not None:
                minimums.append(value)
    for raw_a, raw_b, min_distance in _exclude_target_constraints(plan):
        if {raw_a, raw_b} == {a_id, b_id}:
            minimums.append(min_distance)
    return max(minimums) if minimums else None


def _direction_key(value: Any) -> str:
    text = _norm_lookup(value)
    mapping = {
        "east": "east",
        "e": "east",
        "东": "east",
        "东边": "east",
        "west": "west",
        "w": "west",
        "西": "west",
        "西边": "west",
        "north": "north",
        "n": "north",
        "北": "north",
        "北边": "north",
        "south": "south",
        "s": "south",
        "南": "south",
        "南边": "south",
        "northeast": "northeast",
        "ne": "northeast",
        "东北": "northeast",
        "northwest": "northwest",
        "nw": "northwest",
        "西北": "northwest",
        "southeast": "southeast",
        "se": "southeast",
        "东南": "southeast",
        "southwest": "southwest",
        "sw": "southwest",
        "西南": "southwest",
    }
    return mapping.get(text, text)


def _point_in_area(x: int, z: int, center_x: int, center_z: int, area: dict[str, Any] | None) -> bool:
    if not area:
        return True
    min_x = _coerce_int(area.get("min_x") if isinstance(area, dict) else None, None)
    max_x = _coerce_int(area.get("max_x") if isinstance(area, dict) else None, None)
    min_z = _coerce_int(area.get("min_z") if isinstance(area, dict) else None, None)
    max_z = _coerce_int(area.get("max_z") if isinstance(area, dict) else None, None)
    if min_x is not None and x < min_x:
        return False
    if max_x is not None and x > max_x:
        return False
    if min_z is not None and z < min_z:
        return False
    if max_z is not None and z > max_z:
        return False
    direction = _direction_key(area.get("direction") if isinstance(area, dict) else None)
    if direction in {"east", "northeast", "southeast"} and x < center_x:
        return False
    if direction in {"west", "northwest", "southwest"} and x > center_x:
        return False
    if direction in {"north", "northeast", "northwest"} and z > center_z:
        return False
    if direction in {"south", "southeast", "southwest"} and z < center_z:
        return False
    return True


def _filter_area_candidates(plan: SearchPlan, candidates: list[dict[str, Any]], center_x: int, center_z: int) -> list[dict[str, Any]]:
    if not plan.area:
        return candidates
    return [
        candidate
        for candidate in candidates
        if _point_in_area(int(candidate["x"]), int(candidate["z"]), center_x, center_z, plan.area)
    ]


def _candidate_limit(max_results: int) -> int:
    return max(50, min(256, max_results * 64))


def _anchor_scan_limits(plan: SearchPlan) -> list[int]:
    positive_limits = [
        int(limit)
        for adj in plan.adjacency
        for limit in [_adj_max_distance(adj, None)]
        if limit is not None and adj.get("relation") != "same_biome"
    ]
    tight = positive_limits and min(positive_limits) <= 768
    complex_plan = len(plan.targets) >= 4 or len(plan.adjacency) >= 3 or bool(plan.exclude_biomes or plan.exclude_targets)
    if complex_plan and tight:
        return [MAX_NATIVE_CANDIDATES]
    return [limit for limit in ANCHOR_SCAN_LIMITS if limit <= MAX_NATIVE_CANDIDATES]


def _same_biome_constraints(plan: SearchPlan) -> list[tuple[str, str]]:
    constraints: list[tuple[str, str]] = []
    target_by_id = {t.id: t for t in plan.targets}
    for adj in plan.adjacency:
        if adj.get("relation") != "same_biome":
            continue
        a = target_by_id.get(str(adj.get("a")))
        b = target_by_id.get(str(adj.get("b")))
        if not a or not b:
            continue
        if a.kind == "structure" and b.kind == "biome":
            constraints.append((a.id, b.id))
        elif b.kind == "structure" and a.kind == "biome":
            constraints.append((b.id, a.id))
    return constraints


def _delay_anchor_biome_filter(plan: SearchPlan, anchor_id: str) -> bool:
    return True


def _same_biome_materialized_targets(plan: SearchPlan) -> dict[str, str]:
    mapping: dict[str, str | None] = {}
    for structure_id, biome_id in _same_biome_constraints(plan):
        if biome_id in mapping and mapping[biome_id] != structure_id:
            mapping[biome_id] = None
        else:
            mapping[biome_id] = structure_id
    return {biome_id: structure_id for biome_id, structure_id in mapping.items() if structure_id}


def _materialize_same_biome_target(biome_target: Target, structure_point: dict[str, Any], structure_id: str) -> dict[str, Any]:
    return {
        **structure_point,
        "id": biome_target.id,
        "kind": biome_target.kind,
        "label": biome_target.label,
        "verified_at": structure_id,
    }


def _apply_same_biome_filters(
    seed: str,
    version: str,
    plan: SearchPlan,
    per_target: list[list[dict[str, Any]]],
) -> tuple[list[list[dict[str, Any]]], list[str]]:
    warnings: list[str] = []
    constraints = _same_biome_constraints(plan)
    excluded_by_structure = {
        target.id: _excluded_biomes_for_target(plan, target.id)
        for target in plan.targets
        if target.kind == "structure"
    }
    excluded_by_structure = {target_id: excluded for target_id, excluded in excluded_by_structure.items() if excluded}
    if not constraints and not excluded_by_structure:
        return per_target, warnings

    index_by_id = {target.id: idx for idx, target in enumerate(plan.targets)}
    filtered = [list(items) for items in per_target]
    required_by_structure: dict[str, set[str]] = {}
    for structure_id, biome_id in constraints:
        required_by_structure.setdefault(structure_id, set()).add(biome_id)

    points_to_check: list[tuple[int, int]] = []
    structure_ids_to_check = set(required_by_structure) | set(excluded_by_structure)
    for structure_id in structure_ids_to_check:
        idx = index_by_id.get(structure_id)
        if idx is None:
            continue
        points_to_check.extend((point["x"], point["z"]) for point in filtered[idx])

    biome_cache, biome_warnings = _call_biome_at_many(seed, version, points_to_check)
    warnings.extend(biome_warnings)

    for structure_id in structure_ids_to_check:
        idx = index_by_id.get(structure_id)
        if idx is None:
            continue
        required_biomes = required_by_structure.get(structure_id, set())
        excluded_biomes = excluded_by_structure.get(structure_id, set())
        kept = []
        for point in filtered[idx]:
            key = (point["x"], point["z"])
            biome_name = biome_cache.get(key)
            if not biome_name:
                continue
            if required_biomes and not all(_biome_name_matches(biome_name, biome_id) for biome_id in required_biomes):
                continue
            if excluded_biomes and _biome_matches_any(biome_name, excluded_biomes):
                continue
            kept.append({**point, "biome_at_point": biome_cache[key]})
        filtered[idx] = kept

    target_by_id = {target.id: target for target in plan.targets}
    for structure_id, biome_id in constraints:
        structure_idx = index_by_id.get(structure_id)
        biome_idx = index_by_id.get(biome_id)
        biome_target = target_by_id.get(biome_id)
        if structure_idx is None or biome_idx is None or biome_target is None:
            continue
        filtered[biome_idx] = [
            _materialize_same_biome_target(biome_target, point, structure_id)
            for point in filtered[structure_idx]
        ]
    return filtered, warnings


def _normalize_same_biome_points(plan: SearchPlan, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [dict(point) for point in points]
    for structure_id, biome_id in _same_biome_constraints(plan):
        structure = next((p for p in normalized if p["id"] == structure_id), None)
        biome = next((p for p in normalized if p["id"] == biome_id), None)
        if structure and biome:
            biome["x"] = structure["x"]
            biome["z"] = structure["z"]
            biome["distance_to_center"] = structure.get("distance_to_center", biome.get("distance_to_center", 0))
            biome["verified_at"] = structure_id
    return normalized


def _anchor_index(plan: SearchPlan) -> int:
    structure_indices = [idx for idx, target in enumerate(plan.targets) if target.kind == "structure"]
    if not structure_indices:
        if plan.anchor_target_id:
            for idx, target in enumerate(plan.targets):
                if target.id == plan.anchor_target_id:
                    return idx
        return 0
    if plan.anchor_target_id:
        for idx in structure_indices:
            if plan.targets[idx].id == plan.anchor_target_id:
                return idx

    adjacency_count: dict[str, int] = {}
    tightest_limit: dict[str, int] = {}
    for adj in plan.adjacency:
        relation = adj.get("relation")
        threshold = int(_adj_max_distance(adj, 512) or 512)
        for key in (str(adj.get("a")), str(adj.get("b"))):
            adjacency_count[key] = adjacency_count.get(key, 0) + 1
            if relation != "same_biome":
                tightest_limit[key] = min(tightest_limit.get(key, threshold), threshold)

    def score(idx: int) -> tuple[int, int, int]:
        target = plan.targets[idx]
        rarity = ANCHOR_RARITY_SCORE.get(target.id, 10)
        constraint_bonus = adjacency_count.get(target.id, 0) * 18
        if target.id in tightest_limit:
            constraint_bonus += max(0, 20 - tightest_limit[target.id] // 128)
        requested_bonus = 8 if plan.anchor_target_id == target.id else 0
        return (rarity + constraint_bonus + requested_bonus, rarity, -idx)

    return max(structure_indices, key=score)


def _direct_distance_limit(plan: SearchPlan, a_id: str, b_id: str) -> int | None:
    limits: list[int] = []
    for adj in plan.adjacency:
        if _pair_matches(a_id, b_id, adj.get("a"), adj.get("b")) and adj.get("relation") != "same_biome":
            value = _adj_max_distance(adj, 512)
            if value is not None:
                limits.append(value)
    for constraint in _relative_layout_constraints(plan):
        if {a_id, b_id} == {constraint["center"], constraint["back"]}:
            limits.append(constraint["back_max_distance"])
        if {a_id, b_id} == {constraint["center"], constraint["front"]}:
            limits.append(constraint["front_max_distance"])
    return min(limits) if limits else None


def _pair_distance_limit(plan: SearchPlan, a_id: str, b_id: str) -> int | None:
    direct = _direct_distance_limit(plan, a_id, b_id)
    if direct is not None and plan.pairwise_max_distance:
        return min(direct, plan.pairwise_max_distance)
    return direct if direct is not None else plan.pairwise_max_distance


def _search_radius_from_anchor(plan: SearchPlan, anchor_id: str, target_id: str) -> int:
    direct = _pair_distance_limit(plan, anchor_id, target_id)
    if direct is not None:
        return max(64, direct)
    graph_distance = _graph_distance_limit(plan, anchor_id, target_id)
    if graph_distance is not None:
        return max(64, graph_distance)
    if plan.pairwise_max_distance:
        return max(64, plan.pairwise_max_distance)
    return 3000


def _graph_distance_limit(plan: SearchPlan, start_id: str, target_id: str) -> int | None:
    ids = [target.id for target in plan.targets]
    if start_id not in ids or target_id not in ids:
        return None
    dist = {item_id: math.inf for item_id in ids}
    dist[start_id] = 0
    pending = set(ids)
    while pending:
        current = min(pending, key=lambda item_id: dist[item_id])
        pending.remove(current)
        if current == target_id:
            break
        if not math.isfinite(dist[current]):
            break
        for other in ids:
            if other == current or other not in pending:
                continue
            edge = _pair_distance_limit(plan, current, other)
            if edge is None:
                for adj in plan.adjacency:
                    if adj.get("relation") == "same_biome" and {str(adj.get("a")), str(adj.get("b"))} == {current, other}:
                        edge = 0
                        break
            if edge is None:
                continue
            dist[other] = min(dist[other], dist[current] + max(0, int(edge)))
    value = dist.get(target_id, math.inf)
    return int(value) if math.isfinite(value) else None


def _anchor_reach(plan: SearchPlan, anchor_id: str) -> int:
    ids = [target.id for target in plan.targets]
    if anchor_id not in ids:
        return 3000
    dist = {target_id: math.inf for target_id in ids}
    dist[anchor_id] = 0
    pending = set(ids)
    while pending:
        current = min(pending, key=lambda target_id: dist[target_id])
        pending.remove(current)
        if not math.isfinite(dist[current]):
            break
        for other in ids:
            if other == current or other not in pending:
                continue
            edge = _pair_distance_limit(plan, current, other)
            if edge is None:
                for adj in plan.adjacency:
                    if adj.get("relation") == "same_biome" and {str(adj.get("a")), str(adj.get("b"))} == {current, other}:
                        edge = 0
                        break
            if edge is None:
                continue
            dist[other] = min(dist[other], dist[current] + max(0, int(edge)))
    finite = [value for value in dist.values() if math.isfinite(value)]
    if len(finite) < len(ids):
        finite.append(3000)
    return int(max(finite or [3000]))


def _hub_index(plan: SearchPlan, anchor_idx: int) -> int | None:
    structure_indices = [idx for idx, target in enumerate(plan.targets) if target.kind == "structure" and idx != anchor_idx]
    if not structure_indices:
        return None
    counts: dict[str, int] = {}
    for adj in plan.adjacency:
        a = str(adj.get("a"))
        b = str(adj.get("b"))
        if not a or not b or a == "None" or b == "None":
            continue
        counts[a] = counts.get(a, 0) + 1
        counts[b] = counts.get(b, 0) + 1
    if plan.pairwise_max_distance:
        for target in plan.targets:
            counts[target.id] = max(counts.get(target.id, 0), len(plan.targets) - 1)

    def score(idx: int) -> tuple[int, int, int]:
        target = plan.targets[idx]
        connected_to_anchor = 1 if _pair_distance_limit(plan, plan.targets[anchor_idx].id, target.id) is not None else 0
        centrality = counts.get(target.id, 0)
        commonness = -ANCHOR_RARITY_SCORE.get(target.id, 10)
        return (connected_to_anchor, centrality, commonness)

    best = max(structure_indices, key=score)
    if score(best)[0] <= 0 or score(best)[1] < 2:
        return None
    return best


def _filter_same_biome_candidates(
    seed: str,
    version: str,
    plan: SearchPlan,
    target: Target,
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    required = [biome_id for structure_id, biome_id in _same_biome_constraints(plan) if structure_id == target.id]
    excluded = _excluded_biomes_for_target(plan, target.id)
    if target.kind != "structure" or not candidates or (not required and not excluded):
        return candidates, []
    points = [(candidate["x"], candidate["z"]) for candidate in candidates]
    biome_cache, warnings = _call_biome_at_many(seed, version, points)
    kept: list[dict[str, Any]] = []
    for candidate in candidates:
        key = (candidate["x"], candidate["z"])
        biome_name = biome_cache.get(key)
        if not biome_name:
            continue
        if required and not all(_biome_name_matches(biome_name, biome_id) for biome_id in required):
            continue
        if excluded and _biome_matches_any(biome_name, excluded):
            continue
        kept.append({**candidate, "biome_at_point": biome_name})
    return kept, warnings


def _is_strict_anchor_constrained_plan(plan: SearchPlan) -> bool:
    if len(plan.targets) < 2:
        return False
    anchor_idx = _anchor_index(plan)
    anchor = plan.targets[anchor_idx]
    if anchor.kind != "structure":
        return False
    materialized_biomes = _same_biome_materialized_targets(plan)
    constrained = 0
    for target in plan.targets:
        if target.id == anchor.id:
            continue
        if target.kind == "biome" and target.id in materialized_biomes:
            constrained += 1
            continue
        if _pair_distance_limit(plan, anchor.id, target.id) is None:
            return False
        constrained += 1
    return constrained > 0


def _exhaustive_scan_anchor_index(plan: SearchPlan) -> int:
    structure_indices = [idx for idx, target in enumerate(plan.targets) if target.kind == "structure"]
    if not structure_indices:
        return _anchor_index(plan)

    def score(idx: int) -> tuple[int, int, int, int]:
        target = plan.targets[idx]
        reachable = 0
        tightest = 1_000_000_000
        for other in plan.targets:
            if other.id == target.id:
                continue
            if other.kind == "biome" and other.id in _same_biome_materialized_targets(plan):
                continue
            reach = _search_radius_from_anchor(plan, target.id, other.id)
            if reach:
                reachable += 1
                tightest = min(tightest, reach)
        rarity = ANCHOR_RARITY_SCORE.get(target.id, 10)
        requested = 1 if plan.anchor_target_id == target.id else 0
        return (rarity + reachable * 20, -min(tightest, 1_000_000), rarity, requested)

    return max(structure_indices, key=score)


def _tile_ring(ring: int) -> list[tuple[int, int]]:
    if ring <= 0:
        return [(0, 0)]
    tiles: list[tuple[int, int]] = []
    for ix in range(-ring, ring + 1):
        tiles.append((ix, -ring))
        tiles.append((ix, ring))
    for iz in range(-ring + 1, ring):
        tiles.append((-ring, iz))
        tiles.append((ring, iz))
    return tiles


def _tile_min_distance(ix: int, iz: int, tile_size: int = ANCHOR_TILE_SIZE) -> float:
    dx = max(0.0, (abs(ix) - 0.5) * tile_size)
    dz = max(0.0, (abs(iz) - 0.5) * tile_size)
    return math.hypot(dx, dz)


def _tile_completed_radius(ring: int, tile_size: int = ANCHOR_TILE_SIZE) -> int:
    return int((ring + 0.5) * tile_size)


def _query_anchor_tile(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    search_radius: int,
    plan: SearchPlan,
    anchor_target: Target,
    ix: int,
    iz: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    half = ANCHOR_TILE_SIZE // 2
    tile_center_x = center_x + ix * ANCHOR_TILE_SIZE
    tile_center_z = center_z + iz * ANCHOR_TILE_SIZE
    min_x = tile_center_x - half
    max_x = tile_center_x + half
    min_z = tile_center_z - half
    max_z = tile_center_z + half
    tile_radius = int(math.ceil(math.hypot(half, half))) + 16
    candidates, _mode, warnings = _call_cubiomes(
        seed,
        version,
        tile_center_x,
        tile_center_z,
        tile_radius,
        MAX_NATIVE_CANDIDATES,
        anchor_target,
    )
    if len(candidates) >= MAX_NATIVE_CANDIDATES:
        warnings.append(
            f"全范围分区扫描：{anchor_target.label} tile({ix},{iz}) 达到 native 上限，建议缩小 ANCHOR_TILE_SIZE。"
        )
    kept: list[dict[str, Any]] = []
    max_anchor_radius = search_radius + _anchor_reach(plan, anchor_target.id)
    for candidate in candidates:
        x = int(candidate["x"])
        z = int(candidate["z"])
        if not (min_x <= x < max_x and min_z <= z < max_z):
            continue
        distance = math.dist((center_x, center_z), (x, z))
        if distance > max_anchor_radius:
            continue
        item = {**candidate, "distance_to_center": round(distance, 1)}
        kept.append(item)
    kept = _filter_area_candidates(plan, kept, center_x, center_z)
    kept.sort(key=lambda point: math.dist((center_x, center_z), (point["x"], point["z"])))
    return kept, warnings


def _query_anchor_tiles(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    search_radius: int,
    plan: SearchPlan,
    anchor_target: Target,
    tiles: list[tuple[int, int]],
    max_anchor_radius: int,
    ring: int,
    progress: ProgressCallback | None = None,
) -> list[tuple[int, int, int, list[dict[str, Any]], list[str]]]:
    valid_tiles = [
        (tile_pos, ix, iz)
        for tile_pos, (ix, iz) in enumerate(tiles, start=1)
        if _tile_min_distance(ix, iz) <= max_anchor_radius
    ]
    if not valid_tiles:
        return []

    def query_one(tile: tuple[int, int, int]) -> tuple[int, int, int, list[dict[str, Any]], list[str]]:
        tile_pos, ix, iz = tile
        anchors, tile_warnings = _query_anchor_tile(
            seed,
            version,
            center_x,
            center_z,
            search_radius,
            plan,
            anchor_target,
            ix,
            iz,
        )
        return tile_pos, ix, iz, anchors, tile_warnings

    workers = min(_native_query_workers(), len(valid_tiles))
    results: list[tuple[int, int, int, list[dict[str, Any]], list[str]]] = []
    if workers > 1 and len(valid_tiles) > 1:
        completed = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mc-tile") as executor:
            futures = [executor.submit(query_one, tile) for tile in valid_tiles]
            for future in as_completed(futures):
                results.append(future.result())
                completed += 1
                if progress:
                    progress(
                        stage="anchor",
                        message=f"全范围分区扫描：第 {ring} 圈 tile 查询 {completed}/{len(valid_tiles)}，并行 {workers} 路",
                        radius=None,
                        checked=completed,
                        total=len(valid_tiles),
                    )
    else:
        for tile in valid_tiles:
            results.append(query_one(tile))

    results.sort(key=lambda item: (_tile_min_distance(item[1], item[2]), item[0]))
    return results


def _grid_key(x: int, z: int, cell: int) -> tuple[int, int]:
    return math.floor(x / cell), math.floor(z / cell)


def _build_grid(points: list[dict[str, Any]], radius: int) -> tuple[int, dict[tuple[int, int], list[dict[str, Any]]]]:
    cell = max(1, radius)
    buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for point in points:
        buckets.setdefault(_grid_key(point["x"], point["z"], cell), []).append(point)
    return cell, buckets


def _grid_query(
    grid: tuple[int, dict[tuple[int, int], list[dict[str, Any]]]],
    x: int,
    z: int,
    radius: int,
) -> list[dict[str, Any]]:
    cell, buckets = grid
    gx, gz = _grid_key(x, z, cell)
    found: list[dict[str, Any]] = []
    r2 = radius * radius
    span = math.ceil(radius / cell)
    for dx in range(-span, span + 1):
        for dz in range(-span, span + 1):
            for point in buckets.get((gx + dx, gz + dz), []):
                if (point["x"] - x) ** 2 + (point["z"] - z) ** 2 <= r2:
                    found.append(point)
    return found


def _point_fits_selected(plan: SearchPlan, point: dict[str, Any], selected: list[dict[str, Any]]) -> bool:
    for other in selected:
        distance = math.dist((point["x"], point["z"]), (other["x"], other["z"]))
        limit = _pair_distance_limit(plan, point["id"], other["id"])
        if limit is not None and distance > limit:
            return False
        minimum = _pair_min_distance(plan, point["id"], other["id"])
        if minimum is not None and distance < minimum:
            return False
    return _relative_layout_fits_points(plan, [*selected, point])


def _option_cap(target_count: int) -> int:
    if target_count <= 3:
        return 32
    if target_count == 4:
        return 18
    if target_count == 5:
        return 10
    return 6


def _cluster_sort_key(result: dict[str, Any], center_x: int, center_z: int, plan: SearchPlan) -> tuple[float, float, float]:
    anchor_id = plan.anchor_target_id or (plan.targets[0].id if plan.targets else "")
    anchor = next((p for p in result["targets"] if p["id"] == anchor_id), None)
    anchor_distance = math.dist((center_x, center_z), (anchor["x"], anchor["z"])) if anchor else result["distance"]
    site_distance = result["distance"]
    if plan.sort_by == "largest_area":
        measured_area = max((int(check.get("area") or 0) for check in result.get("biome_area_checks", [])), default=0)
        return (-float(measured_area), site_distance, result["max_pairwise_distance"])
    if plan.sort_by == "compact":
        return (result["max_pairwise_distance"], site_distance, round(anchor_distance, 1))
    if plan.sort_by == "site_distance":
        return (site_distance, result["max_pairwise_distance"], round(anchor_distance, 1))
    if plan.sort_by == "anchor_distance":
        return (round(anchor_distance, 1), result["max_pairwise_distance"], site_distance)
    primary = anchor_distance if plan.nearest_to_player else site_distance
    return (round(primary, 1), result["max_pairwise_distance"], site_distance)


def _evaluate_absence_constraints(
    seed: str,
    version: str,
    plan: SearchPlan,
    points: list[dict[str, Any]],
    site_x: int,
    site_z: int,
) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    for a_id, b_id, min_distance in _exclude_target_constraints(plan):
        a_points = [point for point in points if point["id"] == a_id]
        b_points = [point for point in points if point["id"] == b_id]
        refs = a_points or [{"id": "site", "label": "建议坐标", "x": site_x, "z": site_z}]
        if b_points:
            for ref in refs:
                for blocked in b_points:
                    distance = round(math.dist((ref["x"], ref["z"]), (blocked["x"], blocked["z"])), 1)
                    if distance < min_distance:
                        failures.append(
                            f"{ref['label']} 与 {blocked['label']} 距离 {distance}，小于排除阈值 {min_distance}"
                        )
            continue
        blocked_target = _target_from_id(b_id)
        if not blocked_target:
            failures.append(f"排除目标 {b_id} 不在后端目录中，无法验证。")
            continue
        for ref in refs:
            candidates, _mode, target_warnings = _call_cubiomes(
                seed,
                version,
                int(ref["x"]),
                int(ref["z"]),
                min_distance,
                1,
                blocked_target,
            )
            warnings.extend(target_warnings)
            if candidates:
                nearest = candidates[0]
                failures.append(
                    f"{ref['label']} 周围 {min_distance} 格内存在 {blocked_target.label}（{nearest['x']},{nearest['z']}）。"
                )
    return failures, warnings


def _count_constraint_items(plan: SearchPlan) -> list[tuple[Target, int, int]]:
    items: list[tuple[Target, int, int]] = []
    for raw in plan.count_constraints:
        if not isinstance(raw, dict):
            continue
        target_id = str(raw.get("target") or raw.get("id") or raw.get("name") or "")
        target = _target_from_id(target_id)
        if not target:
            continue
        min_count = max(1, int(_coerce_int(raw.get("min") or raw.get("min_count") or raw.get("count"), 1) or 1))
        within = int(
            _coerce_int(
                raw.get("within") if raw.get("within") is not None else raw.get("radius") if raw.get("radius") is not None else raw.get("distance"),
                plan.pairwise_max_distance or 1000,
            )
            or plan.pairwise_max_distance
            or 1000
        )
        items.append((target, min_count, max(1, within)))
    return items


def _evaluate_count_constraints(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    plan: SearchPlan,
    site_x: int,
    site_z: int,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    details: list[dict[str, Any]] = []
    failures: list[str] = []
    warnings: list[str] = []
    for target, min_count, within in _count_constraint_items(plan):
        limit = min(MAX_NATIVE_CANDIDATES, max(min_count, min_count * 2 + 8, 16))
        candidates, _mode, target_warnings = _call_cubiomes(seed, version, site_x, site_z, within, limit, target)
        warnings.extend(target_warnings)
        candidates = _filter_area_candidates(plan, candidates, center_x, center_z)
        candidates, biome_warnings = _filter_same_biome_candidates(seed, version, plan, target, candidates)
        warnings.extend(biome_warnings)
        found = len(candidates)
        details.append({"target": target.id, "label": target.label, "found": found, "min": min_count, "within": within})
        if found < min_count:
            failures.append(f"{site_x},{site_z} 周围 {within} 格内只找到 {found} 个{target.label}，少于要求的 {min_count} 个。")
    return details, failures, warnings


def _verify_point_search(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    plan: SearchPlan,
) -> tuple[list[dict[str, Any]], list[str]]:
    point = plan.verify_point or {}
    x = int(_coerce_int(point.get("x") if isinstance(point, dict) else None, center_x) or center_x)
    z = int(_coerce_int(point.get("z") if isinstance(point, dict) else None, center_z) or center_z)
    structure_targets = [target for target in plan.targets if target.kind == "structure"]
    if structure_targets:
        labels = "、".join(target.label for target in structure_targets)
        return [], [f"当前只支持核对坐标处的生物群系；是否位于结构内部暂不支持精确验证：{labels}。"]

    biome_name, warnings = _call_biome_at(seed, version, x, z)
    if not biome_name:
        return [], list(dict.fromkeys(warnings + ["没有拿到该坐标的生物群系结果。"]))

    requested_biomes = [target for target in plan.targets if target.kind == "biome"]
    satisfied = not requested_biomes or any(_biome_name_matches(biome_name, target.id) for target in requested_biomes)
    label = str(BIOMES.get(biome_name, {}).get("label") or biome_name)
    failure_reasons = []
    if requested_biomes and not satisfied:
        expected = "、".join(target.label for target in requested_biomes)
        failure_reasons.append(f"坐标 X {x}, Z {z} 的实际生物群系是 {label}，不是 {expected}。")
    result = {
        "type": "verify",
        "mode": "compatibility" if version.startswith("26.2") else "exact",
        "backend": "cubiomes",
        "suggested": {"x": x, "z": z},
        "distance": round(math.dist((center_x, center_z), (x, z)), 1),
        "targets": [
            {
                "id": biome_name,
                "kind": "biome",
                "label": label,
                "x": x,
                "z": z,
                "distance_to_center": round(math.dist((center_x, center_z), (x, z)), 1),
                "distance_to_site": 0,
                "verified": True,
            }
        ],
        "max_pairwise_distance": 0,
        "satisfied": satisfied,
        "failure_reasons": failure_reasons,
        "warnings": [],
        "count_checks": [],
        "map_url": map_url(seed, version, x, z),
        "searched_radius": 0,
    }
    return [result], list(dict.fromkeys(warnings))


def _evaluate_cluster(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    radius: int,
    plan: SearchPlan,
    points: list[dict[str, Any]],
) -> dict[str, Any]:
    points = _normalize_same_biome_points(plan, points)
    site_x, site_z = _site_from_points(points)
    max_pair = _max_pairwise(points)
    site_dist = round(math.dist((center_x, center_z), (site_x, site_z)), 1)
    failure_reasons = []
    warnings: list[str] = []
    same_biome_constraints = _same_biome_constraints(plan)
    required_by_structure: dict[str, set[str]] = {}
    for structure_id, biome_id in same_biome_constraints:
        required_by_structure.setdefault(structure_id, set()).add(biome_id)
    excluded_by_structure = {
        point["id"]: _excluded_biomes_for_target(plan, point["id"])
        for point in points
        if point.get("kind") == "structure"
    }
    excluded_by_structure = {target_id: excluded for target_id, excluded in excluded_by_structure.items() if excluded}
    structure_ids_to_check = set(required_by_structure) | set(excluded_by_structure)
    points_to_check = [
        (int(point["x"]), int(point["z"]))
        for point in points
        if point.get("id") in structure_ids_to_check and not point.get("biome_at_point")
    ]
    if points_to_check:
        biome_cache, biome_warnings = _call_biome_at_many(seed, version, points_to_check)
        warnings.extend(biome_warnings)
        for point in points:
            key = (int(point["x"]), int(point["z"]))
            if point.get("id") in structure_ids_to_check and key in biome_cache:
                point["biome_at_point"] = biome_cache[key]

    target_by_id = {target.id: target for target in plan.targets}
    for structure_id, biome_ids in required_by_structure.items():
        structure = next((point for point in points if point["id"] == structure_id), None)
        if not structure:
            continue
        biome_name = structure.get("biome_at_point")
        for biome_id in biome_ids:
            biome_target = target_by_id.get(biome_id)
            label = biome_target.label if biome_target else biome_id
            if not biome_name:
                failure_reasons.append(f"{structure['label']} 坐标未能校验所在生物群系，无法确认是否位于{label}。")
            elif not _biome_name_matches(biome_name, biome_id):
                actual = BIOMES.get(str(biome_name), {}).get("label") or biome_name
                failure_reasons.append(f"{structure['label']} 坐标所在生物群系是 {actual}，不是{label}。")

    layout_checks, layout_failures = _relative_layout_checks(plan, points)
    failure_reasons.extend(layout_failures)

    if plan.pairwise_max_distance and max_pair > plan.pairwise_max_distance:
        failure_reasons.append(f"目标两两最远距离 {max_pair} 超过 {plan.pairwise_max_distance}")
    if plan.area:
        if not _point_in_area(site_x, site_z, center_x, center_z, plan.area):
            failure_reasons.append("建议坐标不在用户限定的方向或坐标范围内。")
        for p in points:
            if not _point_in_area(int(p["x"]), int(p["z"]), center_x, center_z, plan.area):
                failure_reasons.append(f"{p['label']} 坐标不在用户限定的方向或坐标范围内。")
    adjacency_checks: list[dict[str, Any]] = []
    for adj in plan.adjacency:
        a_id = adj.get("a")
        b_id = adj.get("b")
        if adj.get("relation") == "same_biome":
            continue
        threshold = _adj_max_distance(adj, 512)
        min_threshold = _adj_min_distance(adj)
        a = next((p for p in points if p["id"] == a_id), None)
        b = next((p for p in points if p["id"] == b_id), None)
        if a and b:
            d = round(math.dist((a["x"], a["z"]), (b["x"], b["z"])), 1)
            passed = True
            if threshold is not None and d > threshold:
                passed = False
                failure_reasons.append(f"{a['label']} 与 {b['label']} 距离 {d}，未达到紧邻阈值 {threshold}")
            if min_threshold is not None and d < min_threshold:
                passed = False
                failure_reasons.append(f"{a['label']} 与 {b['label']} 距离 {d}，小于最小距离 {min_threshold}")
            adjacency_checks.append(
                {
                    "a": a_id,
                    "b": b_id,
                    "a_label": a["label"],
                    "b_label": b["label"],
                    "distance": d,
                    "max_distance": threshold,
                    "min_distance": min_threshold,
                    "passed": passed,
                }
            )
    for p in points:
        p["distance_to_center"] = round(math.dist((center_x, center_z), (p["x"], p["z"])), 1)
        p["distance_to_site"] = round(math.dist((p["x"], p["z"]), (site_x, site_z)), 1)
        excluded_biomes = _excluded_biomes_for_target(plan, p["id"])
        if excluded_biomes and p.get("biome_at_point") and _biome_matches_any(p.get("biome_at_point"), excluded_biomes):
            failure_reasons.append(f"{p['label']} 位于被排除的生物群系 {p['biome_at_point']}。")
    absence_failures, absence_warnings = _evaluate_absence_constraints(seed, version, plan, points, site_x, site_z)
    failure_reasons.extend(absence_failures)
    warnings.extend(absence_warnings)
    count_checks: list[dict[str, Any]] = []
    if not failure_reasons and plan.count_constraints:
        count_checks, count_failures, count_warnings = _evaluate_count_constraints(
            seed,
            version,
            center_x,
            center_z,
            plan,
            site_x,
            site_z,
        )
        failure_reasons.extend(count_failures)
        warnings.extend(count_warnings)
    biome_area_checks: list[dict[str, Any]] = []
    if not failure_reasons and plan.biome_area_constraints:
        biome_area_checks, area_failures, area_warnings = _evaluate_biome_area_constraints(seed, version, plan, points)
        failure_reasons.extend(area_failures)
        warnings.extend(area_warnings)
    return {
        "type": "cluster",
        "mode": "compatibility" if version.startswith("26.2") else "exact",
        "backend": "cubiomes",
        "suggested": {"x": site_x, "z": site_z},
        "distance": site_dist,
        "targets": points,
        "max_pairwise_distance": max_pair,
        "satisfied": not failure_reasons,
        "failure_reasons": failure_reasons,
        "warnings": list(dict.fromkeys(warnings)),
        "count_checks": count_checks,
        "biome_area_checks": biome_area_checks,
        "layout_checks": layout_checks,
        "adjacency_checks": adjacency_checks,
        "map_url": map_url(seed, version, site_x, site_z),
        "searched_radius": radius,
    }


def _build_cluster_candidates(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    radius: int,
    max_results: int,
    plan: SearchPlan,
    per_target: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    combos: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, int, int], ...]] = set()
    if not per_target or any(not candidates for candidates in per_target):
        return combos

    anchor_idx = _anchor_index(plan)
    anchor_target = plan.targets[anchor_idx]
    cap = _option_cap(len(per_target))
    max_keep = max(max_results * 40, 120)
    area_target_ids = {target.id for target, _min_area, _preference, _radius in _biome_area_constraint_items(plan)}
    grids: dict[tuple[int, int], tuple[int, dict[tuple[int, int], list[dict[str, Any]]]]] = {}

    for anchor in per_target[anchor_idx]:
        option_groups: list[tuple[int, list[dict[str, Any]]]] = []
        valid_anchor = True
        for idx, target_candidates in enumerate(per_target):
            if idx == anchor_idx:
                continue
            target = plan.targets[idx]
            limit = _pair_distance_limit(plan, anchor_target.id, target.id)
            if limit is not None:
                grid_key = (idx, limit)
                if grid_key not in grids:
                    grids[grid_key] = _build_grid(target_candidates, limit)
                options = _grid_query(grids[grid_key], anchor["x"], anchor["z"], limit)
            else:
                options = list(target_candidates)
            options.sort(key=lambda p: math.dist((anchor["x"], anchor["z"]), (p["x"], p["z"])))
            if not options:
                valid_anchor = False
                break
            option_limit = min(cap, 3) if target.id in area_target_ids else cap
            option_groups.append((idx, options[:option_limit]))
        if not valid_anchor:
            continue

        option_groups.sort(key=lambda item: len(item[1]))

        def backtrack(group_pos: int, selected: list[dict[str, Any]]) -> None:
            if group_pos >= len(option_groups):
                points = [dict(point) for point in selected]
                points.sort(key=lambda p: (p["kind"], p["id"], p["x"], p["z"]))
                key = tuple((p["id"], p["x"], p["z"]) for p in points)
                if key in seen:
                    return
                seen.add(key)
                result = _evaluate_cluster(seed, version, center_x, center_z, radius, plan, points)
                if result["satisfied"]:
                    combos.append(result)
                return
            _idx, options = option_groups[group_pos]
            for option in options:
                if not _point_fits_selected(plan, option, selected):
                    continue
                selected.append(dict(option))
                backtrack(group_pos + 1, selected)
                selected.pop()
                if len(combos) > max_keep * 2:
                    return

        backtrack(0, [dict(anchor)])
        if len(combos) > max_keep * 2:
            combos.sort(key=lambda r: _cluster_sort_key(r, center_x, center_z, plan))
            combos = combos[:max_keep]

    combos.sort(key=lambda r: _cluster_sort_key(r, center_x, center_z, plan))
    return combos


def _spm_point_key(point: dict[str, Any]) -> tuple[str, int, int]:
    return (str(point["id"]), int(point["x"]), int(point["z"]))


def _spm_plan_edges(plan: SearchPlan) -> list[dict[str, Any]]:
    target_ids = [target.id for target in plan.targets]
    edges: dict[tuple[str, str], dict[str, Any]] = {}

    def add_edge(a_id: str, b_id: str, max_distance: int | None, min_distance: int | None = None, source: str = "constraint") -> None:
        if a_id == b_id or a_id not in target_ids or b_id not in target_ids:
            return
        left, right = sorted((a_id, b_id))
        key = (left, right)
        edge = edges.setdefault(key, {"a": left, "b": right, "max": None, "min": None, "sources": []})
        if max_distance is not None:
            edge["max"] = min(edge["max"], int(max_distance)) if edge["max"] is not None else int(max_distance)
        if min_distance is not None:
            edge["min"] = max(edge["min"], int(min_distance)) if edge["min"] is not None else int(min_distance)
        edge["sources"].append(source)

    for adj in plan.adjacency:
        if adj.get("relation") == "same_biome":
            continue
        a_id = str(adj.get("a"))
        b_id = str(adj.get("b"))
        add_edge(a_id, b_id, _adj_max_distance(adj, 512), _adj_min_distance(adj), "adjacency")

    for constraint in _relative_layout_constraints(plan):
        add_edge(
            constraint["center"],
            constraint["back"],
            constraint["back_max_distance"],
            None,
            "relative_layout",
        )
        add_edge(
            constraint["center"],
            constraint["front"],
            constraint["front_max_distance"],
            None,
            "relative_layout",
        )

    if plan.pairwise_max_distance:
        for i, a_id in enumerate(target_ids):
            for b_id in target_ids[i + 1 :]:
                add_edge(a_id, b_id, plan.pairwise_max_distance, None, "pairwise")

    for a_id, b_id, min_distance in _exclude_target_constraints(plan):
        if a_id in target_ids and b_id in target_ids:
            add_edge(a_id, b_id, None, min_distance, "exclude")

    return list(edges.values())


def _spm_candidate_limit(plan: SearchPlan, target: Target) -> int:
    if len(plan.targets) <= 2 and not plan.pairwise_max_distance:
        return _candidate_limit(1)
    return MAX_NATIVE_CANDIDATES


def _spm_deferred_biome_ids(plan: SearchPlan, edges: list[dict[str, Any]]) -> set[str]:
    target_by_id = {target.id: target for target in plan.targets}
    materialized_biomes = _same_biome_materialized_targets(plan)
    deferred: set[str] = set()
    for edge in edges:
        if edge.get("max") is None:
            continue
        a = target_by_id.get(str(edge["a"]))
        b = target_by_id.get(str(edge["b"]))
        if not a or not b:
            continue
        if a.kind == "biome" and a.id not in materialized_biomes and b.kind == "structure":
            deferred.add(a.id)
        if b.kind == "biome" and b.id not in materialized_biomes and a.kind == "structure":
            deferred.add(b.id)
    return deferred


def _spm_materialize_same_biome_layers(plan: SearchPlan, layers: dict[str, list[dict[str, Any]]]) -> None:
    target_by_id = {target.id: target for target in plan.targets}
    for biome_id, structure_id in _same_biome_materialized_targets(plan).items():
        biome_target = target_by_id.get(biome_id)
        if not biome_target:
            continue
        layers[biome_id] = [
            _materialize_same_biome_target(biome_target, point, structure_id)
            for point in layers.get(structure_id, [])
        ]


def _spm_load_candidate_layers(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    search_radius: int,
    plan: SearchPlan,
    progress: ProgressCallback | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[str], bool]:
    warnings: list[str] = []
    edges = _spm_plan_edges(plan)
    deferred_biomes = _spm_deferred_biome_ids(plan, edges)
    materialized_biomes = _same_biome_materialized_targets(plan)
    layers: dict[str, list[dict[str, Any]]] = {target.id: [] for target in plan.targets}
    queries: list[tuple[int, int, int, int, Target]] = []
    refs: list[str] = []

    for target in plan.targets:
        if target.kind == "biome" and (target.id in materialized_biomes or target.id in deferred_biomes):
            continue
        queries.append((center_x, center_z, search_radius, _spm_candidate_limit(plan, target), target))
        refs.append(target.id)

    if progress:
        progress(
            stage="spm",
            message=f"SPM：正在批量读取 {len(queries)} 个目标层候选，范围 {_format_blocks(search_radius)}",
            radius=search_radius,
            checked=0,
            total=len(queries),
        )

    for target_id, (candidates, _mode, target_warnings) in zip(refs, _call_cubiomes_batch(seed, version, queries)):
        warnings.extend(target_warnings)
        target = next((item for item in plan.targets if item.id == target_id), None)
        if not target:
            continue
        candidates = _filter_area_candidates(plan, candidates, center_x, center_z)
        layers[target_id] = candidates
        if len(candidates) >= MAX_NATIVE_CANDIDATES:
            warnings.append(f"SPM 候选层「{target.label}」达到 native 上限 {MAX_NATIVE_CANDIDATES}，结果仍按当前候选池计算。")

    _spm_materialize_same_biome_layers(plan, layers)

    missing = [
        target.label
        for target in plan.targets
        if not layers.get(target.id) and target.id not in deferred_biomes and target.id not in materialized_biomes
    ]
    if missing:
        warnings.append(f"SPM：候选层为空，无法形成组合：{'、'.join(missing)}。")
        return layers, list(dict.fromkeys(warnings)), False
    return layers, list(dict.fromkeys(warnings)), True


def _spm_build_edge_table(
    plan: SearchPlan,
    layers: dict[str, list[dict[str, Any]]],
    edge: dict[str, Any],
) -> list[tuple[tuple[str, int, int], tuple[str, int, int]]]:
    a_id = str(edge["a"])
    b_id = str(edge["b"])
    max_distance = edge.get("max")
    min_distance = edge.get("min")
    a_points = layers.get(a_id, [])
    b_points = layers.get(b_id, [])
    if not a_points or not b_points:
        return []

    pairs: list[tuple[tuple[str, int, int], tuple[str, int, int]]] = []
    if max_distance is not None:
        grid = _build_grid(b_points, max(1, int(max_distance)))
        for a in a_points:
            for b in _grid_query(grid, int(a["x"]), int(a["z"]), int(max_distance)):
                distance2 = (int(a["x"]) - int(b["x"])) ** 2 + (int(a["z"]) - int(b["z"])) ** 2
                if min_distance is not None and distance2 < int(min_distance) ** 2:
                    continue
                pairs.append((_spm_point_key(a), _spm_point_key(b)))
        return pairs

    if min_distance is not None:
        min2 = int(min_distance) ** 2
        for a in a_points:
            for b in b_points:
                distance2 = (int(a["x"]) - int(b["x"])) ** 2 + (int(a["z"]) - int(b["z"])) ** 2
                if distance2 >= min2:
                    pairs.append((_spm_point_key(a), _spm_point_key(b)))
    return pairs


def _spm_prune_layers(
    layers: dict[str, list[dict[str, Any]]],
    edge_tables: dict[tuple[str, str], list[tuple[tuple[str, int, int], tuple[str, int, int]]]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[tuple[str, str], list[tuple[tuple[str, int, int], tuple[str, int, int]]]]]:
    pruned = {target_id: list(points) for target_id, points in layers.items()}
    changed = True
    while changed:
        changed = False
        allowed_by_target: dict[str, set[tuple[str, int, int]]] = {target_id: set(_spm_point_key(p) for p in points) for target_id, points in pruned.items()}
        for (a_id, b_id), pairs in edge_tables.items():
            a_allowed = {a_key for a_key, _b_key in pairs}
            b_allowed = {b_key for _a_key, b_key in pairs}
            allowed_by_target[a_id] = allowed_by_target[a_id] & a_allowed
            allowed_by_target[b_id] = allowed_by_target[b_id] & b_allowed

        for target_id, points in list(pruned.items()):
            allowed = allowed_by_target.get(target_id)
            if allowed is None:
                continue
            kept = [point for point in points if _spm_point_key(point) in allowed]
            if len(kept) != len(points):
                pruned[target_id] = kept
                changed = True

        if changed:
            live = {target_id: set(_spm_point_key(p) for p in points) for target_id, points in pruned.items()}
            for key, pairs in list(edge_tables.items()):
                a_id, b_id = key
                edge_tables[key] = [
                    (a_key, b_key)
                    for a_key, b_key in pairs
                    if a_key in live.get(a_id, set()) and b_key in live.get(b_id, set())
                ]
    return pruned, edge_tables


def _spm_make_indexes(
    layers: dict[str, list[dict[str, Any]]],
    edge_tables: dict[tuple[str, str], list[tuple[tuple[str, int, int], tuple[str, int, int]]]],
) -> tuple[
    dict[tuple[str, int, int], dict[str, Any]],
    dict[tuple[str, str], dict[tuple[str, int, int], list[tuple[str, int, int]]]],
]:
    point_by_key: dict[tuple[str, int, int], dict[str, Any]] = {}
    for points in layers.values():
        for point in points:
            point_by_key[_spm_point_key(point)] = point

    adjacency: dict[tuple[str, str], dict[tuple[str, int, int], list[tuple[str, int, int]]]] = {}
    for (a_id, b_id), pairs in edge_tables.items():
        forward: dict[tuple[str, int, int], list[tuple[str, int, int]]] = {}
        backward: dict[tuple[str, int, int], list[tuple[str, int, int]]] = {}
        for a_key, b_key in pairs:
            forward.setdefault(a_key, []).append(b_key)
            backward.setdefault(b_key, []).append(a_key)
        adjacency[(a_id, b_id)] = forward
        adjacency[(b_id, a_id)] = backward
    return point_by_key, adjacency


def _spm_edge_options(
    edge: dict[str, Any],
    assigned: dict[str, tuple[str, int, int]],
    edge_tables: dict[tuple[str, str], list[tuple[tuple[str, int, int], tuple[str, int, int]]]],
    adjacency: dict[tuple[str, str], dict[tuple[str, int, int], list[tuple[str, int, int]]]],
) -> list[tuple[tuple[str, int, int], tuple[str, int, int]]]:
    a_id = str(edge["a"])
    b_id = str(edge["b"])
    a_key = assigned.get(a_id)
    b_key = assigned.get(b_id)
    if a_key and b_key:
        return [(a_key, b_key)] if b_key in adjacency.get((a_id, b_id), {}).get(a_key, []) else []
    if a_key:
        return [(a_key, candidate) for candidate in adjacency.get((a_id, b_id), {}).get(a_key, [])]
    if b_key:
        return [(candidate, b_key) for candidate in adjacency.get((b_id, a_id), {}).get(b_key, [])]
    return edge_tables.get((a_id, b_id), [])


def _spm_result_sort_key_from_assignment(
    assignment: dict[str, tuple[str, int, int]],
    point_by_key: dict[tuple[str, int, int], dict[str, Any]],
    center_x: int,
    center_z: int,
    plan: SearchPlan,
) -> tuple[float, float]:
    points = [point_by_key[key] for key in assignment.values() if key in point_by_key]
    if not points:
        return (math.inf, math.inf)
    anchor_id = plan.anchor_target_id or (plan.targets[0].id if plan.targets else "")
    anchor = next((p for p in points if p["id"] == anchor_id), None)
    if anchor:
        primary = math.dist((center_x, center_z), (anchor["x"], anchor["z"]))
    else:
        site_x, site_z = _site_from_points(points)
        primary = math.dist((center_x, center_z), (site_x, site_z))
    return (round(primary, 1), _max_pairwise(points))


def _spm_join_results(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    search_radius: int,
    max_results: int,
    plan: SearchPlan,
    layers: dict[str, list[dict[str, Any]]],
    edges: list[dict[str, Any]],
    edge_tables: dict[tuple[str, str], list[tuple[tuple[str, int, int], tuple[str, int, int]]]],
) -> list[dict[str, Any]]:
    point_by_key, adjacency = _spm_make_indexes(layers, edge_tables)
    target_ids = [target.id for target in plan.targets]
    results: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, int, int], ...]] = set()
    max_keep = max(max_results * 20, 80)

    def complete_assignment(assigned: dict[str, tuple[str, int, int]]) -> None:
        missing = [target_id for target_id in target_ids if target_id not in assigned]
        if missing:
            ordered_missing = sorted(missing, key=lambda item: len(layers.get(item, [])))
            def fill(pos: int) -> None:
                if pos >= len(ordered_missing):
                    complete_assignment(assigned)
                    return
                target_id = ordered_missing[pos]
                options = sorted(
                    layers.get(target_id, []),
                    key=lambda p: math.dist((center_x, center_z), (p["x"], p["z"])),
                )
                for option in options[:_option_cap(len(target_ids))]:
                    if _point_fits_selected(plan, option, [point_by_key[key] for key in assigned.values() if key in point_by_key]):
                        assigned[target_id] = _spm_point_key(option)
                        fill(pos + 1)
                        assigned.pop(target_id, None)
            fill(0)
            return

        key = tuple(sorted(assigned.values()))
        if key in seen:
            return
        seen.add(key)
        points = [dict(point_by_key[assigned[target.id]]) for target in plan.targets if assigned.get(target.id) in point_by_key]
        result = _evaluate_cluster(seed, version, center_x, center_z, search_radius, plan, points)
        if result["satisfied"]:
            results.append(result)

    def backtrack(remaining_edges: list[dict[str, Any]], assigned: dict[str, tuple[str, int, int]]) -> None:
        if len(results) >= max_keep:
            return
        if not remaining_edges:
            complete_assignment(assigned)
            return

        best_edge = None
        best_options: list[tuple[tuple[str, int, int], tuple[str, int, int]]] = []
        best_score = (math.inf, math.inf)
        for edge in remaining_edges:
            options = _spm_edge_options(edge, assigned, edge_tables, adjacency)
            assigned_count = int(str(edge["a"]) in assigned) + int(str(edge["b"]) in assigned)
            score = (0 if assigned_count else 1, len(options))
            if score < best_score:
                best_score = score
                best_edge = edge
                best_options = options
        if best_edge is None or not best_options:
            return

        next_edges = [edge for edge in remaining_edges if edge is not best_edge]
        a_id = str(best_edge["a"])
        b_id = str(best_edge["b"])
        best_options = sorted(
            best_options,
            key=lambda pair: _spm_result_sort_key_from_assignment(
                {**assigned, a_id: pair[0], b_id: pair[1]},
                point_by_key,
                center_x,
                center_z,
                plan,
            ),
        )
        for a_key, b_key in best_options:
            old_a = assigned.get(a_id)
            old_b = assigned.get(b_id)
            if old_a is not None and old_a != a_key:
                continue
            if old_b is not None and old_b != b_key:
                continue
            a_point = point_by_key.get(a_key)
            b_point = point_by_key.get(b_key)
            if not a_point or not b_point:
                continue
            selected = [point_by_key[key] for key in assigned.values() if key in point_by_key]
            if old_a is None and not _point_fits_selected(plan, a_point, selected):
                continue
            if old_b is None and not _point_fits_selected(plan, b_point, selected + ([a_point] if old_a is None else [])):
                continue
            assigned[a_id] = a_key
            assigned[b_id] = b_key
            backtrack(next_edges, assigned)
            if old_a is None:
                assigned.pop(a_id, None)
            else:
                assigned[a_id] = old_a
            if old_b is None:
                assigned.pop(b_id, None)
            else:
                assigned[b_id] = old_b
            if len(results) >= max_keep:
                return

    backtrack(sorted(edges, key=lambda edge: len(edge_tables.get((str(edge["a"]), str(edge["b"])), []))), {})
    results.sort(key=lambda result: _cluster_sort_key(result, center_x, center_z, plan))
    return results[:max_results]


def _spm_resolve_deferred_biomes(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    max_results: int,
    plan: SearchPlan,
    layers: dict[str, list[dict[str, Any]]],
    edges: list[dict[str, Any]],
    edge_tables: dict[tuple[str, str], list[tuple[tuple[str, int, int], tuple[str, int, int]]]],
    progress: ProgressCallback | None = None,
) -> list[str]:
    warnings: list[str] = []
    target_by_id = {target.id: target for target in plan.targets}
    deferred_biomes = _spm_deferred_biome_ids(plan, edges)
    anchor_id = plan.anchor_target_id or (plan.targets[0].id if plan.targets else "")

    for biome_id in sorted(deferred_biomes, key=lambda item: len(layers.get(item, [])) or 10**9):
        if layers.get(biome_id):
            continue
        biome_target = target_by_id.get(biome_id)
        if not biome_target:
            continue

        incident: list[tuple[int, int, str, dict[str, Any]]] = []
        for edge in edges:
            a_id = str(edge["a"])
            b_id = str(edge["b"])
            if biome_id not in {a_id, b_id} or edge.get("max") is None:
                continue
            other_id = b_id if a_id == biome_id else a_id
            other_target = target_by_id.get(other_id)
            if not other_target or not layers.get(other_id):
                continue
            if other_target.kind != "structure":
                continue
            incident.append((int(edge["max"]), len(layers.get(other_id, [])), other_id, edge))

        if not incident:
            continue

        radius, _layer_size, source_id, edge = min(incident)
        source_points = sorted(
            layers.get(source_id, []),
            key=lambda point: math.dist((center_x, center_z), (point["x"], point["z"])),
        )
        if not source_points:
            continue

        candidate_by_key: dict[tuple[str, int, int], dict[str, Any]] = {}
        pairs: list[tuple[tuple[str, int, int], tuple[str, int, int]]] = []
        matched_source_distances: list[float] = []
        chunk_size = ANCHOR_NEIGHBOR_BATCH_SIZE
        allow_distance_stop = (
            source_id == anchor_id
            and len(plan.targets) <= 2
            and not _same_biome_constraints(plan)
            and not plan.exclude_biomes
            and not plan.exclude_targets
            and not plan.count_constraints
            and not plan.area
        )

        for start in range(0, len(source_points), chunk_size):
            chunk = source_points[start : start + chunk_size]
            if not chunk:
                continue
            if progress:
                progress(
                    stage="spm",
                    message=(
                        f"SPM：正在 {len(source_points)} 个{target_by_id[source_id].label}候选附近局部查询{biome_target.label}，"
                        f"局部范围 {_format_blocks(radius)}，已处理 {start}"
                    ),
                    radius=radius,
                    checked=start,
                    total=len(source_points),
                )

            queries = [
                (int(point["x"]), int(point["z"]), radius, NEAR_TARGET_LIMIT, biome_target)
                for point in chunk
            ]
            for source_point, (candidates, _mode, target_warnings) in zip(chunk, _call_cubiomes_batch(seed, version, queries)):
                warnings.extend(target_warnings)
                candidates = _filter_area_candidates(plan, candidates, center_x, center_z)
                if candidates:
                    matched_source_distances.append(math.dist((center_x, center_z), (source_point["x"], source_point["z"])))
                source_key = _spm_point_key(source_point)
                for candidate in candidates:
                    distance2 = (int(source_point["x"]) - int(candidate["x"])) ** 2 + (
                        int(source_point["z"]) - int(candidate["z"])
                    ) ** 2
                    if distance2 > radius * radius:
                        continue
                    candidate_key = _spm_point_key(candidate)
                    candidate_by_key.setdefault(candidate_key, candidate)
                    if str(edge["a"]) == source_id:
                        pairs.append((source_key, candidate_key))
                    else:
                        pairs.append((candidate_key, source_key))

            if allow_distance_stop and len(matched_source_distances) >= max_results:
                worst_kept = sorted(matched_source_distances)[max_results - 1]
                next_pos = start + len(chunk)
                if next_pos >= len(source_points):
                    break
                next_distance = math.dist(
                    (center_x, center_z),
                    (source_points[next_pos]["x"], source_points[next_pos]["z"]),
                )
                if next_distance >= worst_kept:
                    warnings.append(
                        f"SPM：{biome_target.label}局部查询按锚点距离下界提前停止；后续{target_by_id[source_id].label}不会产生更近结果。"
                    )
                    break

        layers[biome_id] = list(candidate_by_key.values())
        key = (str(edge["a"]), str(edge["b"]))
        if pairs:
            edge_tables[key] = list(dict.fromkeys(pairs))
        warnings.append(
            f"SPM：已在{target_by_id[source_id].label}附近局部补齐{biome_target.label}候选 {len(layers[biome_id])} 个。"
        )

    return list(dict.fromkeys(warnings))


def _search_by_spm(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    search_radius: int,
    max_results: int,
    plan: SearchPlan,
    progress: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    if len(plan.targets) < 2:
        return [], [], False
    edges = [edge for edge in _spm_plan_edges(plan) if edge.get("max") is not None]
    if not edges:
        return [], [], False

    warnings: list[str] = ["SPM 引擎：使用候选层 + 空间网格 range join + 动态多路 join。"]
    layers, layer_warnings, ok = _spm_load_candidate_layers(seed, version, center_x, center_z, search_radius, plan, progress)
    warnings.extend(layer_warnings)
    if not ok:
        return [], list(dict.fromkeys(warnings)), True

    if progress:
        progress(
            stage="spm",
            message=f"SPM：正在生成 {len(edges)} 条距离约束边表",
            radius=search_radius,
            checked=0,
            total=len(edges),
        )

    edge_tables: dict[tuple[str, str], list[tuple[tuple[str, int, int], tuple[str, int, int]]]] = {}
    pending_edges: list[dict[str, Any]] = []
    for idx, edge in enumerate(edges, start=1):
        key = (str(edge["a"]), str(edge["b"]))
        if not layers.get(key[0]) or not layers.get(key[1]):
            pending_edges.append(edge)
            continue
        edge_tables[key] = _spm_build_edge_table(plan, layers, edge)
        if progress:
            progress(
                stage="spm",
                message=(
                    f"SPM：边表 {edge['a']} - {edge['b']} 生成 {len(edge_tables[key])} 对候选"
                ),
                radius=search_radius,
                checked=idx,
                total=len(edges),
            )
        if not edge_tables[key]:
            warnings.append(f"SPM：约束 {edge['a']} - {edge['b']} 没有候选边，已证明当前候选层内无组合。")
            return [], list(dict.fromkeys(warnings)), True

    if edge_tables:
        layers, edge_tables = _spm_prune_layers(layers, edge_tables)
        _spm_materialize_same_biome_layers(plan, layers)

    deferred_warnings = _spm_resolve_deferred_biomes(
        seed,
        version,
        center_x,
        center_z,
        max_results,
        plan,
        layers,
        edges,
        edge_tables,
        progress=progress,
    )
    warnings.extend(deferred_warnings)
    _spm_materialize_same_biome_layers(plan, layers)

    for edge in pending_edges:
        key = (str(edge["a"]), str(edge["b"]))
        if key in edge_tables:
            continue
        if not layers.get(key[0]) or not layers.get(key[1]):
            missing = []
            if not layers.get(key[0]):
                target = next((item for item in plan.targets if item.id == key[0]), None)
                missing.append(target.label if target else key[0])
            if not layers.get(key[1]):
                target = next((item for item in plan.targets if item.id == key[1]), None)
                missing.append(target.label if target else key[1])
            warnings.append(f"SPM：约束 {edge['a']} - {edge['b']} 缺少候选层：{'、'.join(missing)}。")
            return [], list(dict.fromkeys(warnings)), True
        edge_tables[key] = _spm_build_edge_table(plan, layers, edge)
        if not edge_tables[key]:
            warnings.append(f"SPM：约束 {edge['a']} - {edge['b']} 没有候选边，已证明当前候选层内无组合。")
            return [], list(dict.fromkeys(warnings)), True

    layers, edge_tables = _spm_prune_layers(layers, edge_tables)
    _spm_materialize_same_biome_layers(plan, layers)
    empty = [target.label for target in plan.targets if not layers.get(target.id)]
    if empty:
        warnings.append(f"SPM：边表剪枝后候选层为空：{'、'.join(empty)}。")
        return [], list(dict.fromkeys(warnings)), True

    if progress:
        progress(
            stage="spm",
            message="SPM：正在按最小边表优先做多路 join",
            radius=search_radius,
            checked=sum(len(items) for items in edge_tables.values()),
            total=None,
        )

    results = _spm_join_results(seed, version, center_x, center_z, search_radius, max_results, plan, layers, edges, edge_tables)
    if not results:
        warnings.append("SPM：候选边表已全部连接验证，没有找到满足全部约束的组合。")
    return results, list(dict.fromkeys(warnings)), True


def _search_by_hub_anchor(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    search_radius: int,
    max_results: int,
    plan: SearchPlan,
    progress: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    anchor_idx = _anchor_index(plan)
    hub_idx = _hub_index(plan, anchor_idx)
    if hub_idx is None:
        return [], warnings

    anchor_target = plan.targets[anchor_idx]
    hub_target = plan.targets[hub_idx]
    if anchor_target.kind != "structure" or hub_target.kind != "structure":
        return [], warnings

    anchor_to_hub_radius = _search_radius_from_anchor(plan, anchor_target.id, hub_target.id)
    materialized_biomes = _same_biome_materialized_targets(plan)
    anchor_reach = _anchor_reach(plan, anchor_target.id)
    anchor_limits = _anchor_scan_limits(plan)
    seen_anchor_keys: set[tuple[int, int]] = set()
    accepted: list[dict[str, Any]] = []
    seen_combos: set[tuple[tuple[str, int, int], ...]] = set()

    for anchor_limit in anchor_limits:
        if progress:
            progress(
                stage="anchor",
                message=(
                    f"图搜索：读取距离玩家最近的 {anchor_limit} 个{anchor_target.label}候选，"
                    f"再跳到约束中心「{hub_target.label}」"
                ),
                radius=search_radius,
                checked=len(seen_anchor_keys),
                total=anchor_limit,
            )
        anchors, _mode, anchor_warnings = _call_cubiomes(
            seed, version, center_x, center_z, search_radius, anchor_limit, anchor_target
        )
        warnings.extend(anchor_warnings)
        anchors = _filter_area_candidates(plan, anchors, center_x, center_z)
        if not _delay_anchor_biome_filter(plan, anchor_target.id):
            anchors, biome_warnings = _filter_same_biome_candidates(seed, version, plan, anchor_target, anchors)
            warnings.extend(biome_warnings)
        new_anchors = [anchor for anchor in anchors if (anchor["x"], anchor["z"]) not in seen_anchor_keys]
        processed_before = len(seen_anchor_keys)
        for anchor in new_anchors:
            seen_anchor_keys.add((anchor["x"], anchor["z"]))

        for chunk_start in range(0, len(new_anchors), ANCHOR_NEIGHBOR_BATCH_SIZE):
            anchor_chunk = new_anchors[chunk_start : chunk_start + ANCHOR_NEIGHBOR_BATCH_SIZE]
            if not anchor_chunk:
                continue
            frontier = max(math.dist((center_x, center_z), (anchor["x"], anchor["z"])) for anchor in anchor_chunk)
            checked = min(len(seen_anchor_keys), processed_before + chunk_start + len(anchor_chunk))
            if progress:
                progress(
                    stage="anchor",
                    message=(
                        f"图搜索：正在用{anchor_target.label}预筛{hub_target.label}，"
                        f"已扫到距玩家约 {_format_blocks(int(frontier))}"
                    ),
                    radius=int(frontier),
                    checked=checked,
                    total=len(seen_anchor_keys),
                )

            hub_queries = [
                (anchor["x"], anchor["z"], anchor_to_hub_radius, HUB_CANDIDATE_LIMIT, hub_target)
                for anchor in anchor_chunk
            ]
            hub_query_results = _call_cubiomes_batch(seed, version, hub_queries)
            expansion_queries: list[tuple[int, int, int, int, Target]] = []
            expansion_refs: list[tuple[int, int, int]] = []
            per_anchor_hubs: list[list[dict[str, Any]]] = [[] for _anchor in anchor_chunk]

            for anchor_pos, (hub_candidates, _hub_mode, hub_warnings) in enumerate(hub_query_results):
                warnings.extend(hub_warnings)
                hub_candidates = _filter_area_candidates(plan, hub_candidates, center_x, center_z)
                per_anchor_hubs[anchor_pos] = hub_candidates[:HUB_CANDIDATE_LIMIT]
                for hub_pos, hub in enumerate(per_anchor_hubs[anchor_pos]):
                    for idx, target in enumerate(plan.targets):
                        if idx in (anchor_idx, hub_idx):
                            continue
                        if target.kind == "biome" and target.id in materialized_biomes:
                            continue
                        near_radius = _search_radius_from_anchor(plan, hub_target.id, target.id)
                        expansion_queries.append((hub["x"], hub["z"], near_radius, NEAR_TARGET_LIMIT, target))
                        expansion_refs.append((anchor_pos, hub_pos, idx))

            if progress:
                progress(
                    stage="anchor",
                    message=(
                        f"图搜索：已找到 {sum(len(items) for items in per_anchor_hubs)} 个可能的{hub_target.label}中心，"
                        "正在检查中心附近目标"
                    ),
                    radius=int(frontier),
                    checked=checked,
                    total=len(seen_anchor_keys),
                )

            expansions: dict[tuple[int, int], dict[int, list[dict[str, Any]]]] = {}
            for (anchor_pos, hub_pos, idx), (candidates, _mode, target_warnings) in zip(
                expansion_refs,
                _call_cubiomes_batch(seed, version, expansion_queries),
            ):
                warnings.extend(target_warnings)
                target = plan.targets[idx]
                candidates = _filter_area_candidates(plan, candidates, center_x, center_z)
                expansions.setdefault((anchor_pos, hub_pos), {})[idx] = candidates

            for anchor_pos, anchor in enumerate(anchor_chunk):
                for hub_pos, hub in enumerate(per_anchor_hubs[anchor_pos]):
                    per_target: list[list[dict[str, Any]]] = [[] for _target in plan.targets]
                    per_target[anchor_idx] = [dict(anchor)]
                    per_target[hub_idx] = [dict(hub)]
                    missing = False
                    for idx, target in enumerate(plan.targets):
                        if idx in (anchor_idx, hub_idx):
                            continue
                        if target.kind == "biome" and target.id in materialized_biomes:
                            continue
                        candidates = expansions.get((anchor_pos, hub_pos), {}).get(idx, [])
                        if not candidates:
                            missing = True
                            break
                        per_target[idx] = candidates
                    if missing:
                        continue
                    per_target, biome_filter_warnings = _apply_same_biome_filters(seed, version, plan, per_target)
                    warnings.extend(biome_filter_warnings)
                    if any(not candidates for candidates in per_target):
                        continue

                    combos = _build_cluster_candidates(seed, version, center_x, center_z, search_radius, max_results, plan, per_target)
                    for combo in combos:
                        key = tuple((p["id"], p["x"], p["z"]) for p in sorted(combo["targets"], key=lambda p: (p["id"], p["x"], p["z"])))
                        if key in seen_combos:
                            continue
                        seen_combos.add(key)
                        accepted.append(combo)
                    accepted.sort(key=lambda result: _cluster_sort_key(result, center_x, center_z, plan))

            if len(accepted) >= max_results:
                worst_primary = _cluster_sort_key(accepted[max_results - 1], center_x, center_z, plan)[0]
                lower_bound = max(0, int(frontier) - anchor_reach)
                if lower_bound <= worst_primary:
                    if progress:
                        progress(
                            stage="anchor",
                            message=(
                                f"图搜索已找到候选，继续排除更近可能；"
                                f"锚点边界 {_format_blocks(int(frontier))}，理论下界 {_format_blocks(int(lower_bound))}"
                            ),
                            radius=int(frontier),
                            checked=checked,
                            total=len(seen_anchor_keys),
                        )
                    continue
                if progress:
                    progress(
                        stage="done",
                        message="图搜索已找到满足条件的候选区域；后续区域按距离下界剪枝",
                        radius=int(frontier),
                        checked=len(seen_anchor_keys),
                        total=len(seen_anchor_keys),
                    )
                return accepted[:max_results], list(dict.fromkeys(warnings))

    if seen_anchor_keys:
        warnings.append(
            f"图搜索阶段已检查距离玩家最近的 {len(seen_anchor_keys)} 个{anchor_target.label}候选；未命中时会继续常规候选扩展。"
        )
    return accepted[:max_results], list(dict.fromkeys(warnings))


def _search_by_anchor(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    search_radius: int,
    max_results: int,
    plan: SearchPlan,
    progress: ProgressCallback | None = None,
    exhaustive_override: bool | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    exhaustive = _is_strict_anchor_constrained_plan(plan) if exhaustive_override is None else exhaustive_override
    anchor_idx = _exhaustive_scan_anchor_index(plan) if exhaustive else _anchor_index(plan)
    anchor_target = plan.targets[anchor_idx]
    if anchor_target.kind != "structure":
        return [], warnings

    anchor_limits = _anchor_scan_limits(plan)
    seen_anchor_keys: set[tuple[int, int]] = set()
    accepted: list[dict[str, Any]] = []
    seen_combos: set[tuple[tuple[str, int, int], ...]] = set()
    materialized_biomes = _same_biome_materialized_targets(plan)
    anchor_reach = _anchor_reach(plan, anchor_target.id)

    target_order = [
        idx
        for idx, target in enumerate(plan.targets)
        if idx != anchor_idx and not (target.kind == "biome" and target.id in materialized_biomes)
    ]
    target_order.sort(
        key=lambda idx: (
            0 if plan.targets[idx].kind == "structure" else 1,
            _search_radius_from_anchor(plan, anchor_target.id, plan.targets[idx].id),
            ANCHOR_RARITY_SCORE.get(plan.targets[idx].id, 10),
        )
    )
    near_radii = [_search_radius_from_anchor(plan, anchor_target.id, plan.targets[idx].id) for idx in target_order]
    constraint_radius = max(near_radii) if near_radii else 0

    def process_anchor_batch(
        anchors: list[dict[str, Any]],
        processed_before: int,
        total: int | None,
        allow_chunk_stop: bool,
        source_label: str,
    ) -> bool:
        chunk_size = 16 if plan.biome_area_constraints else ANCHOR_NEIGHBOR_BATCH_SIZE
        for chunk_start in range(0, len(anchors), chunk_size):
            anchor_chunk = anchors[chunk_start : chunk_start + chunk_size]
            per_anchor_targets: list[list[list[dict[str, Any]]]] = []
            for anchor_pos, anchor in enumerate(anchor_chunk):
                per_target: list[list[dict[str, Any]]] = [[] for _target in plan.targets]
                per_target[anchor_idx] = [dict(anchor)]
                per_anchor_targets.append(per_target)

            checked = min(len(seen_anchor_keys), processed_before + chunk_start + len(anchor_chunk))
            frontier = max(
                math.dist((center_x, center_z), (anchor["x"], anchor["z"]))
                for anchor in anchor_chunk
            ) if anchor_chunk else 0
            local_radius = int(frontier)
            constraint_radius = max(near_radii) if near_radii else 0
            if progress:
                progress(
                    stage="anchor",
                    message=(
                        f"{source_label}：正在检查{anchor_target.label}候选，"
                        f"已扫到距玩家约 {_format_blocks(local_radius)}；附近约束范围 {_format_blocks(constraint_radius)}"
                    ),
                    radius=local_radius,
                    checked=checked,
                    total=total,
                )

            missing_anchor_positions: set[int] = set()
            combo_specs = [
                (idx, plan.targets[idx], _search_radius_from_anchor(plan, anchor_target.id, plan.targets[idx].id), NEAR_TARGET_LIMIT)
                for idx in target_order
            ]
            combo_items: list[dict[str, Any]] = []
            combo_ok = False
            if combo_specs:
                if progress:
                    progress(
                        stage="anchor",
                        message=(
                            f"{source_label}：native 组合筛选 {len(anchor_chunk)} 个{anchor_target.label}锚点，"
                            f"{len(combo_specs)} 个附近目标"
                        ),
                        radius=local_radius,
                        checked=checked,
                        total=total,
                    )
                combo_items, combo_warnings, combo_ok = _call_anchor_combo_batch(seed, version, anchor_chunk, combo_specs)
                warnings.extend(combo_warnings)

            if combo_ok:
                for item in combo_items:
                    anchor_pos = int(item["anchor_pos"])
                    missing = not bool(item.get("complete"))
                    by_index = item.get("targets", {})
                    for idx in target_order:
                        candidates = _filter_area_candidates(plan, by_index.get(idx, []), center_x, center_z)
                        if not candidates:
                            missing = True
                        else:
                            per_anchor_targets[anchor_pos][idx] = candidates
                    if missing:
                        missing_anchor_positions.add(anchor_pos)
            else:
                active_anchor_positions = set(range(len(anchor_chunk)))
                for idx in target_order:
                    if not active_anchor_positions:
                        break
                    target = plan.targets[idx]
                    near_radius = _search_radius_from_anchor(plan, anchor_target.id, target.id)
                    batch_refs = sorted(active_anchor_positions)
                    batch_queries = [
                        (anchor_chunk[anchor_pos]["x"], anchor_chunk[anchor_pos]["z"], near_radius, NEAR_TARGET_LIMIT, target)
                        for anchor_pos in batch_refs
                    ]
                    if progress:
                        progress(
                            stage="anchor",
                            message=(
                                f"{source_label}：正在用「{target.label}」筛选{anchor_target.label}候选；"
                                f"剩余 {len(active_anchor_positions)} 个锚点，局部范围 {_format_blocks(near_radius)}"
                            ),
                            radius=local_radius,
                            checked=checked,
                            total=total,
                        )
                    for anchor_pos, (candidates, _target_mode, target_warnings) in zip(batch_refs, _call_cubiomes_batch(seed, version, batch_queries)):
                        warnings.extend(target_warnings)
                        candidates = _filter_area_candidates(plan, candidates, center_x, center_z)
                        if not candidates:
                            missing_anchor_positions.add(anchor_pos)
                            active_anchor_positions.discard(anchor_pos)
                            continue
                        per_anchor_targets[anchor_pos][idx] = candidates

            for anchor_pos, per_target in enumerate(per_anchor_targets):
                if anchor_pos in missing_anchor_positions:
                    continue

                per_target, biome_filter_warnings = _apply_same_biome_filters(seed, version, plan, per_target)
                warnings.extend(biome_filter_warnings)
                if any(not candidates for candidates in per_target):
                    continue

                combos = _build_cluster_candidates(seed, version, center_x, center_z, search_radius, max_results, plan, per_target)
                for combo in combos:
                    key = tuple((p["id"], p["x"], p["z"]) for p in sorted(combo["targets"], key=lambda p: (p["id"], p["x"], p["z"])))
                    if key in seen_combos:
                        continue
                    seen_combos.add(key)
                    accepted.append(combo)
                accepted.sort(key=lambda r: _cluster_sort_key(r, center_x, center_z, plan))
                if len(accepted) >= max_results:
                    worst_primary = _cluster_sort_key(accepted[max_results - 1], center_x, center_z, plan)[0]
                    lower_bound = max(0, local_radius - anchor_reach)
                    if not allow_chunk_stop:
                        continue
                    if lower_bound <= worst_primary:
                        if progress:
                            progress(
                                stage="anchor",
                                message=(
                                    f"{source_label}：已找到候选，继续确认是否存在更近区域；"
                                    f"当前锚点边界 {_format_blocks(local_radius)}，理论下界 {_format_blocks(int(lower_bound))}"
                                ),
                                radius=local_radius,
                                checked=checked,
                                total=total,
                            )
                        continue
                    if progress:
                        progress(
                            stage="done",
                            message=(
                                "已找到满足条件的候选区域；后续未扫描锚点按距离下界判断不可能更近，已剪枝停止"
                            ),
                            radius=local_radius,
                            checked=len(seen_anchor_keys),
                            total=total,
                        )
                    return True
        return False

    if exhaustive:
        max_anchor_radius = search_radius + anchor_reach
        max_ring = max(0, int(math.floor(max_anchor_radius / ANCHOR_TILE_SIZE + 0.5)))
        for ring in range(max_ring + 1):
            tiles = _tile_ring(ring)
            covered_before = 0 if ring == 0 else _tile_completed_radius(ring - 1)
            if progress:
                progress(
                    stage="anchor",
                    message=(
                        f"全范围分区扫描：第 {ring}/{max_ring} 圈，"
                        f"已完整覆盖到约 {_format_blocks(covered_before)}"
                    ),
                    radius=covered_before,
                    checked=len(seen_anchor_keys),
                    total=None,
                )
            tile_results = _query_anchor_tiles(
                seed,
                version,
                center_x,
                center_z,
                search_radius,
                plan,
                anchor_target,
                tiles,
                max_anchor_radius,
                ring,
                progress=progress,
            )
            for tile_pos, ix, iz, anchors, tile_warnings in tile_results:
                warnings.extend(tile_warnings)
                processed_before = len(seen_anchor_keys)
                new_anchors = [anchor for anchor in anchors if (anchor["x"], anchor["z"]) not in seen_anchor_keys]
                for anchor in new_anchors:
                    seen_anchor_keys.add((anchor["x"], anchor["z"]))
                if not new_anchors:
                    continue
                if progress:
                    progress(
                        stage="anchor",
                        message=(
                            f"全范围分区扫描：tile({ix},{iz}) 找到 {len(new_anchors)} 个新的{anchor_target.label}；"
                            f"本圈 {tile_pos}/{len(tiles)}"
                        ),
                        radius=int(_tile_min_distance(ix, iz)),
                        checked=len(seen_anchor_keys),
                        total=None,
                    )
                if process_anchor_batch(
                    new_anchors,
                    processed_before,
                    None,
                    False,
                    "全范围分区扫描",
                ):
                    return accepted[:max_results], list(dict.fromkeys(warnings))

            completed_radius = _tile_completed_radius(ring)
            if len(accepted) >= max_results:
                worst_primary = _cluster_sort_key(accepted[max_results - 1], center_x, center_z, plan)[0]
                lower_bound = max(0, completed_radius - anchor_reach)
                if lower_bound > worst_primary:
                    if progress:
                        progress(
                            stage="done",
                            message=(
                                "全范围分区扫描已找到满足条件的候选；"
                                f"已完整覆盖 {_format_blocks(completed_radius)}，理论下界 {_format_blocks(int(lower_bound))}，已证明没有更近结果"
                            ),
                            radius=completed_radius,
                            checked=len(seen_anchor_keys),
                            total=None,
                        )
                    return accepted[:max_results], list(dict.fromkeys(warnings))

        return accepted[:max_results], list(dict.fromkeys(warnings))

    for anchor_limit in anchor_limits:
        if progress:
            progress(
                stage="anchor",
                message=f"正在读取距离玩家最近的 {anchor_limit} 个{anchor_target.label}候选，最大范围 {_format_blocks(search_radius)}",
                radius=search_radius,
                checked=len(seen_anchor_keys),
                total=anchor_limit,
            )
        anchors, _mode, anchor_warnings = _call_cubiomes(
            seed, version, center_x, center_z, search_radius, anchor_limit, anchor_target
        )
        warnings.extend(anchor_warnings)
        anchors = _filter_area_candidates(plan, anchors, center_x, center_z)
        if not _delay_anchor_biome_filter(plan, anchor_target.id):
            anchors, biome_warnings = _filter_same_biome_candidates(seed, version, plan, anchor_target, anchors)
            warnings.extend(biome_warnings)
        new_anchors = [a for a in anchors if (a["x"], a["z"]) not in seen_anchor_keys]
        processed_before = len(seen_anchor_keys)
        for anchor in new_anchors:
            seen_anchor_keys.add((anchor["x"], anchor["z"]))
        if process_anchor_batch(new_anchors, processed_before, len(seen_anchor_keys), True, "锚点预筛"):
            return accepted[:max_results], list(dict.fromkeys(warnings))
    if seen_anchor_keys:
        warnings.append(f"锚点预筛阶段已检查距离玩家最近的 {len(seen_anchor_keys)} 个{anchor_target.label}候选。")
    return accepted[:max_results], list(dict.fromkeys(warnings))


def _search_largest_biome_area(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    search_radius: int,
    max_results: int,
    plan: SearchPlan,
    progress: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    area_items = _biome_area_constraint_items(plan)
    target = area_items[0][0] if area_items else next((item for item in plan.targets if item.kind == "biome"), None)
    if target is None:
        return [], ["最大面积搜索需要一个生物群系目标。"]
    measurement_radius = area_items[0][3] if area_items else BIOME_AREA_DEFAULT_RADIUS
    if progress:
        progress(
            stage="radius",
            message=f"正在全世界抽样发现大型{target.label}候选",
            radius=search_radius,
            checked=0,
            total=BIOME_AREA_GLOBAL_SAMPLES,
        )
    sampled, mode, sample_warnings, sample_meta = _call_biome_samples(
        seed,
        version,
        center_x,
        center_z,
        search_radius,
        BIOME_AREA_GLOBAL_SAMPLES,
        BIOME_AREA_GLOBAL_CANDIDATES,
        target,
    )
    local, local_mode, local_warnings = _call_cubiomes(
        seed,
        version,
        center_x,
        center_z,
        min(search_radius, 262_144),
        16,
        target,
    )
    if local_mode:
        mode = local_mode
    warnings = list(dict.fromkeys(sample_warnings + local_warnings))
    candidates: list[dict[str, Any]] = []
    seen_points: set[tuple[int, int]] = set()
    for point in [*sampled, *local]:
        key = (int(point["x"]), int(point["z"]))
        if key in seen_points:
            continue
        seen_points.add(key)
        candidates.append(point)
    sample_meta["candidate_count"] = len(candidates)
    if not candidates:
        return [], warnings + [f"抽样范围内没有发现{target.label}候选。"]

    if progress:
        progress(
            stage="search",
            message=f"正在粗测 {len(candidates)} 个{target.label}候选的连通面积",
            radius=search_radius,
            checked=0,
            total=len(candidates),
        )

    measured: list[tuple[dict[str, Any], dict[str, Any]]] = []
    measure_warnings: list[str] = []

    def measure_coarse(point: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, list[str]]:
        area, item_warnings = _measure_biome_point(
            seed,
            version,
            target,
            point,
            None,
            measurement_radius,
            BIOME_AREA_COARSE_STEP,
        )
        return point, area, item_warnings

    workers = min(2, len(candidates))
    completed = 0
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="biome-area") as executor:
            futures = [executor.submit(measure_coarse, point) for point in candidates]
            for future in as_completed(futures):
                point, area, item_warnings = future.result()
                completed += 1
                if area:
                    measured.append((point, area))
                if progress:
                    progress(
                        stage="search",
                        message=f"正在粗测{target.label}候选面积",
                        radius=search_radius,
                        checked=completed,
                        total=len(candidates),
                    )
    else:
        for point in candidates:
            _point, area, item_warnings = measure_coarse(point)
            completed += 1
            if area:
                measured.append((point, area))
    warnings.extend(measure_warnings)
    if not measured:
        return [], list(dict.fromkeys(warnings + [f"未能测量{target.label}候选面积。"]))

    components: dict[tuple[int, int], tuple[dict[str, Any], dict[str, Any]]] = {}
    for point, area in measured:
        center = area.get("center") or point
        component_key = (round(int(center["x"]) / 256), round(int(center["z"]) / 256))
        previous = components.get(component_key)
        if previous is None or int(area.get("area") or 0) > int(previous[1].get("area") or 0):
            components[component_key] = (point, area)
    ranked = sorted(components.values(), key=lambda item: int(item[1].get("area") or 0), reverse=True)

    refine_count = min(len(ranked), max(3, max_results * 2))
    if progress:
        progress(
            stage="search",
            message=f"正在精测面积最大的 {refine_count} 个{target.label}候选",
            radius=search_radius,
            checked=0,
            total=refine_count,
        )

    def measure_fine(item: tuple[dict[str, Any], dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        point, coarse = item
        bounds = coarse.get("bounds") or {}
        required_radius = max(
            1024,
            abs(int(point["x"]) - int(bounds.get("min_x", point["x"]))),
            abs(int(point["x"]) - int(bounds.get("max_x", point["x"]))),
            abs(int(point["z"]) - int(bounds.get("min_z", point["z"]))),
            abs(int(point["z"]) - int(bounds.get("max_z", point["z"]))),
        ) + 256
        fine_radius = min(4096, required_radius)
        fine, item_warnings = _call_biome_area(
            seed,
            version,
            target,
            int(point["x"]),
            int(point["z"]),
            fine_radius,
            BIOME_AREA_FINE_STEP,
        )
        if fine and (not fine.get("truncated") or int(fine.get("area") or 0) >= int(coarse.get("area") or 0)):
            return point, fine, item_warnings
        return point, coarse, item_warnings

    refined: list[tuple[dict[str, Any], dict[str, Any]]] = []
    refine_warnings: list[str] = []
    refine_items = ranked[:refine_count]
    if len(refine_items) > 1:
        with ThreadPoolExecutor(max_workers=min(2, len(refine_items)), thread_name_prefix="biome-area-fine") as executor:
            futures = [executor.submit(measure_fine, item) for item in refine_items]
            for index, future in enumerate(as_completed(futures), start=1):
                point, area, item_warnings = future.result()
                refined.append((point, area))
                if progress:
                    progress(
                        stage="search",
                        message=f"正在精测{target.label}候选面积",
                        radius=search_radius,
                        checked=index,
                        total=refine_count,
                    )
    else:
        point, area, item_warnings = measure_fine(refine_items[0])
        refined.append((point, area))
    refined.extend(ranked[refine_count:])
    warnings.extend(refine_warnings)
    refined.sort(key=lambda item: int(item[1].get("area") or 0), reverse=True)

    results: list[dict[str, Any]] = []
    for rank, (point, measurement) in enumerate(refined[:max_results], start=1):
        area = int(measurement.get("area") or 0)
        suggested = dict(measurement.get("center") or {"x": point["x"], "z": point["z"]})
        target_point = {
            **point,
            "x": int(suggested["x"]),
            "z": int(suggested["z"]),
            "distance_to_site": 0,
            "biome_area": area,
            "biome_area_closed": bool(measurement.get("closed")),
            "biome_area_step": int(measurement.get("step") or BIOME_AREA_COARSE_STEP),
        }
        check = {
            "target": target.id,
            "label": target.label,
            "area": area,
            "min_area": None,
            "preference": "largest",
            "passed": True,
            "step": target_point["biome_area_step"],
            "radius": int(measurement.get("radius") or measurement_radius),
            "closed": bool(measurement.get("closed")),
            "truncated": bool(measurement.get("truncated")),
            "bounds": measurement.get("bounds"),
            "center": suggested,
            "width": measurement.get("width"),
            "height": measurement.get("height"),
            "perimeter": measurement.get("perimeter"),
            "sample_y": int(measurement.get("sample_y") or 63),
        }
        results.append(
            {
                "type": "biome_area",
                "mode": mode,
                "backend": "cubiomes",
                "suggested": suggested,
                "distance": round(math.dist((center_x, center_z), (suggested["x"], suggested["z"])), 1),
                "targets": [target_point],
                "max_pairwise_distance": 0,
                "satisfied": True,
                "failure_reasons": [],
                "warnings": [],
                "count_checks": [],
                "biome_area_checks": [check],
                "area_rank": rank,
                "area_search": sample_meta,
                "map_url": map_url(seed, version, suggested["x"], suggested["z"]),
                "searched_radius": search_radius,
            }
        )
    return results, list(dict.fromkeys(warnings))


def search(
    seed: str,
    version: str,
    center_x: int,
    center_z: int,
    radius: int,
    max_results: int,
    plan: SearchPlan,
    progress: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if plan.objective == "verify":
        return _verify_point_search(seed, version, center_x, center_z, plan)
    if not plan.targets:
        return [], ["没有可搜索目标。"]
    dimensions = _plan_dimensions(plan)
    if len(dimensions) > 1:
        return [], [
            "这个请求同时包含多个维度的目标或排除条件，当前不能把主世界/下界/末地坐标放在同一个距离图里计算。请分别查询同一维度内的目标，或明确只查某一个维度。"
        ]
    dimension = next(iter(dimensions))
    search_center_x, search_center_z, dimension_warnings = _dimension_search_center(dimension, center_x, center_z)
    warnings.extend(dimension_warnings)

    def finish(results: list[dict[str, Any]], extra_warnings: list[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        return _attach_dimension_metadata(results, dimension), list(dict.fromkeys(warnings + (extra_warnings or [])))

    if plan.objective == "largest_area":
        area_results, area_warnings = _search_largest_biome_area(
            seed,
            version,
            search_center_x,
            search_center_z,
            _search_budget(radius),
            max_results,
            plan,
            progress=progress,
        )
        return finish(area_results, area_warnings)

    if len(plan.targets) == 1:
        target = plan.targets[0]
        budget = _search_budget(radius)
        for search_radius in _radius_sequence(radius):
            if progress:
                progress(
                    stage="radius",
                    message=f"正在搜索「{target.label}」，当前范围 {_format_blocks(search_radius)}",
                    radius=search_radius,
                    checked=None,
                    total=None,
                )
            native_limit = _candidate_limit(max_results) if (plan.area or plan.exclude_biomes or plan.exclude_targets or plan.count_constraints or plan.biome_area_constraints) else max_results
            candidates, mode, target_warnings = _call_cubiomes(seed, version, search_center_x, search_center_z, search_radius, native_limit, target)
            warnings.extend(target_warnings)
            candidates = _filter_area_candidates(plan, candidates, search_center_x, search_center_z)
            candidates, biome_warnings = _filter_same_biome_candidates(seed, version, plan, target, candidates)
            warnings.extend(biome_warnings)
            if len(candidates) < max_results and search_radius < budget:
                continue
            results = []
            for c in candidates:
                target_point = {**c, "distance_to_site": 0}
                failure_reasons, absence_warnings = _evaluate_absence_constraints(
                    seed,
                    version,
                    plan,
                    [target_point],
                    int(c["x"]),
                    int(c["z"]),
                )
                count_checks: list[dict[str, Any]] = []
                count_warnings: list[str] = []
                if not failure_reasons and plan.count_constraints:
                    count_checks, count_failures, count_warnings = _evaluate_count_constraints(
                        seed,
                        version,
                        search_center_x,
                        search_center_z,
                        plan,
                        int(c["x"]),
                        int(c["z"]),
                    )
                    failure_reasons.extend(count_failures)
                biome_area_checks: list[dict[str, Any]] = []
                if not failure_reasons and plan.biome_area_constraints:
                    biome_area_checks, area_failures, area_warnings = _evaluate_biome_area_constraints(
                        seed,
                        version,
                        plan,
                        [target_point],
                    )
                    failure_reasons.extend(area_failures)
                    count_warnings.extend(area_warnings)
                item_warnings = list(dict.fromkeys(absence_warnings + count_warnings))
                if failure_reasons:
                    continue
                results.append(
                    {
                        "type": "single",
                        "mode": mode,
                        "backend": "cubiomes",
                        "suggested": {"x": c["x"], "z": c["z"]},
                        "distance": c["distance_to_center"],
                        "targets": [target_point],
                        "max_pairwise_distance": 0,
                        "satisfied": True,
                        "failure_reasons": [],
                        "warnings": item_warnings,
                        "count_checks": count_checks,
                        "biome_area_checks": biome_area_checks,
                        "map_url": map_url(seed, version, c["x"], c["z"]),
                        "searched_radius": search_radius,
                    }
                )
                if len(results) >= max_results:
                    break
            if results:
                return finish(results)
        return finish([], [f"已扩展到 Minecraft 可玩世界范围（约 {_format_blocks(budget)}）仍未找到「{target.label}」。"])

    budget = _search_budget(radius)
    if plan.biome_area_constraints and any(target.kind == "structure" for target in plan.targets):
        area_results, area_warnings = _search_by_anchor(
            seed,
            version,
            search_center_x,
            search_center_z,
            budget,
            max_results,
            plan,
            progress=progress,
            exhaustive_override=False,
        )
        warnings.extend(area_warnings)
        if area_results:
            return finish(area_results[:max_results])
        return finish([])

    if _is_strict_anchor_constrained_plan(plan):
        anchor_results, anchor_warnings = _search_by_anchor(
            seed,
            version,
            search_center_x,
            search_center_z,
            budget,
            max_results,
            plan,
            progress=progress,
        )
        warnings.extend(anchor_warnings)
        if anchor_results:
            return finish(anchor_results[:max_results])
        return finish([])

    spm_results, spm_warnings, spm_handled = _search_by_spm(
        seed,
        version,
        search_center_x,
        search_center_z,
        budget,
        max_results,
        plan,
        progress=progress,
    )
    warnings.extend(spm_warnings)
    if spm_handled:
        if spm_results:
            return finish(spm_results[:max_results])
        strict_text = ""
        if _same_biome_constraints(plan):
            strict_text = " 已按“结构坐标所在生物群系必须匹配”做严格校验；如果你想要宽松条件，可以改写成“村庄附近有平原”。"
        return finish(
            [],
            [
                (
                    "SPM 引擎已在当前候选层和距离约束图中完成 range join，"
                    f"没有找到满足全部约束的组合；已使用搜索范围约 {_format_blocks(budget)}。{strict_text}"
                )
            ],
        )

    graph_results, graph_warnings = _search_by_hub_anchor(
        seed,
        version,
        search_center_x,
        search_center_z,
        budget,
        max_results,
        plan,
        progress=progress,
    )
    warnings.extend(graph_warnings)
    if graph_results:
        return finish(graph_results[:max_results])

    anchor_results, anchor_warnings = _search_by_anchor(
        seed,
        version,
        search_center_x,
        search_center_z,
        budget,
        max_results,
        plan,
        progress=progress,
    )
    warnings.extend(anchor_warnings)
    if anchor_results:
        return finish(anchor_results[:max_results])

    last_missing: list[str] = []
    materialized_biomes = _same_biome_materialized_targets(plan)
    for search_radius in _radius_sequence(radius):
        if progress:
            progress(
                stage="radius",
                message=f"正在扩展全局候选池，当前范围 {_format_blocks(search_radius)}",
                radius=search_radius,
                checked=None,
                total=None,
            )
        missing: list[str] = []
        batch_queries: list[tuple[int, int, int, int, Target]] = []
        batch_indices: list[int] = []
        per_target: list[list[dict[str, Any]]] = [[] for _target in plan.targets]
        for idx, target in enumerate(plan.targets):
            if target.kind == "biome" and target.id in materialized_biomes:
                continue
            batch_queries.append((search_center_x, search_center_z, search_radius, _candidate_limit(max_results), target))
            batch_indices.append(idx)
        for idx, (candidates, _mode, target_warnings) in zip(batch_indices, _call_cubiomes_batch(seed, version, batch_queries)):
            warnings.extend(target_warnings)
            target = plan.targets[idx]
            candidates = _filter_area_candidates(plan, candidates, search_center_x, search_center_z)
            candidates, biome_warnings = _filter_same_biome_candidates(seed, version, plan, target, candidates)
            warnings.extend(biome_warnings)
            if not candidates:
                missing.append(plan.targets[idx].label)
            per_target[idx] = candidates
        if not missing:
            per_target, biome_filter_warnings = _apply_same_biome_filters(seed, version, plan, per_target)
            warnings.extend(biome_filter_warnings)
            for idx, candidates in enumerate(per_target):
                if not candidates:
                    missing.append(plan.targets[idx].label)
        last_missing = missing
        if missing:
            continue
        combos = _build_cluster_candidates(seed, version, search_center_x, search_center_z, search_radius, max_results, plan, per_target)
        satisfied = [combo for combo in combos if combo["satisfied"]]
        if len(satisfied) >= max_results:
            return finish(satisfied[:max_results])
    missing_text = f"；缺少目标：{'、'.join(last_missing)}" if last_missing else ""
    strict_text = ""
    if _same_biome_constraints(plan):
        strict_text = " 已按“结构坐标所在生物群系必须匹配”做严格校验；如果你想要宽松条件，可以改写成“村庄附近有平原”。"
    budget = _search_budget(radius)
    return finish([], [f"没有因为用户配置半径提前停止；已扩展到 Minecraft 可玩世界范围（约 {_format_blocks(budget)}），仍未找到满足全部约束的组合{missing_text}。{strict_text}"])


def target_exists(target: Target) -> bool:
    return target.id in (STRUCTURES if target.kind == "structure" else BIOMES)
