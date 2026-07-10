from __future__ import annotations

import asyncio
import json
import re
from typing import Callable, Optional

import httpx

from .catalog import BIOMES, STRUCTURES
from .models import SearchPlan, Target


LEGACY_MODELS = {
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-pro": "deepseek-v4-pro",
    "deepseek-reasoner": "deepseek-v4-pro",
}

DEFAULT_MODELS = [
    {"id": "deepseek-v4-flash", "name": "deepseek-v4-flash", "source": "built_in"},
    {"id": "deepseek-v4-pro", "name": "deepseek-v4-pro", "source": "built_in"},
]
DEEPSEEK_PLAN_ATTEMPTS = 5
DEEPSEEK_REQUEST_TIMEOUT = 20
ProgressCallback = Callable[..., None]

UNSUPPORTED_OBJECTIVES = {
    "highest_elevation",
    "most_loot",
    "most_vaults",
    "highest",
    "most",
}


def _norm_key(value: object) -> str:
    return re.sub(r"[\s_\-/]+", "", str(value or "").strip().lower())


def _target_aliases() -> dict[str, tuple[str, str, str]]:
    aliases: dict[str, tuple[str, str, str]] = {}
    for kind, catalog in (("structure", STRUCTURES), ("biome", BIOMES)):
        for target_id, info in catalog.items():
            label = str(info.get("label") or target_id)
            for raw in [target_id, label, *(info.get("aliases") or [])]:
                aliases[_norm_key(raw)] = (kind, target_id, label)
    extras = {
        "劫掠塔": ("structure", "pillager_outpost", STRUCTURES["pillager_outpost"]["label"]),
        "掠夺塔": ("structure", "pillager_outpost", STRUCTURES["pillager_outpost"]["label"]),
        "前哨站": ("structure", "pillager_outpost", STRUCTURES["pillager_outpost"]["label"]),
        "沼泽小屋": ("structure", "witch_hut", STRUCTURES["witch_hut"]["label"]),
        "女巫塔": ("structure", "witch_hut", STRUCTURES["witch_hut"]["label"]),
        "樱花": ("biome", "cherry_grove", BIOMES["cherry_grove"]["label"]),
        "樱花树": ("biome", "cherry_grove", BIOMES["cherry_grove"]["label"]),
        "蘑菇岛": ("biome", "mushroom_fields", BIOMES["mushroom_fields"]["label"]),
        "蘑菇群系": ("biome", "mushroom_fields", BIOMES["mushroom_fields"]["label"]),
        "高山": ("biome", "jagged_peaks", BIOMES["jagged_peaks"]["label"]),
        "最高山": ("biome", "jagged_peaks", BIOMES["jagged_peaks"]["label"]),
    }
    for raw, value in extras.items():
        aliases[_norm_key(raw)] = value
    return aliases


TARGET_ALIASES = _target_aliases()


def _resolve_target(value: object, kind_hint: str | None = None) -> tuple[str, str, str] | None:
    if isinstance(value, dict):
        raw = value.get("id") or value.get("name") or value.get("label") or value.get("target") or value.get("type")
        kind_hint = kind_hint or value.get("kind") or value.get("category")
    else:
        raw = value
    key = _norm_key(raw)
    if not key:
        return None
    if key in TARGET_ALIASES:
        kind, target_id, label = TARGET_ALIASES[key]
        if kind_hint in {"structure", "biome"} and kind != kind_hint:
            hinted = STRUCTURES if kind_hint == "structure" else BIOMES
            if target_id not in hinted:
                return None
        return kind, target_id, label
    return None


def _resolve_many(value: object, kind_hint: str | None = None) -> list[tuple[str, str, str]]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, str) and re.search(r"[,，、/]|和|以及|或者", value):
        items = [part for part in re.split(r"[,，、/]|和|以及|或者", value) if part.strip()]
    else:
        items = [value]
    resolved: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for item in items:
        target = _resolve_target(item, kind_hint)
        if target and target[1] not in seen:
            seen.add(target[1])
            resolved.append(target)
    return resolved


def _coerce_int(value: object, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"-?\d+", str(value))
    if match:
        return int(match.group(0))
    return default


def _coerce_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = _norm_key(value)
    if text in {"false", "0", "no", "否", "不是", "不"}:
        return False
    if text in {"true", "1", "yes", "是", "最近", "nearest"}:
        return True
    return default


def _first_present(data: dict, *keys: str) -> object:
    for key in keys:
        if key in data:
            return data[key]
    return None


def _append_target(explicit_targets: list[dict], seen: set[str], resolved: tuple[str, str, str]) -> None:
    kind, target_id, label = resolved
    if target_id not in seen:
        seen.add(target_id)
        explicit_targets.append({"kind": kind, "id": target_id, "label": label})


def _normalize_sort_by(value: object) -> str:
    text = _norm_key(value or "nearest")
    mapping = {
        "nearest": "nearest",
        "最近": "nearest",
        "compact": "compact",
        "tightest": "compact",
        "最紧凑": "compact",
        "距离最短": "compact",
        "site": "site_distance",
        "中心最近": "site_distance",
        "anchor": "anchor_distance",
        "锚点最近": "anchor_distance",
        "largestarea": "largest_area",
        "最大面积": "largest_area",
        "面积最大": "largest_area",
    }
    return mapping.get(text, str(value or "nearest"))


