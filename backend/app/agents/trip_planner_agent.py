from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.tools.rag_tool import get_destination_guide_context
from app.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MAX_RETRIES,
    LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
)
from app.models.schemas import DayPlan, TripEditRequest, TripRequest
from app.services.memory_service import ResolvedPreferences
from app.services.place_candidate_service import (
    CityCandidatePool,
    PlaceCandidate,
    PlaceCandidateCategory,
)


class PlannerDayDraft(BaseModel):
    """LLM 返回的单日最小行程草稿。"""

    day_index: int = Field(..., ge=1)
    theme: str = Field(..., description="当天的简短主题")
    spot_name: str = Field(..., description="当天主要景点名称")
    spot_description: str = Field(..., description="推荐该景点的简短理由")
    meal_name: str = Field(..., description="当天的餐饮或餐厅建议")
    meal_notes: str = Field(..., description="简短的用餐说明")
    daily_note: str = Field(..., description="当天的一条简短规划备注")
    hotel_name: str = Field(..., description="当天入住的酒店名称")

class PlannerDraft(BaseModel):
    """提供给 trip_service.py 使用的结构化行程草稿。"""

    summary: str = Field(..., description="整趟旅行的简短概述")
    tips: list[str] = Field(default_factory=list, description="旅行提示")
    days: list[PlannerDayDraft] = Field(default_factory=list)


class DynamicPlannerDayDraft(BaseModel):
    """动态城市 Planner 只通过 POI ID 选择当天实体。"""

    day_index: int = Field(..., ge=1)
    theme: str = Field(..., description="当天主题")
    spot_poi_id: str = Field(..., description="候选池中的景点 POI ID")
    spot_start_time: str = Field(default="10:00", pattern=r"^\d{2}:\d{2}$")
    spot_end_time: str = Field(default="12:00", pattern=r"^\d{2}:\d{2}$")
    spot_reason: str = Field(..., description="选择该景点的简短理由")
    meal_poi_id: str = Field(..., description="候选池中的餐饮 POI ID")
    meal_notes: str = Field(..., description="用餐安排说明")
    daily_note: str = Field(..., description="当天安排说明")


class DynamicPlannerDraft(BaseModel):
    """未沉淀城市的受约束行程草稿。"""

    summary: str = Field(..., description="整趟旅行概述")
    tips: list[str] = Field(default_factory=list)
    hotel_poi_id: str = Field(..., description="候选池中的住宿 POI ID")
    days: list[DynamicPlannerDayDraft] = Field(default_factory=list)


class DayEditDraft(BaseModel):
    """LLM 返回的单日编辑草稿。"""

    theme: str = Field(..., description="编辑后的当天主题")
    spot_name: str = Field(..., description="编辑后的主要景点名称")
    spot_description: str = Field(..., description="编辑后的景点说明")
    meal_name: str = Field(..., description="编辑后的餐饮名称")
    meal_notes: str = Field(..., description="编辑后的餐饮说明")
    daily_note: str = Field(..., description="编辑后的当天备注")


class PlannerReviewIssue(BaseModel):
    """Reflection 发现的一条可执行问题。"""

    scope: Literal["local", "replan"]
    day_index: int | None = Field(default=None, ge=1)
    feedback: str = Field(..., min_length=1)


class PlannerReview(BaseModel):
    """对初始 Planner 草稿的结构化评估结果。"""

    issues: list[PlannerReviewIssue] = Field(..., max_length=8)


def _normalize_day_edit_payload(payload: dict) -> dict:
    """兼容模型返回的两种单日编辑格式。"""
    if "spot_name" in payload and "meal_name" in payload and "daily_note" in payload:
        return payload

    normalized = dict(payload)

    spots = payload.get("spots")
    if isinstance(spots, list) and spots:
        first_spot = spots[0] or {}
        normalized.setdefault("spot_name", first_spot.get("name", ""))
        normalized.setdefault("spot_description", first_spot.get("description", ""))

    meals = payload.get("meals")
    if isinstance(meals, list) and meals:
        first_meal = meals[0] or {}
        normalized.setdefault("meal_name", first_meal.get("name", ""))
        normalized.setdefault("meal_notes", first_meal.get("notes", ""))

    notes = payload.get("notes")
    if isinstance(notes, list) and notes:
        normalized.setdefault("daily_note", notes[-1] or "")

    return normalized


