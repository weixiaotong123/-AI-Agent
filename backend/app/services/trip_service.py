from __future__ import annotations

from datetime import timedelta
from math import ceil

from app.agents.trip_planner_agent import (
    DynamicPlannerDayDraft,
    DynamicPlannerDraft,
    PlannerDraft,
    PlannerReview,
    PlannerReviewIssue,
    collect_trip_context,
    generate_day_edit_draft,
    generate_dynamic_planner_draft,
    generate_planner_draft,
    review_planner_draft,
    revise_planner_draft,
)
from app.services.memory_service import (
    extract_preferences_from_instruction,
    save_extracted_preferences,
)
from app.config import ENABLE_AMAP_ENRICHMENT
from app.models.schemas import (
    BudgetBreakdown,
    BudgetReview,
    DayPlan,
    HotelItem,
    Itinerary,
    MealItem,
    SpotItem,
    TokenUsage,
    TransportItem,
    TripEditRequest,
    TripRequest,
)
from app.services.map_service import enrich_itinerary_with_map_data
from app.services.fallback_candidates import extract_fallback_candidates
from app.services.dietary_safety import dietary_conflicts
from app.services.place_candidate_service import (
    CityCandidatePool,
    PlaceCandidate,
    PlaceCandidateCategory,
)
from app.services.memory_service import (
    extract_preferences_from_instruction,
    save_extracted_preferences,
)
from app.services.memory_service import resolve_preferences
TECHNICAL_TIP_KEYWORDS = (
    "LLM",
    "RAG",
    "LangChain",
    "Chroma",
    "演示",
    "测试",
    "规则",
    "模型",
    "源码",
    "trip_service",
)


class DietaryCandidateUnavailableError(RuntimeError):
    """Raised when no meal candidate passes the user's dietary restrictions."""


def _clean_user_tips(tips: list[str], destination: str | None = None) -> list[str]:
    """过滤内部实现说明，只保留用户真正能用到的旅行建议。"""
    cleaned_tips: list[str] = []
    for tip in tips:
        normalized_tip = tip.strip()
        if not normalized_tip:
            continue
        if any(keyword in normalized_tip for keyword in TECHNICAL_TIP_KEYWORDS):
            continue
        if normalized_tip not in cleaned_tips:
            cleaned_tips.append(normalized_tip)

    if cleaned_tips:
        return cleaned_tips

    place_text = destination or "目的地"
    return [
        f"建议根据{place_text}当天实时天气准备雨具或薄外套，早晚和临水区域体感可能偏凉。",
        "古镇、生态廊道和石板路更适合慢慢走，鞋子尽量选择舒适防滑的款式。",
        "热门景点建议错峰出发，给拍照、用餐和交通预留更从容的缓冲时间。",
    ]


def _requests_no_fixed_spot(instruction: str) -> bool:
    """识别明确取消景点的指令，避免把“不要安排太满”误判为空行程。"""
    normalized = "".join(instruction.split())
    return any(
        phrase in normalized
        for phrase in (
            "不要安排景点",
            "不安排景点",
            "取消景点",
            "不去景点",
            "改成自由活动",
        )
    )


def _stable_bucket(text: str, modulo: int) -> int:
    """基于文本生成一个稳定桶值，用来做确定性的价格浮动。"""
    return sum(ord(char) for char in text) % modulo if modulo > 0 else 0


def _estimate_ticket_cost(spot_name: str, description: str | None = None) -> float:
    """按景点类型估算单人门票，非实时报价。"""
    text = f"{spot_name} {description or ''}"
    bucket = _stable_bucket(text, 4)

    if any(keyword in text for keyword in ("古城", "古镇", "公园", "廊道", "村", "湿地", "街区")):
        return [0.0, 20.0, 30.0, 40.0][bucket]
    if any(keyword in text for keyword in ("寺", "三塔", "博物馆", "遗址", "山庄")):
        return round(60.0 + (bucket * 18.0), 2)
    if any(keyword in text for keyword in ("索道", "缆车", "游船", "演出", "雪山")):
        return round(120.0 + (bucket * 28.0), 2)
    return round(35.0 + (bucket * 12.0), 2)


def _estimate_meal_cost(
    travelers: int,
    preferences: list[str],
) -> float:
    """估算一餐的团体餐费；行程明细当前每天安排一餐。"""
    per_person_cost = 75.0 if "美食" in preferences else 45.0
    return round(per_person_cost * max(travelers, 1), 2)


def _estimate_hotel_nightly_cost(travelers: int, hotel_level: str | None) -> float:
    """按每间双人房的估算单价和出行人数计算每晚住宿费。"""
    level = hotel_level or "舒适型"
    if "豪华" in level:
        nightly_rate = 900.0
    elif "高档" in level or "高端" in level:
        nightly_rate = 600.0
    elif "经济" in level:
        nightly_rate = 220.0
    else:
        nightly_rate = 380.0
    room_count = ceil(max(travelers, 1) / 2)
    return round(nightly_rate * room_count, 2)


def _estimate_hotel_daily_costs(
    day_count: int,
    travelers: int,
    hotel_level: str | None,
) -> list[float]:
    """按入住晚数估算住宿费，离店日不重复收取一晚房费。"""
    nights = max(day_count - 1, 0)
    nightly_cost = _estimate_hotel_nightly_cost(travelers, hotel_level)
    return [nightly_cost if index < nights else 0.0 for index in range(day_count)]


def _estimate_transport_cost(pace: str | None) -> float:
    """估算一次市内交通段的团体费用；有路线距离时由路线估算覆盖。"""
    if pace == "紧凑":
        return 90.0
    if pace == "轻松":
        return 55.0
    return 70.0