def _normalize_area(value: object) -> dict | None:
    if not isinstance(value, dict):
        if value is None:
            return None
        text = _norm_key(value)
        if text in {"east", "东", "东边"}:
            return {"direction": "east"}
        if text in {"west", "西", "西边"}:
            return {"direction": "west"}
        if text in {"north", "北", "北边"}:
            return {"direction": "north"}
        if text in {"south", "南", "南边"}:
            return {"direction": "south"}
        return None
    area: dict = {}
    direction = value.get("direction")
    if direction:
        area["direction"] = str(direction)
    bounds = value.get("bounds") if isinstance(value.get("bounds"), dict) else value
    for src, dst in (("min_x", "min_x"), ("max_x", "max_x"), ("min_z", "min_z"), ("max_z", "max_z"), ("x_min", "min_x"), ("x_max", "max_x"), ("z_min", "min_z"), ("z_max", "max_z")):
        if src in bounds:
            area[dst] = _coerce_int(bounds.get(src), None)
    return area or None


def _normalize_point(value: object) -> dict | None:
    if isinstance(value, dict):
        x = _coerce_int(_first_present(value, "x", "center_x", "coord_x"), None)
        z = _coerce_int(_first_present(value, "z", "center_z", "coord_z"), None)
        if x is not None and z is not None:
            return {"x": x, "z": z}
        return None
    if isinstance(value, str):
        nums = re.findall(r"-?\d+", value)
        if len(nums) >= 2:
            return {"x": int(nums[0]), "z": int(nums[1])}
    return None


def _unwrap_plan_data(data: dict) -> dict:
    current = data
    for key in ("plan", "search_plan", "tool_args", "arguments", "parameters", "query"):
        value = current.get(key)
        if isinstance(value, dict):
            current = value
    return current


def _normalize_capability(value: object) -> str:
    text = _norm_key(value or "supported")
    if text in {"unsupported", "notsupported", "不支持", "无法支持", "不能执行", "当前不能精确执行"}:
        return "unsupported"
    if text in {"approximate", "compatible", "compatibility", "近似", "兼容"}:
        return "approximate"
    return "supported"


def _normalize_objective(value: object) -> str:
    text = _norm_key(value or "nearest")
    mapping = {
        "最近": "nearest",
        "nearest": "nearest",
        "locate": "locate",
        "cluster": "cluster",
        "verify": "verify",
        "check": "verify",
        "核对": "verify",
        "验证": "verify",
        "区域": "cluster",
        "组合": "cluster",
        "最大": "largest_area",
        "最大面积": "largest_area",
        "largestarea": "largest_area",
        "largest": "largest_area",
        "最高": "highest_elevation",
        "最高海拔": "highest_elevation",
        "highestelevation": "highest_elevation",
        "宝箱最多": "most_loot",
        "mostloot": "most_loot",
        "宝库最多": "most_vaults",
        "不详宝箱最多": "most_vaults",
        "mostvaults": "most_vaults",
    }
    return mapping.get(text, str(value or "nearest"))