def _extract_entity_names(contexts: list[str], pattern: str) -> list[str]:
    """从 RAG 上下文中提取匹配 pattern 的实体名称列表。"""
    names: list[str] = []
    seen: set[str] = set()
    for ctx in contexts:
        for match in re.finditer(pattern, ctx):
            name = match.group(1).strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _extract_json_object(raw_text: str) -> str | None:
    """从模型原始文本中尽量提取 JSON 对象字符串。"""
    text = raw_text.strip()

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    start_index = text.find("{")
    end_index = text.rfind("}")
    if start_index == -1 or end_index == -1 or end_index <= start_index:
        return None

    return text[start_index : end_index + 1]


def _invoke_structured_llm(
    llm,
    system_prompt: str,
    human_prompt: str,
    response_model: type[BaseModel],
    log_prefix: str,
) -> tuple[BaseModel | None, dict[str, int]]:
    """调用模型并解析 JSON 结构；失败时交由 service 保留原草稿或兜底。"""
    empty_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    try:
        response = llm.invoke(
            [("system", system_prompt), ("human", human_prompt)]
        )
    except Exception as exc:
        print(f"[{log_prefix}] 大模型调用失败: {type(exc).__name__}: {exc}")
        return None, empty_usage

    token_usage = _extract_token_usage(response)
    raw_text = getattr(response, "content", "")
    if isinstance(raw_text, list):
        raw_text = "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in raw_text
        )

    json_text = _extract_json_object(str(raw_text))
    if json_text is None:
        print(f"[{log_prefix}] 未能从模型返回中提取 JSON。")
        return None, token_usage

    try:
        return response_model.model_validate(json.loads(json_text)), token_usage
    except Exception as exc:
        print(f"[{log_prefix}] JSON 解析失败: {type(exc).__name__}: {exc}")
        return None, token_usage


def _reflection_context(
    request: TripRequest,
    rag_contexts: list[str],
    resolved_preferences,
    candidate_pool: CityCandidatePool | None,
) -> str:
    context = [
        f"目的地：{request.destination}",
        f"日期：{request.start_date.isoformat()} 至 {request.end_date.isoformat()}",
        f"预算上限：{request.budget}",
        f"人数：{request.travelers}",
        f"用户偏好：{'、'.join(request.preferences) if request.preferences else '无'}",
        f"节奏：{request.pace or '适中'}",
        f"饮食偏好：{'、'.join(request.dietary_preferences) if request.dietary_preferences else '无'}",
        f"额外要求：{request.special_notes or '无'}",
        _build_memory_text(resolved_preferences),
    ]
    if candidate_pool is not None:
        candidates = {
            category.value: _dynamic_candidate_payload(
                candidate_pool.candidates_for(category)
            )
            for category in PlaceCandidateCategory
        }
        context.append(
            "可用真实 POI 候选（不得超出）："
            + json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))
        )
    elif rag_contexts:
        context.append("本地攻略依据：\n" + "\n\n".join(rag_contexts))
    return "\n".join(item for item in context if item)


def review_planner_draft(
    request: TripRequest,
    draft: PlannerDraft | DynamicPlannerDraft,
    rag_contexts: list[str],
    resolved_preferences=None,
    candidate_pool: CityCandidatePool | None = None,
) -> tuple[PlannerReview | None, dict[str, int]]:
    """评估草稿，只返回可操作的问题；没有问题时 issues 为空。"""
    empty_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    llm = _build_chat_llm()
    if llm is None:
        return None, empty_usage

    system_prompt = (
        "你是旅行行程质量审查助手。检查草稿是否违反用户硬约束、遗漏要求、"
        "安排不完整、节奏不合理或在已知信息范围内存在明显路线冲突。"
        "预算金额和 POI 真实性以程序及提供的数据为准；不得推测实时价格、"
        "开放时间或路线耗时。只报告明确、可修复的问题；没有问题返回空 issues。"
        "局部问题标 scope=local 并给 day_index；跨多天或整体不成立标 scope=replan。"
        "每条 feedback 必须具体说明问题和期望修正。只输出 JSON。"
    )
    human_prompt = (
        f"请求与可用依据：\n{_reflection_context(request, rag_contexts, resolved_preferences, candidate_pool)}"
        f"\n\n待审查草稿：\n{draft.model_dump_json()}"
        '\n\n输出格式：{"issues":[{"scope":"local","day_index":1,"feedback":"具体问题与修正要求"}]}。'
        "整体问题的 day_index 可为 null。"
    )
    result, usage = _invoke_structured_llm(
        llm,
        system_prompt,
        human_prompt,
        PlannerReview,
        "planner_reflection_review",
    )
    return (result if isinstance(result, PlannerReview) else None), usage


