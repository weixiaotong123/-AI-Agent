from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.config import SessionLocal
from app.models.db_models import (
    DestinationPreferenceRecord,
    GlobalConstraintRecord,
    GlobalPreferenceRecord,
)


# ---------- 读写：全局硬约束 ----------

def add_global_constraint(user_id: str, constraint_type: str, value: str) -> None:
    """新增一条全局硬约束（过敏、忌口等）。不做覆盖，允许同类型多条。"""
    session = SessionLocal()
    try:
        existing = (
            session.query(GlobalConstraintRecord)
            .filter(
                GlobalConstraintRecord.user_id == user_id,
                GlobalConstraintRecord.constraint_type == constraint_type,
                GlobalConstraintRecord.value == value,
            )
            .first()
        )
        if existing is not None:
            return
        session.add(
            GlobalConstraintRecord(
                user_id=user_id, constraint_type=constraint_type, value=value
            )
        )
        session.commit()
    finally:
        session.close()


def list_global_constraints(user_id: str) -> list[dict]:
    """读取用户的全部全局硬约束。"""
    session = SessionLocal()
    try:
        records = (
            session.query(GlobalConstraintRecord)
            .filter(GlobalConstraintRecord.user_id == user_id)
            .all()
        )
        return [
            {"type": r.constraint_type, "value": r.value} for r in records
        ]
    finally:
        session.close()


# ---------- 读写：全局软偏好 ----------

def upsert_global_preference(user_id: str, key: str, value: str) -> None:
    """写入或更新一条全局偏好。"""
    session = SessionLocal()
    try:
        existing = (
            session.query(GlobalPreferenceRecord)
            .filter(
                GlobalPreferenceRecord.user_id == user_id,
                GlobalPreferenceRecord.preference_key == key,
            )
            .first()
        )
        if existing is not None:
            existing.preference_value = value
        else:
            session.add(
                GlobalPreferenceRecord(
                    user_id=user_id, preference_key=key, preference_value=value
                )
            )
        session.commit()
    finally:
        session.close()


def list_global_preferences(user_id: str) -> dict[str, str]:
    session = SessionLocal()
    try:
        records = (
            session.query(GlobalPreferenceRecord)
            .filter(GlobalPreferenceRecord.user_id == user_id)
            .all()
        )
        return {r.preference_key: r.preference_value for r in records}
    finally:
        session.close()


# ---------- 读写：目的地偏好 ----------

def upsert_destination_preference(
    user_id: str, destination: str, key: str, value: str
) -> None:
    session = SessionLocal()
    try:
        existing = (
            session.query(DestinationPreferenceRecord)
            .filter(
                DestinationPreferenceRecord.user_id == user_id,
                DestinationPreferenceRecord.destination == destination,
                DestinationPreferenceRecord.preference_key == key,
            )
            .first()
        )
        if existing is not None:
            existing.preference_value = value
        else:
            session.add(
                DestinationPreferenceRecord(
                    user_id=user_id,
                    destination=destination,
                    preference_key=key,
                    preference_value=value,
                )
            )
        session.commit()
    finally:
        session.close()


def list_destination_preferences(user_id: str, destination: str) -> dict[str, str]:
    session = SessionLocal()
    try:
        records = (
            session.query(DestinationPreferenceRecord)
            .filter(
                DestinationPreferenceRecord.user_id == user_id,
                DestinationPreferenceRecord.destination == destination,
            )
            .all()
        )
        return {r.preference_key: r.preference_value for r in records}
    finally:
        session.close()


# ---------- 三级优先级仲裁 ----------

@dataclass
class ResolvedPreferences:
    hard_constraints: list[dict] = field(default_factory=list)
    effective_preferences: dict[str, dict] = field(default_factory=dict)


def resolve_preferences(
    user_id: str,
    destination: str,
    current_instruction_overrides: dict[str, str] | None = None,
) -> ResolvedPreferences:
    """按优先级合并：当前指令 > 全局偏好 > 目的地偏好。硬约束单独返回，永远生效。"""
    hard_constraints = list_global_constraints(user_id)
    destination_prefs = list_destination_preferences(user_id, destination)
    global_prefs = list_global_preferences(user_id)
    current_overrides = current_instruction_overrides or {}

    merged: dict[str, dict] = {}
    for key, value in destination_prefs.items():
        merged[key] = {"value": value, "source": "destination"}
    for key, value in global_prefs.items():
        merged[key] = {"value": value, "source": "global"}
    for key, value in current_overrides.items():
        merged[key] = {"value": value, "source": "current"}

    return ResolvedPreferences(
        hard_constraints=hard_constraints, effective_preferences=merged
    )


# ---------- 从自然语言指令抽取结构化偏好 ----------