def _estimate_draft_total_cost(
    request: TripRequest,
    days: list[tuple[str | None, str | None, str | None]],
    has_hotel: bool,
) -> float:
    """估算 Planner 草案中的已安排项目，不使用用户预算推导费用。"""
    day_count = len(days)
    tickets = sum(
        _estimate_ticket_cost(day[0], day[1]) * request.travelers
        for day in days
        if day[0]
    )
    meals = sum(
        _estimate_meal_cost(request.travelers, request.preferences)
        for day in days
        if day[2]
    )
    hotel = (
        sum(_estimate_hotel_daily_costs(day_count, request.travelers, request.hotel_level))
        if has_hotel
        else 0.0
    )
    transport = sum(
        _estimate_transport_cost(request.pace)
        for day in days
        if day[0]
    )
    subtotal = tickets + meals + hotel + transport
    return round(subtotal * 1.1, 2)


def _apply_route_based_transport_costs(itinerary: Itinerary) -> None:
    """在已有路线距离时，用路线信息修正交通花费和耗时。"""
    for day in itinerary.days:
        for transport in day.transport:
            if transport.estimated_minutes is not None:
                transport.duration = f"{transport.estimated_minutes} 分钟"

            if transport.distance_km is None:
                continue

            mode = transport.mode or ""
            if "公交" in mode:
                cost = max(2.0, 2.0 + (transport.distance_km * 0.25))
                cost *= max(itinerary.travelers or 1, 1)
            elif "步行" in mode:
                cost = 0.0
            elif "包车" in mode:
                cost = 30.0 + (transport.distance_km * 3.8)
            else:
                cost = 10.0 + (transport.distance_km * 2.2)

            transport.estimated_cost = round(cost, 2)


def _refresh_budget_breakdown(itinerary: Itinerary) -> Itinerary:
    """回算行程费用；超预算时仅压缩可选的其他支出预留款。"""
    _apply_route_based_transport_costs(itinerary)

    transport_total = round(
        sum(item.estimated_cost for day in itinerary.days for item in day.transport),
        2,
    )
    hotel_total = round(
        sum(day.hotel.estimated_cost for day in itinerary.days if day.hotel is not None),
        2,
    )
    meal_total = round(
        sum(item.estimated_cost for day in itinerary.days for item in day.meals),
        2,
    )
    ticket_total = round(
        sum(item.estimated_cost for day in itinerary.days for item in day.spots),
        2,
    )

    subtotal = transport_total + hotel_total + meal_total + ticket_total
    original_other_total = round(subtotal * 0.1, 2)
    original_total = round(subtotal + original_other_total, 2)
    other_total = original_other_total
    adjustments: list[str] = []

    if itinerary.budget_limit is not None and original_total > itinerary.budget_limit:
        other_total = round(
            min(original_other_total, max(itinerary.budget_limit - subtotal, 0)),
            2,
        )
        if other_total < original_other_total:
            adjustments.append(
                "已压缩“其他费用”预留款（原按分项小计的 10% 计），"
                f"从 ¥{original_other_total:.2f} 调整为 ¥{other_total:.2f}；"
                "未调整酒店、餐饮、景点或交通安排。"
            )

    total = round(subtotal + other_total, 2)
    itinerary.budget_breakdown = BudgetBreakdown(
        transport=transport_total,
        hotel=hotel_total,
        meals=meal_total,
        tickets=ticket_total,
        other=other_total,
        total=total,
    )
    itinerary.estimated_budget = total
    over_budget = (
        round(max(total - itinerary.budget_limit, 0), 2)
        if itinerary.budget_limit is not None
        else 0.0
    )
    if over_budget > 0:
        status = "unresolved"
        conflict_message = (
            f"保留当前酒店档次、行程安排和已知约束后，估算费用仍超预算 "
            f"¥{over_budget:.2f}。当前估算不是实时报价。"
        )
        alternatives = [
            f"将全团预算提高至少 ¥{over_budget:.2f}，即不低于约 ¥{total:.2f}。",
            "若酒店档次是可协商偏好，可选择降低档次后重新生成；系统不会擅自降档。",
            "选择缩短行程或调整非必需的付费活动后重新生成；请保留硬约束和本次明确要求。",
        ]
    elif adjustments:
        status = "reserve_reduced"
        conflict_message = None
        alternatives = []
    else:
        status = "within_budget"
        conflict_message = None
        alternatives = []
    itinerary.budget_review = BudgetReview(
        status=status,
        estimated_before_adjustment=original_total,
        estimated_after_adjustment=total,
        budget_limit=itinerary.budget_limit,
        over_budget=over_budget,
        adjustments=adjustments,
        conflict_message=conflict_message,
        alternatives=alternatives,
    )
    return itinerary


def _maybe_enrich_itinerary_with_map_data(
    itinerary: Itinerary,
    city: str | None = None,
) -> Itinerary:
    """按开关补充地图信息，并在最后统一刷新预算。"""
    if ENABLE_AMAP_ENRICHMENT:
        try:
            itinerary = enrich_itinerary_with_map_data(itinerary, city=city)
        except Exception:
            pass

    return _refresh_budget_breakdown(itinerary)


def _validated_dynamic_draft(
    draft: DynamicPlannerDraft | None,
    candidate_pool: CityCandidatePool,
    day_count: int,
    dietary_restrictions: list[str] | None = None,
) -> DynamicPlannerDraft | None:
    """拒绝包含候选池外 ID 或重复 day_index 的动态 Planner 结果。"""
    if draft is None:
        return None

    candidate_ids = {
        category: {
            candidate.poi_id
            for candidate in candidate_pool.candidates_for(category)
        }
        for category in PlaceCandidateCategory
    }
    if draft.hotel_poi_id not in candidate_ids[PlaceCandidateCategory.HOTEL]:
        return None
    if sorted(day.day_index for day in draft.days) != list(range(1, day_count + 1)):
        return None

    meal_by_id = _candidate_map(candidate_pool, PlaceCandidateCategory.MEAL)
    for day in draft.days:
        if day.spot_poi_id not in candidate_ids[PlaceCandidateCategory.SPOT]:
            return None
        if day.meal_poi_id not in candidate_ids[PlaceCandidateCategory.MEAL]:
            return None
        meal = meal_by_id[day.meal_poi_id]
        meal_text = f"{meal.name} {meal.type_name or ''} {day.meal_notes}"
        if dietary_conflicts(meal_text, dietary_restrictions or []):
            return None

    return draft