def revise_planner_draft(
    request: TripRequest,
    draft: PlannerDraft | DynamicPlannerDraft,
    review: PlannerReview,
    day_count: int,
    rag_contexts: list[str],
    resolved_preferences=None,
    candidate_pool: CityCandidatePool | None = None,
) -> tuple[PlannerDraft | DynamicPlannerDraft | None, dict[str, int]]:
    """按一次 Reflection 反馈修订草稿；服务层负责再次执行硬校验。"""
    empty_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    llm = _build_chat_llm()
    if llm is None or not review.issues:
        return None, empty_usage

    response_model = (
        DynamicPlannerDraft
        if isinstance(draft, DynamicPlannerDraft)
        else PlannerDraft
    )
    system_prompt = (
        "你是旅行行程修订助手。只根据审查反馈修复草稿，未涉及内容尽量保持不变。"
        "严格遵守用户约束与提供的事实依据。动态城市只能使用候选池中的原始 POI ID，"
        "不得编造或更改 ID；不得编造价格、开放时间或路线耗时。"
        "必须返回完整且符合原草稿字段结构的 JSON，不要输出解释。"
    )
    human_prompt = (
        f"请求与可用依据：\n{_reflection_context(request, rag_contexts, resolved_preferences, candidate_pool)}"
        f"\n\n当前草稿：\n{draft.model_dump_json()}"
        f"\n\n必须处理的问题：\n{review.model_dump_json()}"
        f"\n\n请返回恰好 {day_count} 天的完整修订草稿。"
    )
    result, usage = _invoke_structured_llm(
        llm,
        system_prompt,
        human_prompt,
        response_model,
        "planner_reflection_revision",
    )
    if not isinstance(result, response_model):
        return None, usage
    if len(result.days) != day_count or sorted(day.day_index for day in result.days) != list(
        range(1, day_count + 1)
    ):
        print("[planner_reflection_revision] 修订草稿天数或 day_index 不匹配。")
        return None, usage
    return result, usage


def collect_trip_context(
    destination: str,
    preferences: list[str] | None = None,
    pace: str | None = None,
    special_notes: str | None = None,
    top_k: int = 5,
) -> tuple[list[str], dict[str, int], dict[str, int], dict[str, int]]:
    """收集本地攻略片段。返回 (contexts, rewrite_usage, rerank_usage, embedding_usage)。"""
    return get_destination_guide_context(
        destination=destination,
        preferences=preferences,
        pace=pace,
        special_notes=special_notes,
        top_k=top_k,
    )


def _build_chat_llm():
    """创建通用 ChatOpenAI 实例。"""
    if not LLM_API_KEY:
        return None

    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        return None

    return ChatOpenAI(
        model=LLM_MODEL,
        temperature=0.3,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL or None,
        timeout=LLM_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
    )


def _extract_token_usage(response) -> dict[str, int]:
    """从 LangChain AIMessage 中提取 token 使用量。"""
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    metadata = getattr(response, "response_metadata", None) or {}
    token_usage = metadata.get("token_usage", {})
    if token_usage:
        usage["prompt_tokens"] = token_usage.get("prompt_tokens", 0)
        usage["completion_tokens"] = token_usage.get("completion_tokens", 0)
    return usage