def _normalize_plan_data(data: dict) -> tuple[dict, list[str]]:
    source = _unwrap_plan_data(data)
    notes: list[str] = []

    raw_targets = _first_present(source, "targets", "target", "objects", "requirements", "goals")
    if raw_targets is None:
        raw_targets = []
    if isinstance(raw_targets, (str, dict)):
        raw_targets = [raw_targets]
    if not isinstance(raw_targets, list):
        raw_targets = []

    explicit_targets: list[dict] = []
    seen: set[str] = set()
    for item in raw_targets:
        kind_hint = item.get("kind") if isinstance(item, dict) else None
        resolved = _resolve_target(item, kind_hint)
        if not resolved:
            continue
        _append_target(explicit_targets, seen, resolved)

    for key, kind_hint in (
        ("structure", "structure"),
        ("structures", "structure"),
        ("biome", "biome"),
        ("biomes", "biome"),
    ):
        raw = source.get(key)
        if raw is None:
            continue
        values = raw if isinstance(raw, list) else [raw]
        for item in values:
            resolved = _resolve_target(item, kind_hint)
            if not resolved:
                continue
            _append_target(explicit_targets, seen, resolved)

    raw_adjacency = _first_present(source, "adjacency", "constraints", "relations", "nearby", "rules")
    if raw_adjacency is None:
        raw_adjacency = []
    if isinstance(raw_adjacency, dict):
        raw_adjacency = [raw_adjacency]
    if not isinstance(raw_adjacency, list):
        raw_adjacency = []

    adjacency: list[dict] = []
    exclude_biomes: list[dict] = []
    exclude_targets: list[dict] = []
    for item in raw_adjacency:
        if not isinstance(item, dict):
            continue
        a = _resolve_target(_first_present(item, "a", "from", "source", "target_a", "left"))
        b = _resolve_target(_first_present(item, "b", "to", "target", "target_b", "right"))
        if not a or not b:
            continue
        relation_raw = _norm_key(_first_present(item, "relation", "type", "operator") or "near")
        if relation_raw in {"notinbiome", "notin", "exclude_biome", "excludebiome", "forbiddenbiome", "avoidbiome", "不在", "不能在", "不要在", "排除群系"}:
            structure = a if a[0] == "structure" else b if b[0] == "structure" else None
            biome = b if b[0] == "biome" else a if a[0] == "biome" else None
            if structure and biome:
                _append_target(explicit_targets, seen, structure)
                exclude_biomes.append({"target": structure[1], "biomes": [biome[1]]})
            continue
        if relation_raw in {"notnear", "avoid", "away", "mindistance", "min_distance", "不要靠近", "远离", "至少"}:
            threshold = _coerce_int(_first_present(item, "threshold", "distance", "min_distance", "radius", "within", "range"), 512)
            exclude_targets.append({"a": a[1], "b": b[1], "min_distance": threshold})
            _append_target(explicit_targets, seen, a)
            continue
        relation = "same_biome" if relation_raw in {"samebiome", "inbiome", "坐落于", "位于", "在上", "同生物群系"} else "near"
        threshold = _coerce_int(_first_present(item, "threshold", "distance", "max_distance", "radius", "within", "range"), 512)
        min_threshold = _coerce_int(_first_present(item, "min_threshold", "min_distance", "at_least"), None)
        if relation == "same_biome":
            threshold = 0
        adj_item = {"a": a[1], "b": b[1], "threshold": threshold, "relation": relation}
        if min_threshold is not None:
            adj_item["min_threshold"] = min_threshold
        adjacency.append(adj_item)
        for resolved in (a, b):
            _append_target(explicit_targets, seen, resolved)

    raw_layouts = _first_present(
        source,
        "relative_layout",
        "relative_layouts",
        "layout_constraints",
        "spatial_layout",
        "orientation_constraints",
    )
    if isinstance(raw_layouts, dict):
        raw_layouts = [raw_layouts]
    if not isinstance(raw_layouts, list):
        raw_layouts = []

    relative_layout: list[dict] = []
    seen_layouts: set[tuple[str, str, str]] = set()
    for item in raw_layouts:
        if not isinstance(item, dict):
            continue
        center = _resolve_target(_first_present(item, "center", "pivot", "anchor", "subject"))
        back = _resolve_target(_first_present(item, "back", "behind", "rear", "a", "left"))
        front = _resolve_target(_first_present(item, "front", "facing", "face", "b", "right"))
        if not center or not back or not front or len({center[1], back[1], front[1]}) < 3:
            continue
        relation = _norm_key(_first_present(item, "relation", "type", "operator") or "opposite_sides")
        if relation not in {"oppositesides", "opposite", "across", "between", "两侧", "相反方向", "背靠面朝"}:
            continue
        key = (center[1], back[1], front[1])
        if key in seen_layouts:
            continue
        seen_layouts.add(key)
        min_angle = max(90, min(180, int(_coerce_int(_first_present(item, "min_angle", "angle", "minimum_angle"), 120) or 120)))
        shared_distance = max(64, int(_coerce_int(_first_present(item, "max_distance", "threshold", "within", "radius"), 512) or 512))
        back_distance = max(64, int(_coerce_int(_first_present(item, "back_max_distance", "back_distance", "rear_distance"), shared_distance) or shared_distance))
        front_distance = max(64, int(_coerce_int(_first_present(item, "front_max_distance", "front_distance", "facing_distance"), shared_distance) or shared_distance))
        relative_layout.append(
            {
                "center": center[1],
                "back": back[1],
                "front": front[1],
                "relation": "opposite_sides",
                "min_angle": min_angle,
                "back_max_distance": back_distance,
                "front_max_distance": front_distance,
            }
        )
        for resolved in (center, back, front):
            _append_target(explicit_targets, seen, resolved)
        for other, threshold in ((back, back_distance), (front, front_distance)):
            existing = next(
                (
                    adj
                    for adj in adjacency
                    if adj.get("relation") != "same_biome"
                    and {str(adj.get("a")), str(adj.get("b"))} == {center[1], other[1]}
                ),
                None,
            )
            if existing is None:
                adjacency.append({"a": center[1], "b": other[1], "threshold": threshold, "relation": "near"})

    raw_exclude_biomes = _first_present(source, "exclude_biomes", "forbidden_biomes", "not_in_biomes", "avoid_biomes", "biome_exclusions")
    if isinstance(raw_exclude_biomes, dict) and any(key in raw_exclude_biomes for key in ("target", "structure", "id", "biomes", "exclude", "not_in", "forbidden")):
        raw_exclude_biomes = [raw_exclude_biomes]
    if isinstance(raw_exclude_biomes, dict):
        items = raw_exclude_biomes.items()
        for target_raw, biomes_raw in items:
            target = _resolve_target(target_raw, "structure") or _resolve_target(target_raw)
            if not target:
                continue
            biomes = [item[1] for item in _resolve_many(biomes_raw, "biome")]
            if biomes:
                _append_target(explicit_targets, seen, target)
                exclude_biomes.append({"target": target[1], "biomes": biomes})
    elif isinstance(raw_exclude_biomes, list):
        for item in raw_exclude_biomes:
            if isinstance(item, dict):
                target = _resolve_target(_first_present(item, "target", "structure", "a", "id"))
                biomes = [entry[1] for entry in _resolve_many(_first_present(item, "biomes", "exclude", "not_in", "forbidden", "b"), "biome")]
                if target and biomes:
                    _append_target(explicit_targets, seen, target)
                    exclude_biomes.append({"target": target[1], "biomes": biomes})

    raw_exclude_targets = _first_present(source, "exclude_targets", "avoid_targets", "not_near_targets", "target_exclusions")
    if isinstance(raw_exclude_targets, dict):
        raw_exclude_targets = [raw_exclude_targets]
    if isinstance(raw_exclude_targets, list):
        for item in raw_exclude_targets:
            if not isinstance(item, dict):
                continue
            a = _resolve_target(_first_present(item, "a", "from", "source"))
            b = _resolve_target(_first_present(item, "b", "target", "to"))
            if a and b:
                distance = _coerce_int(_first_present(item, "min_distance", "distance", "radius", "threshold"), 512)
                exclude_targets.append({"a": a[1], "b": b[1], "min_distance": distance})
                _append_target(explicit_targets, seen, a)

    count_constraints: list[dict] = []
    raw_counts = _first_present(source, "count_constraints", "counts", "min_counts", "quantity")
    if isinstance(raw_counts, dict):
        raw_counts = [{"target": key, "min": value} for key, value in raw_counts.items()]
    if isinstance(raw_counts, list):
        for item in raw_counts:
            if not isinstance(item, dict):
                continue
            target = _resolve_target(_first_present(item, "target", "id", "name"))
            if target:
                _append_target(explicit_targets, seen, target)
                count_constraints.append(
                    {
                        "target": target[1],
                        "min": _coerce_int(_first_present(item, "min", "min_count", "count", "at_least"), 1),
                        "within": _coerce_int(_first_present(item, "within", "radius", "distance"), None),
                    }
                )

    biome_area_constraints: list[dict] = []
    raw_biome_areas = _first_present(
        source,
        "biome_area_constraints",
        "biome_areas",
        "area_size_constraints",
        "biome_size_constraints",
    )
    if isinstance(raw_biome_areas, dict):
        raw_biome_areas = [raw_biome_areas]
    if isinstance(raw_biome_areas, list):
        for item in raw_biome_areas:
            if not isinstance(item, dict):
                continue
            target = _resolve_target(_first_present(item, "target", "biome", "id", "name"), "biome")
            if not target:
                continue
            _append_target(explicit_targets, seen, target)
            min_area = _coerce_int(_first_present(item, "min_area", "minimum_area", "area", "at_least"), None)
            preference_raw = _norm_key(_first_present(item, "preference", "sort", "ranking") or "larger")
            preference = "largest" if preference_raw in {"largest", "max", "maximum", "最大", "面积最大"} else "larger"
            constraint = {
                "target": target[1],
                "min_area": max(0, int(min_area)) if min_area is not None else None,
                "preference": preference,
            }
            measurement_radius = _coerce_int(_first_present(item, "measurement_radius", "radius", "scan_radius"), None)
            if measurement_radius is not None:
                constraint["measurement_radius"] = max(512, min(16384, int(measurement_radius)))
            biome_area_constraints.append(constraint)

    pairwise = _coerce_int(
        _first_present(source, "pairwise_max_distance", "max_pairwise_distance", "pairwise_distance", "max_distance_between_targets"),
        None,
    )
    objective = _normalize_objective(_first_present(source, "objective", "intent", "task", "mode"))
    capability = _normalize_capability(source.get("capability"))
    unsupported_reason = source.get("unsupported_reason") or source.get("reason")
    fallback_suggestion = source.get("fallback_suggestion") or source.get("suggestion")

    if objective in UNSUPPORTED_OBJECTIVES and capability == "supported":
        capability = "unsupported"
        unsupported_reason = unsupported_reason or "当前后端只能验证结构/生物群系坐标和距离约束，还不能计算面积、海拔、战利品或宝库数量排名。"
        fallback_suggestion = fallback_suggestion or "可以先改问“找最近的对应结构或生物群系”，或指定多个目标之间的距离约束。"

    anchor = _resolve_target(_first_present(source, "anchor_target_id", "anchor", "center_target"))
    anchor_id = anchor[1] if anchor else None

    normalized = {
        "targets": explicit_targets,
        "pairwise_max_distance": pairwise,
        "adjacency": adjacency,
        "relative_layout": relative_layout,
        "exclude_biomes": exclude_biomes,
        "exclude_targets": exclude_targets,
        "count_constraints": count_constraints,
        "biome_area_constraints": biome_area_constraints,
        "area": _normalize_area(_first_present(source, "area", "bounds", "search_area", "region", "direction")),
        "verify_point": _normalize_point(_first_present(source, "verify_point", "point", "coordinate", "coords", "location")),
        "sort_by": _normalize_sort_by(_first_present(source, "sort_by", "sort", "ranking", "priority")),
        "nearest_to_player": _coerce_bool(source.get("nearest_to_player"), True),
        "anchor_target_id": anchor_id,
        "objective": objective,
        "capability": capability,
        "unsupported_reason": unsupported_reason,
        "fallback_suggestion": fallback_suggestion,
        "notes": source.get("notes") if isinstance(source.get("notes"), list) else [],
    }
    return normalized, notes