def _validated_planner_draft(
    draft: PlannerDraft | None,
    day_count: int,
    dietary_restrictions: list[str] | None = None,
) -> PlannerDraft | None:
    if draft is None:
        return None
    if sorted(day.day_index for day in draft.days) != list(range(1, day_count + 1)):
        return None
    restrictions = dietary_restrictions or []
    if any(
        dietary_conflicts(
            f"{day.meal_name} {day.meal_notes}",
            restrictions,
        )
        for day in draft.days
    ):
        return None
    return draft


def _dietary_restrictions(request: TripRequest, resolved_preferences) -> list[str]:
    restrictions = list(request.dietary_preferences)
    restrictions.extend(
        constraint["value"]
        for constraint in resolved_preferences.hard_constraints
        if constraint.get("type") == "dietary"
    )
    return list(dict.fromkeys(restrictions))


def _request_preference_overrides(request: TripRequest) -> dict[str, str]:
    overrides: dict[str, str] = {}
    if request.pace:
        overrides["travel_pace"] = request.pace
    if request.dietary_preferences:
        overrides["dietary"] = "、".join(request.dietary_preferences)
    if request.hotel_level:
        overrides["hotel_level"] = request.hotel_level
    if request.preferences:
        overrides["travel_interests"] = "、".join(request.preferences)
    return overrides


def _fallback_meal_is_safe(
    meal_name: str,
    rag_contexts: list[str],
    dietary_restrictions: list[str],
) -> bool:
    supporting_context = "\n".join(
        context for context in rag_contexts if meal_name in context
    )
    return not dietary_conflicts(
        supporting_context or meal_name,
        dietary_restrictions,
    )


def _planner_budget_review_issue(
    request: TripRequest,
    draft: PlannerDraft | DynamicPlannerDraft,
    candidate_pool: CityCandidatePool | None,
) -> PlannerReviewIssue | None:
    if isinstance(draft, DynamicPlannerDraft):
        if candidate_pool is None:
            return None
        spots_by_id = _candidate_map(candidate_pool, PlaceCandidateCategory.SPOT)
        meals_by_id = _candidate_map(candidate_pool, PlaceCandidateCategory.MEAL)
        hotels_by_id = _candidate_map(candidate_pool, PlaceCandidateCategory.HOTEL)
        draft_days = [
            (
                spots_by_id[day.spot_poi_id].name
                if day.spot_poi_id in spots_by_id
                else None,
                spots_by_id[day.spot_poi_id].type_name
                if day.spot_poi_id in spots_by_id
                else None,
                meals_by_id[day.meal_poi_id].name
                if day.meal_poi_id in meals_by_id
                else None,
            )
            for day in draft.days
        ]
        has_hotel = draft.hotel_poi_id in hotels_by_id
    else:
        draft_days = [
            (day.spot_name, day.spot_description, day.meal_name)
            for day in draft.days
        ]
        has_hotel = any(day.hotel_name for day in draft.days)

    estimated_total = _estimate_draft_total_cost(request, draft_days, has_hotel)
    if estimated_total <= request.budget + 0.01:
        return None
    return PlannerReviewIssue(
        scope="replan",
        feedback=(
            f"按当前行程条目规则估算的团体费用约为 {estimated_total:.2f} 元，"
            f"超过预算 {request.budget:.2f} 元（全体出行人总预算）；"
            "请优先选择费用较低的真实候选，"
            "不要编造或修改价格。"
        ),
    )


def _add_token_usage(
    current: dict[str, int], additional: dict[str, int]
) -> dict[str, int]:
    return {
        "prompt_tokens": current.get("prompt_tokens", 0)
        + additional.get("prompt_tokens", 0),
        "completion_tokens": current.get("completion_tokens", 0)
        + additional.get("completion_tokens", 0),
    }


def _planner_rule_review_issues(
    request: TripRequest,
    draft: PlannerDraft | DynamicPlannerDraft,
    candidate_pool: CityCandidatePool | None,
) -> list[PlannerReviewIssue]:
    issues: list[PlannerReviewIssue] = []
    if not draft.summary.strip():
        issues.append(
            PlannerReviewIssue(
                scope="replan",
                feedback="行程概述为空，请补充与目的地和天数相符的简要概述。",
            )
        )
    budget_issue = _planner_budget_review_issue(request, draft, candidate_pool)
    if budget_issue is not None:
        issues.append(budget_issue)
    if isinstance(draft, DynamicPlannerDraft):
        for day in draft.days:
            missing_fields = [
                field_name
                for field_name, value in (
                    ("theme", day.theme),
                    ("spot_reason", day.spot_reason),
                    ("meal_notes", day.meal_notes),
                    ("daily_note", day.daily_note),
                )
                if not value.strip()
            ]
            start_minutes = _clock_minutes(day.spot_start_time)
            end_minutes = _clock_minutes(day.spot_end_time)
            if missing_fields or start_minutes is None or end_minutes is None or end_minutes <= start_minutes:
                details = []
                if missing_fields:
                    details.append("空字段：" + ", ".join(missing_fields))
                if start_minutes is None or end_minutes is None or end_minutes <= start_minutes:
                    details.append("景点开始和结束时间必须是有效且递增的时间")
                issues.append(
                    PlannerReviewIssue(
                        scope="local",
                        day_index=day.day_index,
                        feedback=f"第 {day.day_index} 天信息不完整或时段无效：{'；'.join(details)}。",
                    )
                )
    else:
        for day in draft.days:
            missing_fields = [
                field_name
                for field_name, value in (
                    ("theme", day.theme),
                    ("spot_name", day.spot_name),
                    ("spot_description", day.spot_description),
                    ("meal_name", day.meal_name),
                    ("meal_notes", day.meal_notes),
                    ("hotel_name", day.hotel_name),
                    ("daily_note", day.daily_note),
                )
                if not value.strip()
            ]
            if missing_fields:
                issues.append(
                    PlannerReviewIssue(
                        scope="local",
                        day_index=day.day_index,
                        feedback=(
                            f"第 {day.day_index} 天缺少必要信息："
                            + ", ".join(missing_fields)
                            + "。请补齐。"
                        ),
                    )
                )
        explicit_hotel_change = any(
            keyword in (request.special_notes or "")
            for keyword in ("换酒店", "更换酒店", "中途换住")
        )
        hotel_names = {day.hotel_name.strip() for day in draft.days if day.hotel_name.strip()}
        if not explicit_hotel_change and len(hotel_names) > 1:
            issues.append(
                PlannerReviewIssue(
                    scope="replan",
                    feedback="用户未要求中途换酒店，但各天住宿名称不一致；请统一为同一家酒店。",
                )
            )
    return issues