def generate_planner_draft(
    request: TripRequest,
    rag_contexts: list[str],
    day_count: int,
    resolved_preferences: "ResolvedPreferences | None" = None,
) -> tuple[PlannerDraft | None, dict[str, int]]:
    """
    使用 LangChain 生成结构化行程草稿。返回 (draft, token_usage)。

    如果当前环境还没有准备好模型调用条件，就返回 None，
    这样 service 层还能回退到规则版实现。
    """
    empty_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    llm = _build_chat_llm()
    if llm is None:
        return None, empty_usage

    guide_context = "\n\n".join(rag_contexts) if rag_contexts else "暂无本地攻略上下文。"

    # 从上下文中提取真实商户名，强制 LLM 使用
    hotel_names = _extract_entity_names(rag_contexts, r"【([^】]*(?:酒店|民宿|客栈|宾馆|饭店|旅馆|旅店)[^】]*)】")
    restaurant_names = _extract_entity_names(rag_contexts, r"【([^】]*(?:餐厅|小吃|火锅|菜|饭|面|汤|粥|粉|私房菜)[^】]*)】")
    hotel_list = "、".join(hotel_names[:6]) if hotel_names else "暂无"
    restaurant_list = "、".join(restaurant_names[:6]) if restaurant_names else "暂无"
    memory_text = ""
    if resolved_preferences is not None:
        constraint_lines = [
            f"- {c['type']}：{c['value']}（禁止违反）"
            for c in resolved_preferences.hard_constraints
        ]
        preference_lines = [
            f"- {key}：{item['value']}（来源：{item['source']}）"
            for key, item in resolved_preferences.effective_preferences.items()
        ]
        if constraint_lines or preference_lines:
            memory_text = "\n\n用户长期记忆：\n"
            if constraint_lines:
                memory_text += "硬性约束（必须遵守）：\n" + "\n".join(constraint_lines) + "\n"
            if preference_lines:
                memory_text += "长期偏好（尽量参考，若与本次额外要求冲突，以本次要求为准）：\n" + "\n".join(preference_lines)
    system_prompt = (
        "你是一名旅行规划助手。"
        "请用中文生成简洁的结构化旅行草稿。"
        "需要遵守用户给出的目的地、预算、节奏和本地攻略上下文。"
        "你必须只输出一个 JSON 对象，不要输出 Markdown，不要输出解释文字，不要输出代码块。"
        "输出内容必须严格符合给定的结构化字段要求。"
        "餐饮和住宿必须使用上下文中提供的真实商户名称，禁止使用泛称。"
        "如果用户在额外备注里提出了明确诉求，例如看日落、不想早起、少辣、拍照等，你要优先把这些诉求落实到具体某一天的主要景点或当天安排里，而不是只写成泛泛的提示。"
        "如果用户明确提到想看日落，请优先把适合看日落的地点安排为某一天的主要景点，或至少让当天主景点与日落安排保持强关联。"
        + memory_text
    )

    human_prompt = f"""
目的地：{request.destination}
出发日期：{request.start_date.isoformat()}
结束日期：{request.end_date.isoformat()}
天数：{day_count}
人数：{request.travelers}
预算：{request.budget}
偏好：{'、'.join(request.preferences) if request.preferences else '无特别偏好'}
节奏：{request.pace or '适中'}
饮食偏好：{'、'.join(request.dietary_preferences) if request.dietary_preferences else '无'}
酒店档次：{request.hotel_level or '舒适型'}
额外备注：{request.special_notes or '无'}

本地攻略上下文：
{guide_context}

可选的真实酒店名称（住宿安排必须从中选择）：
{hotel_list}

可选的真实餐厅名称（餐饮建议必须从中选择）：
{restaurant_list}

要求：
1. 输出一个整体 summary。
2. 输出 {day_count} 天的 daily draft。
3. 每天只给一个主要景点、一个餐饮建议和一条当天备注。
4. tips 保持简洁。
5. day_index 必须从 1 到 {day_count}。
6. 如果额外备注里有”想看日落””不想早起”这类明确要求，必须在 days 中体现，不要只放到 tips。
7. 如果安排了看日落，当天的 spot_name 应尽量就是适合看日落的地点，或与 daily_note 中的日落安排保持一致，避免”主景点”和”日落地点”完全割裂。
8. 每天的安排要符合”轻松”节奏，避免过满、避免太早出发。
9. meal_name 必须从"可选的真实餐厅名称"列表中选择，不要用泛称。例如用"泸州幺妹私房菜(望花路店)"而不要用"当地私房菜"。
10. hotel_name 必须从"可选的真实酒店名称"列表中选择，严禁使用"舒适型住宿""当地酒店""XX 舒适型住宿 1"等泛称。例如用"北京中关村皇冠假日酒店"而不要用"北京 舒适型住宿 1"。
11. 除非用户明确要求中途换酒店，否则全部 {day_count} 天的 hotel_name 必须保持一致，代表整趟旅行入住同一家酒店。
12. 只返回 JSON 对象，不要返回任何额外说明，不要使用 ```json 代码块。

JSON 结构示例：
{{
  "summary": "整体概述",
  "tips": ["提示1", "提示2"],
  "days": [
    {{
      "day_index": 1,
      "theme": "当天主题",
      "spot_name": "主要景点",
      "spot_description": "景点推荐理由",
      "meal_name": "餐饮名称",
      "meal_notes": "餐饮说明",
      "hotel_name": "酒店名称",
      "daily_note": "当天备注"
    }}
  ]
}}
"""

    print("[trip_planner_agent] 准备调用大模型...")
    print(f"[trip_planner_agent] model = {LLM_MODEL}")
    print(f"[trip_planner_agent] base_url = {LLM_BASE_URL or '<DEFAULT>'}")
    print(f"[trip_planner_agent] timeout = {LLM_TIMEOUT_SECONDS}s")
    print(f"[trip_planner_agent] max_retries = {LLM_MAX_RETRIES}")

    try:
        response = llm.invoke(
            [
                ("system", system_prompt),
                ("human", human_prompt),
            ]
        )
    except Exception as exc:
        print(f"[trip_planner_agent] 大模型调用失败: {type(exc).__name__}: {exc}")
        return None, empty_usage

    token_usage = _extract_token_usage(response)
    print(f"[trip_planner_agent] 大模型调用完成。token: prompt={token_usage['prompt_tokens']}, completion={token_usage['completion_tokens']}")

    raw_text = getattr(response, "content", "")
    if isinstance(raw_text, list):
        raw_text = "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in raw_text
        )

    json_text = _extract_json_object(str(raw_text))
    if json_text is None:
        print("[trip_planner_agent] 未能从模型返回中提取 JSON。")
        print(f"[trip_planner_agent] 原始返回预览: {str(raw_text)[:300]}")
        return None, token_usage

    try:
        result = PlannerDraft.model_validate(json.loads(json_text))
    except Exception as exc:
        print(f"[trip_planner_agent] JSON 解析失败: {type(exc).__name__}: {exc}")
        print(f"[trip_planner_agent] 原始返回预览: {str(raw_text)[:300]}")
        return None, token_usage

    if len(result.days) != day_count:
        print(
            "[trip_planner_agent] 结构化结果天数不匹配，"
            f"expected={day_count}, actual={len(result.days)}"
        )
        return None, token_usage

    return result, token_usage