def _blocked_plan(reason: str, suggestion: str = "请先在左侧 DeepSeek 设置里填写并保存 API Key。") -> SearchPlan:
    return SearchPlan(
        targets=[],
        pairwise_max_distance=None,
        adjacency=[],
        nearest_to_player=True,
        anchor_target_id=None,
        objective="ai_required",
        capability="unsupported",
        unsupported_reason=reason,
        fallback_suggestion=suggestion,
        notes=["AI 解析未完成，搜索核心未运行。"],
    )


def _validate_plan(plan: SearchPlan) -> SearchPlan:
    invalid = []
    for target in plan.targets:
        allowed = STRUCTURES if target.kind == "structure" else BIOMES
        if target.id not in allowed:
            invalid.append(f"{target.kind}/{target.id}")
    if invalid:
        return _blocked_plan(
            f"DeepSeek 返回了后端不支持的目标：{'、'.join(invalid)}。",
            "请换一种说法，或等后端目录接入这些目标后再搜索。",
        )
    if not plan.targets and plan.capability != "unsupported" and plan.objective != "verify":
        return _blocked_plan("DeepSeek 没有解析出可执行目标。", "请补充你要找的结构或生物群系。")
    return plan


def _validate_plan_or_raise(data: dict) -> SearchPlan:
    plan = SearchPlan.model_validate(data)
    invalid = []
    for target in plan.targets:
        allowed = STRUCTURES if target.kind == "structure" else BIOMES
        if target.id not in allowed:
            invalid.append(f"{target.kind}/{target.id}")
    if invalid:
        return _blocked_plan(
            f"DeepSeek 识别到了后端目录外的目标：{'、'.join(invalid)}。",
            "请换一种说法，或等后端目录接入这些目标后再搜索。",
        )
    if not plan.targets and plan.capability != "unsupported" and plan.objective != "verify":
        return _blocked_plan("DeepSeek 没有解析出可执行目标。", "请补充你要找的结构或生物群系。")
    return plan