def _clock_minutes(value: str) -> int | None:
    try:
        hour_text, minute_text = value.split(":", maxsplit=1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (AttributeError, ValueError):
        return None
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        return None
    return hour * 60 + minute


def _reflection_status_note(status: str | None) -> str | None:
    return {
        "passed": "初始行程已完成二次评估，未发现需要修正的问题。",
        "revised": "初始行程经二次评估后完成一次修订，并通过结构与实体校验。",
        "unavailable": "二次评估当前不可用，保留了初始行程。",
        "revision_rejected": "二次评估后的修订未通过校验，保留了初始行程。",
    }.get(status)


def _reflect_on_planner_draft(
    request: TripRequest,
    draft: PlannerDraft | DynamicPlannerDraft,
    day_count: int,
    rag_contexts: list[str],
    resolved_preferences,
    candidate_pool: CityCandidatePool | None = None,
) -> tuple[PlannerDraft | DynamicPlannerDraft, dict[str, int], str]:
    """评估一次；最多修订一次；任何异常或无效修订都保留有效初稿。"""
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    rule_issues = _planner_rule_review_issues(request, draft, candidate_pool)
    review = None
    try:
        review, review_usage = review_planner_draft(
            request=request,
            draft=draft,
            rag_contexts=rag_contexts,
            resolved_preferences=resolved_preferences,
            candidate_pool=candidate_pool,
        )
        usage = _add_token_usage(usage, review_usage)
    except Exception as exc:
        print(f"[planner_reflection] LLM 评估失败: {type(exc).__name__}: {exc}")

    if review is None:
        if not rule_issues:
            return draft, usage, "unavailable"
        review = PlannerReview(issues=rule_issues)
    else:
        review.issues.extend(rule_issues)

    if not review.issues:
        return draft, usage, "passed"

    try:
        revised_draft, revision_usage = revise_planner_draft(
            request=request,
            draft=draft,
            review=review,
            day_count=day_count,
            rag_contexts=rag_contexts,
            resolved_preferences=resolved_preferences,
            candidate_pool=candidate_pool,
        )
        usage = _add_token_usage(usage, revision_usage)
    except Exception as exc:
        print(f"[planner_reflection] 修订失败，保留初稿: {type(exc).__name__}: {exc}")
        return draft, usage, "revision_rejected"

    if isinstance(revised_draft, DynamicPlannerDraft):
        validated_draft = _validated_dynamic_draft(
            revised_draft,
            candidate_pool,
            day_count,
            _dietary_restrictions(request, resolved_preferences),
        ) if candidate_pool is not None else None
    else:
        validated_draft = _validated_planner_draft(
            revised_draft,
            day_count,
            _dietary_restrictions(request, resolved_preferences),
        )
    if validated_draft is None:
        return draft, usage, "revision_rejected"

    remaining_rule_issues = _planner_rule_review_issues(
        request, validated_draft, candidate_pool
    )
    try:
        final_review, final_review_usage = review_planner_draft(
            request=request,
            draft=validated_draft,
            rag_contexts=rag_contexts,
            resolved_preferences=resolved_preferences,
            candidate_pool=candidate_pool,
        )
        usage = _add_token_usage(usage, final_review_usage)
    except Exception as exc:
        print(f"[planner_reflection] 修订复核失败，保留初稿: {type(exc).__name__}: {exc}")
        return draft, usage, "revision_rejected"
    if final_review is None:
        return draft, usage, "revision_rejected"
    final_review.issues.extend(remaining_rule_issues)
    if final_review.issues:
        return draft, usage, "revision_rejected"
    return validated_draft, usage, "revised"


def _candidate_map(
    candidate_pool: CityCandidatePool,
    category: PlaceCandidateCategory,
) -> dict[str, PlaceCandidate]:
    return {
        candidate.poi_id: candidate
        for candidate in candidate_pool.candidates_for(category)
    }


def generate_dynamic_trip_itinerary(
    request: TripRequest,
    candidate_pool: CityCandidatePool,
) -> Itinerary:
    """使用地图候选生成动态城市行程，所有展示实体都由 POI ID 回填。"""
    day_count = max((request.end_date - request.start_date).days + 1, 1)
    resolved = resolve_preferences(
        user_id=request.user_id,
        destination=request.destination,
        current_instruction_overrides=_request_preference_overrides(request),
    )
    raw_draft, planner_usage = generate_dynamic_planner_draft(
        request=request,
        candidate_pool=candidate_pool,
        day_count=day_count,
        resolved_preferences=resolved,
    )
    dietary_restrictions = _dietary_restrictions(request, resolved)
    draft = _validated_dynamic_draft(
        raw_draft, candidate_pool, day_count, dietary_restrictions
    )
    reflection_status = None
    if draft is not None:
        draft, reflection_usage, reflection_status = _reflect_on_planner_draft(
            request=request,
            draft=draft,
            day_count=day_count,
            rag_contexts=[],
            resolved_preferences=resolved,
            candidate_pool=candidate_pool,
        )
        planner_usage = _add_token_usage(planner_usage, reflection_usage)

    spots = candidate_pool.candidates_for(PlaceCandidateCategory.SPOT)
    candidate_meals = candidate_pool.candidates_for(PlaceCandidateCategory.MEAL)
    hotels = candidate_pool.candidates_for(PlaceCandidateCategory.HOTEL)
    if not spots or not candidate_meals or not hotels:
        raise ValueError("动态城市候选池缺少景点、餐饮或住宿，无法生成行程。")
    meals = [
        candidate
        for candidate in candidate_meals
        if not dietary_conflicts(
            f"{candidate.name} {candidate.type_name or ''}",
            dietary_restrictions,
        )
    ]
    if not meals:
        if dietary_restrictions:
            raise DietaryCandidateUnavailableError(
                "当前城市没有名称或类别可确认符合饮食限制的餐饮候选。"
            )
        meals = candidate_meals

    spot_by_id = _candidate_map(candidate_pool, PlaceCandidateCategory.SPOT)
    meal_by_id = _candidate_map(candidate_pool, PlaceCandidateCategory.MEAL)
    hotel_by_id = _candidate_map(candidate_pool, PlaceCandidateCategory.HOTEL)
    selected_hotel = (
        hotel_by_id[draft.hotel_poi_id]
        if draft is not None
        else hotels[0]
    )

    selected_days: list[
        tuple[PlaceCandidate, PlaceCandidate, DynamicPlannerDayDraft | None]
    ] = []
    ticket_costs: list[float] = []
    for index in range(day_count):
        day_number = index + 1
        planner_day = (
            next((day for day in draft.days if day.day_index == day_number), None)
            if draft is not None
            else None
        )
        selected_spot = (
            spot_by_id[planner_day.spot_poi_id]
            if planner_day is not None
            else spots[index % len(spots)]
        )
        selected_meal = (
            meal_by_id[planner_day.meal_poi_id]
            if planner_day is not None
            else meals[index % len(meals)]
        )
        selected_days.append((selected_spot, selected_meal, planner_day))
        ticket_costs.append(
            _estimate_ticket_cost(selected_spot.name, selected_spot.type_name)
            * request.travelers
        )

    hotel_level = request.hotel_level or "舒适型"
    daily_hotel_costs = _estimate_hotel_daily_costs(
        day_count,
        request.travelers,
        hotel_level,
    )
    daily_meal_costs = [
        _estimate_meal_cost(request.travelers, request.preferences)
        for _ in range(day_count)
    ]
    daily_transport_costs = [
        _estimate_transport_cost(request.pace)
        for _ in range(day_count)
    ]

    days: list[DayPlan] = []
    for index, (spot, meal, planner_day) in enumerate(selected_days):
        spot_reason = (
            planner_day.spot_reason
            if planner_day is not None
            else "该地点来自当前城市的高德 POI 候选池。"
        )
        meal_notes = (
            planner_day.meal_notes
            if planner_day is not None
            else "餐厅来自当前城市的高德 POI 候选池，请以实际营业信息为准。"
        )
        daily_note = (
            planner_day.daily_note
            if planner_day is not None
            else "按真实地点候选安排，出发前请再次确认开放时间和交通情况。"
        )
        theme = (
            planner_day.theme
            if planner_day is not None
            else f"{request.destination}第 {index + 1} 天探索"
        )
        current_date = request.start_date + timedelta(days=index)
        days.append(
            DayPlan(
                day_index=index + 1,
                date=current_date,
                theme=theme,
                spots=[
                    SpotItem(
                        name=spot.name,
                        start_time=(
                            planner_day.spot_start_time
                            if planner_day is not None
                            else "10:00"
                        ),
                        end_time=(
                            planner_day.spot_end_time
                            if planner_day is not None
                            else "12:00"
                        ),
                        description=spot_reason,
                        estimated_cost=ticket_costs[index],
                        location=spot.district or spot.city or request.destination,
                        image_url=spot.image_url,
                        address=spot.address,
                        latitude=spot.latitude,
                        longitude=spot.longitude,
                        poi_id=spot.poi_id,
                    )
                ],
                meals=[
                    MealItem(
                        name=meal.name,
                        meal_type="午餐",
                        estimated_cost=daily_meal_costs[index],
                        notes=meal_notes,
                        address=meal.address,
                        latitude=meal.latitude,
                        longitude=meal.longitude,
                        poi_id=meal.poi_id,
                        image_url=meal.image_url,
                    )
                ],
                hotel=(
                    HotelItem(
                        name=selected_hotel.name,
                        level=hotel_level,
                        estimated_cost=daily_hotel_costs[index],
                        location=(
                            selected_hotel.district
                            or selected_hotel.city
                            or request.destination
                        ),
                        address=selected_hotel.address,
                        latitude=selected_hotel.latitude,
                        longitude=selected_hotel.longitude,
                        poi_id=selected_hotel.poi_id,
                        image_url=selected_hotel.image_url,
                    )
                    if daily_hotel_costs[index] > 0
                    else None
                ),
                transport=[
                    TransportItem(
                        mode="公共交通 / 打车",
                        from_place=selected_hotel.name,
                        to_place=spot.name,
                        estimated_cost=daily_transport_costs[index],
                        duration="请以实时地图路线为准",
                    )
                ],
                notes=[
                    f"当前旅行节奏：{request.pace or '适中'}",
                    daily_note,
                    "费用按全体出行人估算；门票按人计，住宿按双人间和入住晚数计，餐饮只估算行程列出的餐次，交通按团体行程估算。",
                ],
            )
        )

    preference_text = "、".join(request.preferences) if request.preferences else "常规旅行体验"
    summary = (
        draft.summary
        if draft is not None
        else f"这是一份为{request.destination}生成的 {day_count} 日动态行程，偏好重点为：{preference_text}。"
    )
    tips = _clean_user_tips(
        draft.tips if draft is not None else [],
        request.destination,
    )
    source_notes = [
        "本行程的景点、餐饮和住宿实体均由本次高德 POI 候选池回填。",
        "预算为规划估算，地点开放、营业和可订状态请以出发前实时信息为准。",
    ]
    reflection_note = _reflection_status_note(reflection_status)
    if reflection_note:
        source_notes.append(reflection_note)
    if raw_draft is not None and draft is None:
        source_notes.append("动态 Planner 返回了候选池外数据，已自动改用真实候选规则方案。")
    elif draft is None:
        source_notes.append("动态 Planner 当前不可用，已使用真实 POI 候选生成规则方案。")
    if resolved.hard_constraints:
        constraint_text = "、".join(c["value"] for c in resolved.hard_constraints)
        source_notes.append(f"已应用长期约束：{constraint_text}")
    if resolved.effective_preferences:
        pref_text = "、".join(item["value"] for item in resolved.effective_preferences.values())
        source_notes.append(f"已应用历史偏好：{pref_text}")
    itinerary = Itinerary(
        trip_id=f"trip_{request.destination}_{request.start_date.isoformat()}",
        destination=request.destination,
        travelers=request.travelers,
        budget_limit=request.budget,
        summary=summary,
        days=days,
        estimated_budget=0.0,
        budget_breakdown=BudgetBreakdown(),
        tips=tips,
        source_notes=source_notes,
        token_usage=TokenUsage(
            planner_prompt_tokens=planner_usage.get("prompt_tokens", 0),
            planner_completion_tokens=planner_usage.get("completion_tokens", 0),
        ),
    )
    return _refresh_budget_breakdown(itinerary)


def generate_trip_itinerary(request: TripRequest) -> Itinerary:
    """生成完整 itinerary，并使用更真实的预算估算方式。"""
    day_count = (request.end_date - request.start_date).days + 1
    day_count = max(day_count, 1)

    rag_contexts, rewrite_usage, rerank_usage, embedding_usage = collect_trip_context(
        destination=request.destination,
        preferences=request.preferences,
        pace=request.pace,
        special_notes=request.special_notes,
    )
    resolved = resolve_preferences(
        user_id=request.user_id,
        destination=request.destination,
        current_instruction_overrides=_request_preference_overrides(request),
    )
    dietary_restrictions = _dietary_restrictions(request, resolved)
    llm_draft, planner_usage = generate_planner_draft(request, rag_contexts, day_count, resolved)
    llm_draft = _validated_planner_draft(
        llm_draft, day_count, dietary_restrictions
    )
    reflection_status = None
    if llm_draft is not None:
        llm_draft, reflection_usage, reflection_status = _reflect_on_planner_draft(
            request=request,
            draft=llm_draft,
            day_count=day_count,
            rag_contexts=rag_contexts,
            resolved_preferences=resolved,
        )
        planner_usage = _add_token_usage(planner_usage, reflection_usage)

    token_usage = TokenUsage(
        rewrite_prompt_tokens=rewrite_usage.get("prompt_tokens", 0),
        rewrite_completion_tokens=rewrite_usage.get("completion_tokens", 0),
        embedding_prompt_tokens=embedding_usage.get("prompt_tokens", 0),
        embedding_completion_tokens=embedding_usage.get("completion_tokens", 0),
        planner_prompt_tokens=planner_usage.get("prompt_tokens", 0),
        planner_completion_tokens=planner_usage.get("completion_tokens", 0),
        rerank_prompt_tokens=rerank_usage.get("prompt_tokens", 0),
        rerank_completion_tokens=rerank_usage.get("completion_tokens", 0),
    )
    print(
        "[token_usage] Query Rewrite: "
        f"prompt={token_usage.rewrite_prompt_tokens}, "
        f"completion={token_usage.rewrite_completion_tokens}"
    )
    print(
        "[token_usage] Rerank: "
        f"prompt={token_usage.rerank_prompt_tokens}, "
        f"completion={token_usage.rerank_completion_tokens}"
    )
    print(
        "[token_usage] Query Embedding: "
        f"prompt={token_usage.embedding_prompt_tokens}, "
        f"completion={token_usage.embedding_completion_tokens}"
    )
    print(
        "[token_usage] Planner: "
        f"prompt={token_usage.planner_prompt_tokens}, "
        f"completion={token_usage.planner_completion_tokens}"
    )
    print(
        "[token_usage] Total: "
        f"prompt={token_usage.total_prompt_tokens}, "
        f"completion={token_usage.total_completion_tokens}, "
        f"all={token_usage.total_tokens}"
    )
    fallback_candidates = extract_fallback_candidates(rag_contexts)
    fallback_spot_names = fallback_candidates["spots"]
    fallback_meal_names = [
        meal_name
        for meal_name in fallback_candidates["meals"]
        if _fallback_meal_is_safe(
            meal_name,
            rag_contexts,
            dietary_restrictions,
        )
    ]
    fallback_hotel_names = fallback_candidates["hotels"]
    fallback_hotel_name = fallback_hotel_names[0] if fallback_hotel_names else None

    raw_days: list[dict[str, object]] = []
    for index in range(day_count):
        day_number = index + 1
        current_date = request.start_date + timedelta(days=index)
        llm_day = None
        if llm_draft is not None:
            llm_day = next((item for item in llm_draft.days if item.day_index == day_number), None)

        spot_name = (
            llm_day.spot_name
            if llm_day is not None
            else fallback_spot_names[index] if index < len(fallback_spot_names) else None
        )
        theme = llm_day.theme if llm_day is not None else f"{request.destination} 第 {day_number} 天轻松游"
        spot_description = (
            llm_day.spot_description
            if llm_day is not None
            else "根据本地攻略检索到的景点信息安排。" if spot_name else None
        )
        meal_name = (
            llm_day.meal_name
            if llm_day is not None
            else fallback_meal_names[index] if index < len(fallback_meal_names) else None
        )
        meal_note = (
            llm_day.meal_notes
            if llm_day is not None
            else "来自本地攻略的餐饮条目。" if meal_name else None
        )
        daily_note = (
            llm_day.daily_note
            if llm_day is not None
            else "今天以轻松游览为主，建议根据体力和天气灵活调整停留时间。"
        )
        hotel_name = (
            llm_day.hotel_name
            if llm_day is not None
            else fallback_hotel_name
        )
        unavailable_notes: list[str] = []
        if llm_day is None and not spot_name:
            unavailable_notes.append("未从当前攻略检索到景点信息，今天未安排景点。")
        if llm_day is None and not meal_name:
            unavailable_notes.append(
                "未从当前攻略检索到餐饮信息或符合饮食限制的餐饮候选，今天未安排餐饮。"
                if dietary_restrictions
                else "未从当前攻略检索到餐饮信息，今天未安排餐饮。"
            )
        if fallback_hotel_name is None:
            unavailable_notes.append("未从当前攻略检索到住宿信息，未安排住宿。")

        ticket_cost = (
            _estimate_ticket_cost(spot_name, spot_description) * request.travelers
            if spot_name
            else 0.0
        )

        raw_days.append(
            {
                "day_index": day_number,
                "date": current_date,
                "theme": theme,
                "spot_name": spot_name,
                "spot_description": spot_description,
                "hotel_name": hotel_name,
                "meal_name": meal_name,
                "meal_note": meal_note,
                "daily_note": daily_note,
                "unavailable_notes": unavailable_notes,
                "ticket_cost": ticket_cost,
            }
        )
    hotel_level = request.hotel_level or "舒适型"
    daily_hotel_costs = _estimate_hotel_daily_costs(
        day_count,
        request.travelers,
        hotel_level,
    )
    daily_meal_costs = [
        _estimate_meal_cost(request.travelers, request.preferences)
        if raw_day["meal_name"]
        else 0.0
        for raw_day in raw_days
    ]
    daily_transport_costs = [
        _estimate_transport_cost(request.pace)
        for _ in range(day_count)
    ]

    days: list[DayPlan] = []
    for index, raw_day in enumerate(raw_days):
        spot_name = raw_day["spot_name"]
        meal_name = raw_day["meal_name"]
        daily_notes = [
            f"当前旅行节奏：{request.pace or '适中'}",
            str(raw_day["daily_note"]),
            *[str(note) for note in raw_day["unavailable_notes"]],
        ]
        day_plan = DayPlan(
            day_index=int(raw_day["day_index"]),
            date=raw_day["date"],
            theme=str(raw_day["theme"]),
            spots=(
                [
                    SpotItem(
                        name=str(spot_name),
                        start_time="10:00",
                        end_time="12:00",
                        description=str(raw_day["spot_description"]),
                        estimated_cost=float(raw_day["ticket_cost"]),
                        location=request.destination,
                    )
                ]
                if spot_name
                else []
            ),
            meals=(
                [
                    MealItem(
                        name=str(meal_name),
                        meal_type="午餐",
                        estimated_cost=daily_meal_costs[index],
                        notes=str(raw_day["meal_note"]),
                    )
                ]
                if meal_name
                else []
            ),
            hotel=(
                HotelItem(
                    name=str(raw_day["hotel_name"]),
                    level=hotel_level,
                    estimated_cost=daily_hotel_costs[index],
                    location=request.destination,
                )
                if fallback_hotel_name and daily_hotel_costs[index] > 0
                else None
            ),
            transport=(
                [
                    TransportItem(
                        mode="打车",
                        from_place=f"{request.destination} 出发点",
                        to_place=str(spot_name),
                        estimated_cost=daily_transport_costs[index],
                        duration="30 分钟",
                    )
                ]
                if spot_name
                else []
            ),
            notes=daily_notes,
        )
        day_plan.notes.append(
            "费用按全体出行人估算；门票按人计，住宿按双人间和入住晚数计，餐饮只估算行程列出的餐次，交通按团体行程估算。"
        )
        days.append(day_plan)

    preference_text = "、".join(request.preferences) if request.preferences else "常规旅行体验"
    source_notes = [
        "Itinerary is assembled by trip_service.py and can optionally use LangChain structured output.",
    ]
    source_notes.extend(rag_contexts[:2])
    applied_memory_notes: list[str] = []
    if resolved.hard_constraints:
        constraint_text = "、".join(f"{c['value']}" for c in resolved.hard_constraints)
        applied_memory_notes.append(f"已应用长期约束：{constraint_text}")
    if resolved.effective_preferences:
        pref_text = "、".join(
            f"{item['value']}" for item in resolved.effective_preferences.values()
        )
        applied_memory_notes.append(f"已应用历史偏好：{pref_text}")

    source_notes.extend(applied_memory_notes)
    reflection_note = _reflection_status_note(reflection_status)
    if reflection_note:
        source_notes.append(reflection_note)
    tips = (
        llm_draft.tips
        if llm_draft is not None and llm_draft.tips
        else [
            f"建议根据{request.destination}当天实时天气准备雨具或薄外套。",
            "古镇、生态廊道和石板路更适合慢慢走，鞋子尽量选择舒适防滑的款式。",
        ]
    )
    if any("骑行" in context for context in rag_contexts):
        tips.append("如计划骑行，请以当地实时路况和可通行区域为准。")
    tips = _clean_user_tips(tips, request.destination)

    summary = (
        llm_draft.summary
        if llm_draft is not None
        else (
            f"这是一份为 {request.destination} 生成的 {day_count} 日行程，偏好重点为：{preference_text}。"
            "未检索到的信息不会以虚构地点补充。"
        )
    )


    itinerary = Itinerary(
        trip_id=f"trip_{request.destination}_{request.start_date.isoformat()}",
        destination=request.destination,
        travelers=request.travelers,
        budget_limit=request.budget,
        summary=summary,
        days=days,
        estimated_budget=0.0,
        budget_breakdown=BudgetBreakdown(),
        tips=tips,
        source_notes=source_notes,
        token_usage=token_usage,
    )
    return _maybe_enrich_itinerary_with_map_data(
        itinerary,
        city=request.destination,
    )

def _is_locked(day_index: int, field: str | None, constraints: list[str]) -> bool:
    """判断某天或某天的某个字段是否被显式锁定，不参与本次编辑。"""
    day_key = f"day_{day_index}"
    if day_key in constraints:
        return True
    if field and f"{day_key}.{field}" in constraints:
        return True
    return False
def _parse_edit_scope(edit_scope: str | None, all_days: list[DayPlan]) -> list[DayPlan]:
    """解析 edit_scope，支持单天(day_2)、多天(day_2,day_3)和全部(all/空)。"""
    if not edit_scope or edit_scope == "all":
        return list(all_days)

    target_indexes: set[int] = set()
    for part in edit_scope.split(","):
        part = part.strip()
        if not part.startswith("day_"):
            continue
        try:
            target_indexes.add(int(part.split("_")[1]))
        except (IndexError, ValueError):
            continue

    if not target_indexes:
        return all_days[:1] if all_days else []

    matched = [day for day in all_days if day.day_index in target_indexes]
    return matched if matched else (all_days[:1] if all_days else [])
def edit_trip_itinerary(request: TripEditRequest) -> Itinerary:
    """优先使用 LLM 编辑单日行程，失败时回退到规则编辑；支持一次编辑多天。"""
    updated_itinerary = request.current_itinerary.model_copy(deep=True)

    target_days = _parse_edit_scope(request.edit_scope, updated_itinerary.days)

    llm_edit_applied = False
    total_edit_token_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    edited_day_indexes: list[int] = []

    for target_day in target_days:
        day_locked = _is_locked(target_day.day_index, None, request.preserve_constraints)
        if day_locked:
            target_day.notes.append("该天已被锁定，本次编辑未做改动。")
            continue

        day_uses_candidate_pool = bool(
            target_day.hotel is not None
            and target_day.hotel.poi_id
            and target_day.spots
            and target_day.spots[0].poi_id
            and target_day.meals
            and target_day.meals[0].poi_id
        )
        day_edit_draft, edit_token_usage = generate_day_edit_draft(request, target_day)
        total_edit_token_usage["prompt_tokens"] += edit_token_usage.get("prompt_tokens", 0)
        total_edit_token_usage["completion_tokens"] += edit_token_usage.get("completion_tokens", 0)

        if day_edit_draft is not None:
            target_day.theme = day_edit_draft.theme
            if target_day.spots:
                spot_is_grounded = (
                    day_uses_candidate_pool
                    and target_day.spots[0].poi_id is not None
                )
                if not spot_is_grounded:
                    target_day.spots[0].name = day_edit_draft.spot_name
                target_day.spots[0].description = day_edit_draft.spot_description
                target_day.spots[0].estimated_cost = _estimate_ticket_cost(
                    target_day.spots[0].name,
                    day_edit_draft.spot_description,
                )
                if not spot_is_grounded:
                    target_day.spots[0].address = None
                    target_day.spots[0].latitude = None
                    target_day.spots[0].longitude = None
                    target_day.spots[0].poi_id = None
            if target_day.meals:
                if not day_uses_candidate_pool:
                    target_day.meals[0].name = day_edit_draft.meal_name
                target_day.meals[0].notes = day_edit_draft.meal_notes

            if target_day.notes:
                target_day.notes[-1] = day_edit_draft.daily_note
            else:
                target_day.notes.append(day_edit_draft.daily_note)

            llm_edit_applied = True
            edited_day_indexes.append(target_day.day_index)
        else:
            if "轻松" in request.user_instruction:
                target_day.theme = f"{target_day.theme}（已调整为更轻松）"
                target_day.notes.append("已根据用户要求把节奏调整得更轻松。")

        if _requests_no_fixed_spot(request.user_instruction):
            removed_spot_names = {spot.name for spot in target_day.spots}
            target_day.spots = []
            target_day.transport = [
                transport
                for transport in target_day.transport
                if transport.from_place not in removed_spot_names
                and transport.to_place not in removed_spot_names
            ]
            target_day.notes.append(
                "已根据你的要求取消固定景点，保留自由活动时间。"
            )
    extracted_items = extract_preferences_from_instruction(request.user_instruction)
    if extracted_items:
        save_extracted_preferences(
            user_id=request.user_id,
            destination=updated_itinerary.destination,
            items=extracted_items,
        )
        extracted_text = "、".join(f"{item.value}" for item in extracted_items)
        updated_itinerary.source_notes.append(f"已记住新偏好：{extracted_text}")
    edited_summary = "、".join(f"第{i}天" for i in sorted(edited_day_indexes)) or "本次范围"
    updated_itinerary.source_notes.append(
        f"已根据用户编辑指令更新{edited_summary}：{request.user_instruction}"
    )
    updated_itinerary.tips = _clean_user_tips(
        updated_itinerary.tips,
        updated_itinerary.destination,
    )
    updated_itinerary.tips.append("已根据你的修改要求更新目标日期，出发前建议再确认当天交通、天气和景点开放情况。")

    updated_itinerary.token_usage = TokenUsage(
        rewrite_prompt_tokens=0,
        rewrite_completion_tokens=0,
        planner_prompt_tokens=total_edit_token_usage.get("prompt_tokens", 0),
        planner_completion_tokens=total_edit_token_usage.get("completion_tokens", 0),
    )

    return _maybe_enrich_itinerary_with_map_data(
        updated_itinerary,
        city=updated_itinerary.destination,
    )