def _dynamic_candidate_payload(candidates: list[PlaceCandidate]) -> list[dict[str, str]]:
    """只把 Planner 做选择所需的稳定字段放入提示词。"""
    return [
        {
            "poi_id": candidate.poi_id,
            "name": candidate.name,
            "district": candidate.district or "",
            "address": candidate.address or "",
            "type": candidate.type_name or "",
        }
        for candidate in candidates
    ]
def _build_memory_text(resolved_preferences) -> str:
    """把 resolve_preferences 的结果拼成可以插入 prompt 的文字。"""
    if resolved_preferences is None:
        return ""

    constraint_lines = [
        f"- {c['type']}：{c['value']}（禁止违反）"
        for c in resolved_preferences.hard_constraints
    ]
    preference_lines = [
        f"- {key}：{item['value']}（来源：{item['source']}）"
        for key, item in resolved_preferences.effective_preferences.items()
    ]

    if not constraint_lines and not preference_lines:
        return ""

    text = "\n\n用户长期记忆：\n"
    if constraint_lines:
        text += "硬性约束（必须遵守）：\n" + "\n".join(constraint_lines) + "\n"
    if preference_lines:
        text += (
            "长期偏好（尽量参考，若与本次额外要求冲突，以本次要求为准）：\n"
            + "\n".join(preference_lines)
        )
    return text