class ExtractedPreference(BaseModel):
    """从一句用户指令中识别出的一条偏好或约束。"""

    category: str = Field(
        ..., description="constraint（安全类硬约束，如过敏）或 preference（软偏好，如口味、节奏）"
    )
    key: str = Field(..., description="偏好类型，如 dietary / hotel_level / pace")
    value: str = Field(..., description="具体内容，如 '海鲜过敏' / '少辣' / '近古城'")
    scope: str = Field(
        ..., description="global（长期生效）、destination（仅本目的地）或 session（仅本次）"
    )
    destination: str | None = Field(
        default=None,
        description="scope=destination 时的目标城市，其它 scope 为 null",
    )


class ExtractionResult(BaseModel):
    items: list[ExtractedPreference] = Field(default_factory=list)


def _normalize_preference_scopes(
    instruction: str,
    items: list[ExtractedPreference],
) -> list[ExtractedPreference]:
    has_destination_scope = re.search(
        r"(?:去|到|在)[^，。；,;]{1,12}(?:时|期间)", instruction
    ) is not None
    from app.services.city_registry_service import CURATED_CITY_REGISTRY

    mentioned_destination = next(
        (
            entry.city
            for entry in sorted(
                CURATED_CITY_REGISTRY.values(),
                key=lambda entry: max(map(len, entry.aliases)),
                reverse=True,
            )
            if any(alias in instruction for alias in entry.aliases)
        ),
        None,
    )

    return [
        item.model_copy(
            update={
                "scope": "destination",
                "destination": mentioned_destination or item.destination,
            }
        )
        if item.category == "preference"
        and item.scope == "global"
        and has_destination_scope
        else item.model_copy(
            update={"destination": mentioned_destination or item.destination}
        )
        if item.category == "preference" and item.scope == "destination"
        else item
        for item in items
    ]


def extract_preferences_from_instruction(
    instruction: str,
) -> list[ExtractedPreference]:
    """用 LLM 从一句自然语言指令中抽取结构化偏好，抽取失败时返回空列表（不阻塞主流程）。"""
    from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MAX_RETRIES, LLM_MODEL, LLM_TIMEOUT_SECONDS

    if not LLM_API_KEY:
        return []

    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        return []

    llm = ChatOpenAI(
        model=LLM_MODEL,
        temperature=0,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL or None,
        timeout=LLM_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
    )

    system_prompt = (
    "你是旅行偏好抽取助手。从用户的一句旅行指令中，只识别出明确、长期、可复用的偏好或约束。"
    "严格区分以下三类，不要混淆：\n"
    "1. category=constraint：仅限过敏、忌口、宗教饮食限制等安全或原则性硬约束，"
    "错误执行会造成实际伤害或严重不适的信息。\n"
    "2. category=preference：口味浓淡、住宿档次、旅行节奏、预算范围等软性偏好，"
    "可以被后续更明确的指令覆盖，不属于安全问题。\n"
    "3. 不要提取：仅针对当前这一次编辑的临时性描述（如'这一天轻松一点'"
    "'这次不要太赶'），这些是一次性指令，不代表长期偏好，一律不提取，返回空列表。\n"
    "如果用户明确说'这次可以...''仅此次...''这一趟...'，"
    "提取为 scope=session，且要连同其暗示的长期基线（如果提到）一并给出对应 global 条目。\n"
    "只输出 JSON 对象，不要输出解释或 Markdown。"
    )
    human_prompt = f"""
        用户指令：{instruction}

        如果是目的地偏好，destination 必须填用户提到的目标城市规范名；其它情况填 null。

        参考示例：
        - "我对花生过敏" → constraint, dietary, "花生过敏", global
        - "我喜欢经济型酒店" → preference, hotel_level, "经济型", global
        - "这一天节奏轻松一点" → 不提取，返回空 items
        - "这次在大理可以吃辣，平时不太能吃辣" → 两条：
        1) preference, dietary, "可以吃辣", session
        2) preference, dietary, "不吃辣/清淡", global

        请按以下 JSON 结构输出（如果没有值得记住的信息，items 返回空列表）：
        {{
        "items": [
            {{"category": "constraint", "key": "dietary", "value": "海鲜过敏", "scope": "global", "destination": null}}
        ]
        }}
        """

    try:
        response = llm.invoke(
            [("system", system_prompt), ("human", human_prompt)]
        )
        raw_text = getattr(response, "content", "")
        if isinstance(raw_text, list):
            raw_text = "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in raw_text
            )
        start = str(raw_text).find("{")
        end = str(raw_text).rfind("}")
        if start == -1 or end == -1:
            return []
        parsed = ExtractionResult.model_validate(json.loads(str(raw_text)[start : end + 1]))
        return _normalize_preference_scopes(instruction, parsed.items)
    except Exception:
        return []


def save_extracted_preferences(
    user_id: str, destination: str, items: list[ExtractedPreference]
) -> None:
    """把抽取结果按 scope 落库；session 级不落库。"""
    for item in items:
        if item.category == "constraint":
            add_global_constraint(user_id, item.key, item.value)
        elif item.scope == "global":
            upsert_global_preference(user_id, item.key, item.value)
        elif item.scope == "destination":
            target_destination = item.destination or destination
            upsert_destination_preference(
                user_id, target_destination, item.key, item.value
            )
        # scope == "session" 的不落库，只在本次生效