def _resolve_target_in_text(text: str) -> tuple[str, str, str] | None:
    normalized = _norm_key(text)
    matches = [
        (len(alias), resolved)
        for alias, resolved in TARGET_ALIASES.items()
        if alias and alias in normalized
    ]
    return max(matches, key=lambda item: item[0])[1] if matches else None


def _augment_relative_layout_from_message(message: str, plan: SearchPlan) -> SearchPlan:
    back_match = re.search(r"背靠", message)
    face_match = re.search(r"面朝|面对|朝向", message)
    if not back_match or not face_match or back_match.end() >= face_match.start():
        return plan

    clause_break = r"[，,。；;]|并且|而且|同时|另外"
    back_clause = re.split(clause_break, message[back_match.end() : face_match.start()], maxsplit=1)[0]
    front_clause = re.split(clause_break, message[face_match.end() :], maxsplit=1)[0]
    center = _resolve_target_in_text(message[: back_match.start()])
    back = _resolve_target_in_text(back_clause)
    front = _resolve_target_in_text(front_clause)
    if not center:
        structures = [target for target in plan.targets if target.kind == "structure"]
        if len(structures) == 1:
            only = structures[0]
            center = (only.kind, only.id, only.label)
    if not center or not back or not front or len({center[1], back[1], front[1]}) < 3:
        return plan

    targets = list(plan.targets)
    target_ids = {target.id for target in targets}
    for kind, target_id, label in (center, back, front):
        if target_id not in target_ids:
            target_ids.add(target_id)
            targets.append(Target(kind=kind, id=target_id, label=label))

    adjacency = [dict(item) for item in plan.adjacency]
    for other in (back, front):
        exists = any(
            item.get("relation") != "same_biome"
            and {str(item.get("a")), str(item.get("b"))} == {center[1], other[1]}
            for item in adjacency
        )
        if not exists:
            adjacency.append({"a": center[1], "b": other[1], "threshold": 512, "relation": "near"})

    for near_match in re.finditer(
        r"(?P<subject>[^，,。；;]{0,16}?)附近\s*(?P<distance>\d+)\s*格(?:以内|内)?有(?P<target>[^，,。；;]+)",
        message,
    ):
        near_center = _resolve_target_in_text(near_match.group("subject")) or center
        near_target = _resolve_target_in_text(near_match.group("target"))
        if not near_center or not near_target or near_center[1] == near_target[1]:
            continue
        for kind, target_id, label in (near_center, near_target):
            if target_id not in target_ids:
                target_ids.add(target_id)
                targets.append(Target(kind=kind, id=target_id, label=label))
        distance = max(1, int(near_match.group("distance")))
        existing = next(
            (
                item
                for item in adjacency
                if item.get("relation") != "same_biome"
                and {str(item.get("a")), str(item.get("b"))} == {near_center[1], near_target[1]}
            ),
            None,
        )
        if existing is None:
            adjacency.append({"a": near_center[1], "b": near_target[1], "threshold": distance, "relation": "near"})

    if center[1] == "village" and re.search(r"村庄.{0,12}(?:坐落于|位于|在).{0,6}(?:平原|草原)", message):
        plains = _resolve_target("plains", "biome")
        if plains and plains[1] not in target_ids:
            targets.append(Target(kind=plains[0], id=plains[1], label=plains[2]))
            target_ids.add(plains[1])
        if plains and not any(
            item.get("relation") == "same_biome"
            and {str(item.get("a")), str(item.get("b"))} == {center[1], plains[1]}
            for item in adjacency
        ):
            adjacency.append({"a": center[1], "b": plains[1], "threshold": 0, "relation": "same_biome"})

    relative_layout = [dict(item) for item in plan.relative_layout]
    if not any(
        str(item.get("center")) == center[1]
        and str(item.get("back")) == back[1]
        and str(item.get("front")) == front[1]
        for item in relative_layout
    ):
        relative_layout.append(
            {
                "center": center[1],
                "back": back[1],
                "front": front[1],
                "relation": "opposite_sides",
                "min_angle": 120,
                "back_max_distance": 512,
                "front_max_distance": 512,
            }
        )

    notes = list(plan.notes)
    note = "已将“背靠 / 面朝”转换为以中心目标为顶点、前后目标夹角至少 120° 的两侧布局约束。"
    if note not in notes:
        notes.append(note)
    capability = plan.capability
    unsupported_reason = plan.unsupported_reason
    fallback_suggestion = plan.fallback_suggestion
    if capability == "unsupported" and plan.objective not in UNSUPPORTED_OBJECTIVES:
        capability = "supported"
        unsupported_reason = None
        fallback_suggestion = None
    return plan.model_copy(
        update={
            "targets": targets,
            "adjacency": adjacency,
            "relative_layout": relative_layout,
            "anchor_target_id": center[1],
            "capability": capability,
            "unsupported_reason": unsupported_reason,
            "fallback_suggestion": fallback_suggestion,
            "notes": notes,
        }
    )