def generate_dynamic_planner_draft(
    request: TripRequest,
    candidate_pool: CityCandidatePool,
    day_count: int,
    resolved_preferences=None,
) -> tuple[DynamicPlannerDraft | None, dict[str, int]]:
    """让模型只能通过 POI ID 从动态候选池中选择行程实体。"""
    empty_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    llm = _build_chat_llm()
    if llm is None:
        return None, empty_usage

    candidate_payload = {
        category.value: _dynamic_candidate_payload(
            candidate_pool.candidates_for(category)
        )
        for category in PlaceCandidateCategory
    }
    system_prompt = (
        "你是旅行规划助手。请根据用户需求从给定的高德 POI 候选中编排行程。"
        "景点、餐饮和住宿只能通过候选中的 poi_id 选择，禁止输出候选池外的地点，"
        "禁止自行创造或改写 poi_id。不得编造价格、开放时间、星级、评分或实时营业状态。"
        "只输出一个 JSON 对象，不要输出 Markdown 或解释。"
         + _build_memory_text(resolved_preferences)
    )
    human_prompt = f"""
目的地：{request.destination}
出发日期：{request.start_date.isoformat()}
结束日期：{request.end_date.isoformat()}
天数：{day_count}
人数：{request.travelers}
预算：{request.budget}
偏好：{'、'.join(request.preferences) if request.preferences else '常规旅行体验'}
节奏：{request.pace or '适中'}
饮食偏好：{'、'.join(request.dietary_preferences) if request.dietary_preferences else '无'}
酒店档次：{request.hotel_level or '舒适型'}
额外要求：{request.special_notes or '无'}

候选池：
{json.dumps(candidate_payload, ensure_ascii=False, separators=(',', ':'))}

要求：
1. 返回 {day_count} 天，day_index 必须从 1 连续到 {day_count}。
2. 每天选择一个 spot_poi_id 和一个 meal_poi_id，整趟行程选择一个 hotel_poi_id。
3. 所有 ID 必须逐字来自对应类别候选；不要在名称字段中返回地点。
4. 尽量避免重复景点和餐饮，并结合区域、偏好、节奏与额外要求安排。
5. 只返回以下 JSON 结构：
{{
  "summary": "整体概述",
  "tips": ["旅行提示"],
  "hotel_poi_id": "住宿候选 ID",
  "days": [
    {{
      "day_index": 1,
      "theme": "当天主题",
      "spot_poi_id": "景点候选 ID",
      "spot_start_time": "10:00",
      "spot_end_time": "12:00",
      "spot_reason": "选择理由",
      "meal_poi_id": "餐饮候选 ID",
      "meal_notes": "用餐说明",
      "daily_note": "当天说明"
    }}
  ]
}}
"""

    print("[trip_planner_agent] 准备调用动态城市 Planner...")
    try:
        response = llm.invoke(
            [
                ("system", system_prompt),
                ("human", human_prompt),
            ]
        )
    except Exception as exc:
        print(
            "[trip_planner_agent] 动态城市 Planner 调用失败: "
            f"{type(exc).__name__}: {exc}"
        )
        return None, empty_usage

    token_usage = _extract_token_usage(response)
    raw_text = getattr(response, "content", "")
    if isinstance(raw_text, list):
        raw_text = "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in raw_text
        )

    json_text = _extract_json_object(str(raw_text))
    if json_text is None:
        print("[trip_planner_agent] 未能从动态城市 Planner 返回中提取 JSON。")
        return None, token_usage

    try:
        result = DynamicPlannerDraft.model_validate(json.loads(json_text))
    except Exception as exc:
        print(
            "[trip_planner_agent] 动态城市 Planner JSON 解析失败: "
            f"{type(exc).__name__}: {exc}"
        )
        return None, token_usage

    expected_indexes = list(range(1, day_count + 1))
    actual_indexes = sorted(day.day_index for day in result.days)
    if len(result.days) != day_count or actual_indexes != expected_indexes:
        print(
            "[trip_planner_agent] 动态城市 Planner 天数或 day_index 不匹配: "
            f"expected={expected_indexes}, actual={actual_indexes}"
        )
        return None, token_usage

    return result, token_usage


