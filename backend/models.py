from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

WORLD_SEARCH_RADIUS = 30_000_000


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=6, max_length=200)


class SettingsIn(BaseModel):
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    seed: str = "0"
    version: str = "26.2"
    center_x: int = 0
    center_z: int = 0
    search_radius: int = Field(default=WORLD_SEARCH_RADIUS, ge=1)
    max_results: int = Field(default=1, ge=1, le=20)

    @field_validator("center_x", "center_z", mode="before")
    @classmethod
    def default_coord(cls, value):
        if value in (None, ""):
            return 0
        return value

    @field_validator("search_radius", mode="before")
    @classmethod
    def default_radius(cls, value):
        if value in (None, ""):
            return WORLD_SEARCH_RADIUS
        return value

    @field_validator("max_results", mode="before")
    @classmethod
    def default_limit(cls, value):
        if value in (None, ""):
            return 1
        return value


class SettingsOut(BaseModel):
    deepseek_api_key_set: bool
    deepseek_base_url: str
    deepseek_model: str
    seed: str
    version: str
    center_x: int
    center_z: int
    search_radius: int
    max_results: int
    key_storage: str


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=3000)
    request_id: Optional[str] = Field(default=None, max_length=80)


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=3000)
    request_id: Optional[str] = Field(default=None, max_length=80)
    seed: str = "0"
    version: str = "26.2"
    center_x: int = 0
    center_z: int = 0
    search_radius: int = Field(default=WORLD_SEARCH_RADIUS, ge=1)
    max_results: int = Field(default=1, ge=1, le=20)
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    @field_validator("center_x", "center_z", mode="before")
    @classmethod
    def default_coord(cls, value):
        if value in (None, ""):
            return 0
        return value

    @field_validator("search_radius", mode="before")
    @classmethod
    def default_radius(cls, value):
        if value in (None, ""):
            return WORLD_SEARCH_RADIUS
        return value

    @field_validator("max_results", mode="before")
    @classmethod
    def default_limit(cls, value):
        if value in (None, ""):
            return 1
        return value


class UnmetFeedbackIn(BaseModel):
    query: str = Field(min_length=1, max_length=3000)
    request_id: Optional[str] = Field(default=None, max_length=80)
    reason: Literal["not_solved", "wrong_result", "missing_capability", "other"] = "not_solved"
    detail: Optional[str] = Field(default=None, max_length=1000)
    planner: Optional[str] = Field(default=None, max_length=80)
    plan: Optional[dict[str, Any]] = None

    @field_validator("plan")
    @classmethod
    def limit_plan_size(cls, value):
        if value is not None and len(repr(value)) > 50_000:
            raise ValueError("plan payload is too large")
        return value


class ModelsIn(BaseModel):
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"


class MapIn(BaseModel):
    seed: str = "0"
    version: str = "26.2"
    center_x: int = 0
    center_z: int = 0
    radius: int = Field(default=1024, ge=1)
    size: int = Field(default=128, ge=16, le=256)


class Target(BaseModel):
    kind: Literal["structure", "biome"]
    id: str
    label: str


class SearchPlan(BaseModel):
    targets: list[Target]
    pairwise_max_distance: Optional[int] = None
    adjacency: list[dict[str, Any]] = Field(default_factory=list)
    relative_layout: list[dict[str, Any]] = Field(default_factory=list)
    exclude_biomes: list[dict[str, Any]] = Field(default_factory=list)
    exclude_targets: list[dict[str, Any]] = Field(default_factory=list)
    count_constraints: list[dict[str, Any]] = Field(default_factory=list)
    biome_area_constraints: list[dict[str, Any]] = Field(default_factory=list)
    area: Optional[dict[str, Any]] = None
    verify_point: Optional[dict[str, Any]] = None
    sort_by: str = "nearest"
    nearest_to_player: bool = True
    anchor_target_id: Optional[str] = None
    objective: str = "nearest"
    capability: str = "supported"
    unsupported_reason: Optional[str] = None
    fallback_suggestion: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


class ChatOut(BaseModel):
    reply: str
    planner: str
    plan: SearchPlan
    results: list[dict[str, Any]]
    warnings: list[str] = Field(default_factory=list)