def _augment_biome_area_from_message(message: str, plan: SearchPlan) -> SearchPlan:
    largest_requested = bool(re.search(r"最大(?:的|面积)?|面积最大|largest", message, re.I)) or plan.objective == "largest_area"
    larger_requested = bool(re.search(r"大一点|大一些|大点|比较大|足够大|面积大|规模大", message))
    area_match = re.search(r"(\d+(?:\.\d+)?)\s*(万)?\s*(?:平方格|平方块|方块面积|格面积)", message)
    if not largest_requested and not larger_requested and not area_match and not plan.biome_area_constraints:
        return plan

    biome_targets = [target for target in plan.targets if target.kind == "biome"]
    resolved = _resolve_target_in_text(message)
    if resolved and resolved[0] == "biome" and resolved[1] not in {target.id for target in biome_targets}:
        biome_targets.append(Target(kind=resolved[0], id=resolved[1], label=resolved[2]))
    if not biome_targets:
        return plan

    area_target = biome_targets[-1]
    if any(target.id == "ocean" for target in biome_targets) and re.search(r"(?:大海|海洋|海).{0,12}(?:大一点|大一些|大点|比较大|足够大|面积大|规模大)", message):
        area_target = next(target for target in biome_targets if target.id == "ocean")
    elif largest_requested and len(biome_targets) == 1:
        area_target = biome_targets[0]

    explicit_area = None
    if area_match:
        explicit_area = int(float(area_match.group(1)) * (10000 if area_match.group(2) else 1))
    default_min_area = 1_000_000 if area_target.id == "ocean" else 100_000
    min_area = explicit_area if explicit_area is not None else None if largest_requested else default_min_area

    constraints = [dict(item) for item in plan.biome_area_constraints]
    existing = next((item for item in constraints if str(item.get("target")) == area_target.id), None)
    if existing is None:
        constraints.append(
            {
                "target": area_target.id,
                "min_area": min_area,
                "preference": "largest" if largest_requested else "larger",
            }
        )
    else:
        if existing.get("min_area") is None and min_area is not None:
            existing["min_area"] = min_area
        if largest_requested:
            existing["preference"] = "largest"

    targets = list(plan.targets)
    if area_target.id not in {target.id for target in targets}:
        targets.append(area_target)
    objective = "largest_area" if largest_requested else plan.objective
    return plan.model_copy(
        update={
            "targets": targets,
            "biome_area_constraints": constraints,
            "objective": objective,
            "sort_by": "largest_area" if largest_requested else plan.sort_by,
            "nearest_to_player": False if largest_requested else plan.nearest_to_player,
            "capability": "approximate",
            "unsupported_reason": None,
            "fallback_suggestion": None,
        }
    )


def _extract_json_object(content: str) -> dict:
    text = (content or "").strip()
    if not text:
        raise json.JSONDecodeError("empty content", content, 0)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        fenced = "\n".join(lines).strip()
        try:
            data = json.loads(fenced)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            text = fenced

    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            data, _end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise json.JSONDecodeError("no json object found", content, 0)


def _retry_delay(attempt: int) -> float:
    return min(1.5, 0.25 * attempt)


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