def generate_day_edit_draft(
    request: TripEditRequest,
    target_day: DayPlan,
) -> tuple[DayEditDraft | None, dict[str, int]]:
    """
    使用 LLM 生成单日编辑草稿。返回 (draft, token_usage)。

    这个函数只负责产出目标那一天的编辑结果，
    最终如何合并回完整 itinerary 由 service 层处理。
    """
    empty_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    llm = _build_chat_llm()
    if llm is None:
        return None, empty_usage

    current_day_payload = {
        "day_index": target_day.day_index,
        "date": target_day.date.isoformat() if target_day.date else None,
        "theme": target_day.theme,
        "spots": [spot.model_dump(mode="json") for spot in target_day.spots],
        "meals": [meal.model_dump(mode="json") for meal in target_day.meals],
        "notes": list(target_day.notes),
    }

    current_itinerary_payload = request.current_itinerary.model_dump(mode="json")

    system_prompt = (
        "你是一名旅行行程编辑助手。"
        "请根据用户编辑指令，只重写目标那一天的核心安排。"
        "你必须只输出一个 JSON 对象，不要输出 Markdown，不要输出解释文字，不要输出代码块。"
        "编辑结果要尽量保留原 itinerary 的整体风格、预算结构和轻松程度。"
    )

    human_prompt = f"""
当前完整 itinerary：
{json.dumps(current_itinerary_payload, ensure_ascii=False, indent=2)}

需要重点编辑的目标 day：
{json.dumps(current_day_payload, ensure_ascii=False, indent=2)}

用户编辑指令：{request.user_instruction}
编辑范围：{request.edit_scope or '未指定'}
需要尽量保留的约束：{', '.join(request.preserve_constraints) if request.preserve_constraints else '无'}

要求：
1. 只输出目标那一天编辑后的结果。
2. 如果用户要求“更轻松”“不要安排太满”，请减少固定景点压力，让备注更自然。
3. 尽量延续原 itinerary 的城市、风格、餐饮语气和预算结构。
4. 不要输出额外字段。
5. 只返回 JSON 对象。

JSON 结构示例：
{{
  "theme": "编辑后的当天主题",
  "spot_name": "编辑后的主要景点",
  "spot_description": "编辑后的景点说明",
  "meal_name": "编辑后的餐饮名称",
  "meal_notes": "编辑后的餐饮说明",
  "daily_note": "编辑后的当天备注"
}}
"""

    print("[trip_planner_agent] 准备调用大模型进行单日编辑...")
    print(f"[trip_planner_agent] model = {LLM_MODEL}")
    print(f"[trip_planner_agent] base_url = {LLM_BASE_URL or '<DEFAULT>'}")

    try:
        response = llm.invoke(
            [
                ("system", system_prompt),
                ("human", human_prompt),
            ]
        )
    except Exception as exc:
        print(f"[trip_planner_agent] 单日编辑调用失败: {type(exc).__name__}: {exc}")
        return None, empty_usage

    token_usage = _extract_token_usage(response)
    print(f"[trip_planner_agent] 单日编辑调用完成。token: prompt={token_usage['prompt_tokens']}, completion={token_usage['completion_tokens']}")

    raw_text = getattr(response, "content", "")
    if isinstance(raw_text, list):
        raw_text = "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in raw_text
        )

    json_text = _extract_json_object(str(raw_text))
    if json_text is None:
        print("[trip_planner_agent] 未能从单日编辑结果中提取 JSON。")
        print(f"[trip_planner_agent] 原始返回预览: {str(raw_text)[:300]}")
        return None, token_usage

    try:
        payload = json.loads(json_text)
        if not isinstance(payload, dict):
            raise ValueError("单日编辑结果不是 JSON 对象。")
        normalized_payload = _normalize_day_edit_payload(payload)
        return DayEditDraft.model_validate(normalized_payload), token_usage
    except Exception as exc:
        print(f"[trip_planner_agent] 单日编辑 JSON 解析失败: {type(exc).__name__}: {exc}")
        print(f"[trip_planner_agent] 原始返回预览: {str(raw_text)[:300]}")
        return None, token_usage