async def deepseek_plan(
    message: str,
    context: dict,
    api_key: Optional[str],
    base_url: str,
    model: str,
    progress: Optional[ProgressCallback] = None,
) -> tuple[str, SearchPlan, list[str]]:
    if not api_key:
        return "needs_api_key", _blocked_plan("未填写 DeepSeek API Key，系统不会启用本地关键词兜底。"), ["未填写 DeepSeek API Key，搜索未运行。"]

    requested_model = (model or "deepseek-v4-flash").strip()
    actual_model = LEGACY_MODELS.get(requested_model, requested_model)
    warnings = []
    if actual_model != requested_model:
        warnings.append(f"DeepSeek 模型 {requested_model} 已按当前 API 文档自动切换为 {actual_model}。")

    structures = {k: v["label"] for k, v in STRUCTURES.items()}
    biomes = {k: v["label"] for k, v in BIOMES.items()}
    system = (
        "你是 Minecraft Java 种子地点搜索规划器。只输出一个原始 JSON 对象。"
        "不要使用 Markdown，不要使用 ```json 代码块，不要输出解释文字，不要编造坐标。"
        "把用户需求转换为本地搜索工具参数。"
        "kind 只能是 structure 或 biome；id 必须来自给定目录。"
        "用户说樱花树时使用 biome/cherry_grove。"
        "用户说试炼之地、审判密室时使用 structure/trial_chambers。"
        "用户说“村庄坐落于平原上 / 村庄在平原上 / 平原村庄”时，targets 必须同时包含 village 和 plains，"
        "并在 adjacency 中输出 {\"a\":\"village\",\"b\":\"plains\",\"threshold\":0,\"relation\":\"same_biome\"}。"
        "用户说“X 附近 N 格内有 Y”时，在 adjacency 中输出 {\"a\":\"X的id\",\"b\":\"Y的id\",\"threshold\":N}；"
        "如果一句话是“村庄附近1500格以内有女巫小屋、劫掠塔”，要拆成 village-witch_hut 和 village-pillager_outpost 两条约束。"
        "用户说“相互距离不超过 N 格 / 两两不超过 N 格”时，输出 pairwise_max_distance=N。"
        "用户说“X 和 Y 距离至少 N 格 / 不要靠近 / 远离”时，不要把 Y 当作正向目标；"
        "输出 exclude_targets=[{\"a\":\"X的id\",\"b\":\"Y的id\",\"min_distance\":N}]。"
        "用户说“X 不在某群系 / X 不能在沙漠和金合欢”时，输出 exclude_biomes=[{\"target\":\"X的id\",\"biomes\":[\"desert\",\"savanna\"]}]。"
        "用户说“距离在 A 到 B 格之间”时，在 adjacency 中同时输出 min_threshold=A 和 threshold=B。"
        "用户说“往东/西/南/北找”或给出坐标范围时，输出 area，例如 {\"direction\":\"east\"} 或 {\"min_x\":0,\"max_x\":10000}。"
        "用户说“至少 N 个 / N 个以上”时，输出 count_constraints=[{\"target\":\"目标id\",\"min\":N,\"within\":半径或null}]。"
        "用户要求按最紧凑、离中心最近、锚点最近排序时，输出 sort_by=compact/site_distance/anchor_distance；默认 nearest。"
        "用户说“核对/验证 x,z 是不是某群系/是什么群系”时，objective 输出 verify，verify_point 输出 {\"x\":x,\"z\":z}；"
        "如果只是问是什么群系，targets 可以为空且 capability=supported。"
        "用户说“紧邻”但没有给距离时，默认 threshold=512。"
        "用户说“A 背靠 B、面朝 C”“B 和 C 分居 A 两侧”时，targets 必须包含 A/B/C，并输出 "
        "relative_layout=[{\"center\":\"A的id\",\"back\":\"B的id\",\"front\":\"C的id\",\"relation\":\"opposite_sides\",\"min_angle\":120,\"back_max_distance\":512,\"front_max_distance\":512}]；"
        "同时在 adjacency 中输出 A-B 和 A-C 两条距离约束。未给距离时两侧均默认 512 格、最小夹角 120 度。这个约束当前后端支持，capability 输出 supported。"
        "下界要塞、堡垒遗迹属于下界，末地城属于末地；不要把不同维度目标规划成同一个距离组合。"
        "如果用户要求主世界目标和下界/末地目标互相距离、紧邻或同一区域，capability 输出 unsupported，并说明跨维度距离当前不能混算。"
        "用户要求最大群系、最大岛、最大海或面积最大的某群系时，objective=largest_area、sort_by=largest_area，"
        "并输出 biome_area_constraints=[{\"target\":\"群系id\",\"min_area\":null,\"preference\":\"largest\"}]，capability=approximate。"
        "用户说某群系要大一点、比较大或足够大时，输出 biome_area_constraints；海洋默认 min_area=1000000，其他群系默认 min_area=100000，preference=larger。"
        "群系面积是确定性抽样候选加连通区域测量，属于近似能力。最高山、最高海拔、宝箱最多、战利品最多、宝库最多、不详宝箱最多、刷怪笼最多仍输出 unsupported。"
        "如果用户只是要求最近、附近、若干目标在 N 格内、A 在 B 生物群系上、A 附近有 B，则 capability 输出 supported。"
        "示例 json：{\"targets\":[{\"kind\":\"structure\",\"id\":\"village\",\"label\":\"村庄\"}],"
        "\"pairwise_max_distance\":null,\"adjacency\":[],\"nearest_to_player\":true,"
        "\"anchor_target_id\":\"village\",\"objective\":\"nearest\",\"capability\":\"supported\","
        "\"unsupported_reason\":null,\"fallback_suggestion\":null,\"notes\":[]}"
    )
    schema_hint = {
        "targets": [{"kind": "structure|biome", "id": "village", "label": "村庄"}],
        "pairwise_max_distance": 2000,
        "adjacency": [
            {"a": "village", "b": "plains", "threshold": 0, "relation": "same_biome"},
            {"a": "village", "b": "cherry_grove", "threshold": 500},
            {"a": "village", "b": "pillager_outpost", "min_threshold": 300, "threshold": 800},
            {"a": "village", "b": "witch_hut", "threshold": 1500},
        ],
        "relative_layout": [
            {
                "center": "village",
                "back": "cherry_grove",
                "front": "ocean",
                "relation": "opposite_sides",
                "min_angle": 120,
                "back_max_distance": 512,
                "front_max_distance": 512,
            }
        ],
        "exclude_biomes": [{"target": "pillager_outpost", "biomes": ["desert", "savanna"]}],
        "exclude_targets": [{"a": "village", "b": "woodland_mansion", "min_distance": 2000}],
        "count_constraints": [{"target": "village", "min": 2, "within": 1000}],
        "biome_area_constraints": [{"target": "ocean", "min_area": 1000000, "preference": "larger"}],
        "area": {"direction": "east", "min_x": None, "max_x": None, "min_z": None, "max_z": None},
        "verify_point": {"x": 0, "z": 0},
        "sort_by": "nearest|compact|site_distance|anchor_distance",
        "nearest_to_player": True,
        "anchor_target_id": "village",
        "objective": "nearest|cluster|verify|largest_area|highest_elevation|most_loot|most_vaults|locate",
        "capability": "supported|unsupported|approximate",
        "unsupported_reason": "如果 capability=unsupported，用中文说明后端缺少什么核心能力",
        "fallback_suggestion": "可执行的替代查询，例如先定位最近的试炼密室",
        "notes": ["中文说明"],
    }
    base_messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "message": message,
                    "context": context,
                    "allowed_structures": structures,
                    "allowed_biomes": biomes,
                    "output_schema": schema_hint,
                },
                ensure_ascii=False,
            ),
        },
    ]
    url = _chat_completions_url(base_url)
    last_error = "未知错误"
    last_preview = ""
    timeout = httpx.Timeout(DEEPSEEK_REQUEST_TIMEOUT, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, DEEPSEEK_PLAN_ATTEMPTS + 1):
            if progress:
                progress(
                    stage="ai",
                    message="DeepSeek 正在解析",
                    checked=None,
                    total=None,
                    radius=None,
                )
            messages = list(base_messages)
            if attempt > 1:
                messages.append(
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "previous_attempt_failed": last_error,
                                "retry_instruction": "重新输出一个严格合法的原始 JSON 对象。不要 Markdown，不要解释，不要代码块；字段必须符合 output_schema，id 必须来自 allowed_structures/allowed_biomes。",
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
            payload = {
                "model": actual_model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "max_tokens": 1000,
                "temperature": 0.1,
            }

            try:
                resp = await client.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
            except Exception as exc:
                last_error = f"{type(exc).__name__}"
                if attempt < DEEPSEEK_PLAN_ATTEMPTS:
                    if progress:
                        progress(
                            stage="ai",
                            message="DeepSeek 正在解析",
                            checked=None,
                            total=None,
                            radius=None,
                        )
                    await asyncio.sleep(_retry_delay(attempt))
                    continue
                return (
                    "deepseek_error",
                    _blocked_plan(f"DeepSeek 调用失败：{last_error}。", "请稍后重试，或换用另一个 DeepSeek 模型。"),
                    warnings + [f"DeepSeek 解析失败：{last_error}，搜索未运行。"],
                )

            if resp.status_code >= 400:
                detail = _error_detail(resp)
                last_error = f"HTTP {resp.status_code} {detail}".strip()
                if _is_retryable_http_status(resp.status_code) and attempt < DEEPSEEK_PLAN_ATTEMPTS:
                    if progress:
                        progress(
                            stage="ai",
                            message="DeepSeek 正在解析",
                            checked=None,
                            total=None,
                            radius=None,
                        )
                    await asyncio.sleep(_retry_delay(attempt))
                    continue
                return (
                    "deepseek_error",
                    _blocked_plan(f"DeepSeek 调用失败：{last_error}", "请检查 API Key、模型和 Base URL 后重试。"),
                    warnings + [f"DeepSeek 解析失败：{last_error}，搜索未运行。"],
                )

            content = ""
            try:
                message_payload = resp.json()["choices"][0]["message"]
                content = message_payload.get("content") or ""
                if not content and isinstance(message_payload.get("tool_calls"), list):
                    for tool_call in message_payload["tool_calls"]:
                        function_payload = tool_call.get("function") if isinstance(tool_call, dict) else None
                        arguments = function_payload.get("arguments") if isinstance(function_payload, dict) else None
                        if arguments:
                            content = arguments
                            break
                if not content:
                    raise ValueError("DeepSeek 返回空内容")
                parsed = _extract_json_object(content)
                normalized, normalize_warnings = _normalize_plan_data(parsed)
                plan = _augment_biome_area_from_message(
                    message,
                    _augment_relative_layout_from_message(message, _validate_plan_or_raise(normalized)),
                )
                warnings.extend(normalize_warnings)
                if progress:
                    progress(
                        stage="ai",
                        message="DeepSeek 解析完成，正在启动搜索",
                        checked=None,
                        total=None,
                        radius=None,
                    )
                return "deepseek_json", plan, warnings
            except Exception as parse_exc:
                last_preview = (content or resp.text).replace("\n", " ").strip()[:180]
                last_error = f"{type(parse_exc).__name__}: {str(parse_exc)[:160]}"
                if attempt < DEEPSEEK_PLAN_ATTEMPTS:
                    if progress:
                        progress(
                            stage="ai",
                            message="DeepSeek 正在解析",
                            checked=None,
                            total=None,
                            radius=None,
                        )
                    await asyncio.sleep(_retry_delay(attempt))
                    continue

    return (
        "deepseek_error",
        _blocked_plan(
            f"DeepSeek 返回内容无法解析：{last_error}。",
            f"请重试或换用支持 JSON 输出的模型。最后返回开头：{last_preview}",
        ),
        warnings
        + [f"DeepSeek 返回内容无法解析，搜索未运行。最后返回开头：{last_preview}"],
    )


def _chat_completions_url(base_url: str) -> str:
    base = (base_url or "https://api.deepseek.com").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _models_url(base_url: str) -> str:
    base = (base_url or "https://api.deepseek.com").rstrip("/")
    if base.endswith("/models"):
        return base
    return f"{base}/models"


async def list_deepseek_models(api_key: Optional[str], base_url: str) -> tuple[list[dict], list[str]]:
    if not api_key:
        return DEFAULT_MODELS, ["未提供 DeepSeek API Key，显示内置模型候选。"]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_models_url(base_url), headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code >= 400:
            return DEFAULT_MODELS, [f"拉取 DeepSeek 模型失败：HTTP {resp.status_code} {_error_detail(resp)}，显示内置模型候选。"]
        data = resp.json()
        items = data.get("data", []) if isinstance(data, dict) else []
        models = []
        for item in items:
            model_id = item.get("id") if isinstance(item, dict) else None
            if model_id:
                models.append({"id": model_id, "name": model_id, "source": "deepseek"})
        return (models or DEFAULT_MODELS), [] if models else ["DeepSeek 未返回模型列表，显示内置模型候选。"]
    except Exception as exc:
        return DEFAULT_MODELS, [f"拉取 DeepSeek 模型失败：{type(exc).__name__}，显示内置模型候选。"]


def _error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "").strip()
        if isinstance(data, dict):
            return str(data.get("message") or data.get("detail") or "").strip()
    except Exception:
        pass
    return resp.text[:180].strip()
