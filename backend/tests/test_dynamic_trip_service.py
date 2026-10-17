from pathlib import Path
import sys
from dataclasses import replace

import pytest


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.services.trip_service as trip_service  # noqa: E402
from app.agents.trip_planner_agent import (  # noqa: E402
    DynamicPlannerDayDraft,
    DynamicPlannerDraft,
    PlannerReview,
    PlannerReviewIssue,
)
from app.models.schemas import TripEditRequest, TripRequest  # noqa: E402
from app.services.place_candidate_service import (  # noqa: E402
    CityCandidatePool,
    PlaceCandidate,
    PlaceCandidateCategory,
)


def build_request() -> TripRequest:
    return TripRequest(
        destination="上海",
        start_date="2026-07-19",
        end_date="2026-07-21",
        travelers=2,
        budget=3600,
        preferences=["自然风景", "美食"],
        pace="轻松",
        dietary_preferences=["少辣"],
        hotel_level="舒适型",
    )


def build_candidate(
    category: PlaceCandidateCategory,
    index: int,
) -> PlaceCandidate:
    return PlaceCandidate(
        poi_id=f"{category.value}_{index}",
        name=f"上海{category.value}地点{index}",
        category=category,
        address=f"上海市测试路{index}号",
        city="上海市",
        district="黄浦区",
        type_name=f"测试{category.value}类型",
        latitude=31.20 + index / 1000,
        longitude=121.40 + index / 1000,
        image_url=f"https://example.test/{category.value}-{index}.jpg",
    )


def build_candidate_pool() -> CityCandidatePool:
    return CityCandidatePool(
        city="上海",
        adcode="310000",
        candidates={
            category: [build_candidate(category, index) for index in range(1, 5)]
            for category in PlaceCandidateCategory
        },
        minimum_counts={category: 1 for category in PlaceCandidateCategory},
    )


def test_dynamic_itinerary_fallback_only_uses_candidate_entities(monkeypatch) -> None:
    """模型不可用时也只能从真实候选中选择，不得生成模板地点。"""
    candidate_pool = build_candidate_pool()
    monkeypatch.setattr(
        trip_service,
        "generate_dynamic_planner_draft",
        lambda **_kwargs: (None, {"prompt_tokens": 0, "completion_tokens": 0}),
    )

    itinerary = trip_service.generate_dynamic_trip_itinerary(
        build_request(),
        candidate_pool,
    )

    spot_ids = {
        candidate.poi_id
        for candidate in candidate_pool.candidates_for(PlaceCandidateCategory.SPOT)
    }
    meal_ids = {
        candidate.poi_id
        for candidate in candidate_pool.candidates_for(PlaceCandidateCategory.MEAL)
    }
    hotel_ids = {
        candidate.poi_id
        for candidate in candidate_pool.candidates_for(PlaceCandidateCategory.HOTEL)
    }
    assert len(itinerary.days) == 3
    assert all(day.spots[0].poi_id in spot_ids for day in itinerary.days)
    assert all(day.meals[0].poi_id in meal_ids for day in itinerary.days)
    assert all(
        day.hotel is not None and day.hotel.poi_id in hotel_ids
        for day in itinerary.days[:-1]
    )
    assert itinerary.days[-1].hotel is None
    assert all(day.spots[0].address for day in itinerary.days)
    assert all(day.meals[0].latitude is not None for day in itinerary.days)
    assert "推荐景点" not in itinerary.model_dump_json()
    assert any("Planner 当前不可用" in note for note in itinerary.source_notes)


def test_dynamic_budget_reduces_only_reserve_and_scales_cost_with_party(
    monkeypatch,
) -> None:
    candidate_pool = build_candidate_pool()
    monkeypatch.setattr(
        trip_service,
        "generate_dynamic_planner_draft",
        lambda **_kwargs: (None, {"prompt_tokens": 0, "completion_tokens": 0}),
    )
    monkeypatch.setattr(
        trip_service,
        "resolve_preferences",
        lambda **_kwargs: type(
            "Resolved",
            (),
            {
                "hard_constraints": [],
                "effective_preferences": {},
            },
        )(),
    )
    monkeypatch.setattr(trip_service, "ENABLE_AMAP_ENRICHMENT", False)

    request = build_request()
    low_budget = trip_service.generate_dynamic_trip_itinerary(
        request.model_copy(update={"budget": 1000}),
        candidate_pool,
    )
    high_budget = trip_service.generate_dynamic_trip_itinerary(
        request.model_copy(update={"budget": 100000}),
        candidate_pool,
    )
    larger_party = trip_service.generate_dynamic_trip_itinerary(
        request.model_copy(update={"budget": 1000, "travelers": 3}),
        candidate_pool,
    )
    high_hotel_budget = trip_service.generate_dynamic_trip_itinerary(
        request.model_copy(update={"budget": 1000, "hotel_level": "高档"}),
        candidate_pool,
    )

    assert low_budget.budget_breakdown.other == 0
    assert high_budget.budget_breakdown.other > low_budget.budget_breakdown.other
    assert low_budget.estimated_budget <= high_budget.estimated_budget
    assert low_budget.budget_limit == 1000
    assert high_budget.budget_limit == 100000
    assert low_budget.budget_review.status == "unresolved"
    assert low_budget.budget_review.alternatives
    assert high_hotel_budget.budget_review.status == "unresolved"
    assert all(
        day.hotel is None or day.hotel.level == "高档"
        for day in high_hotel_budget.days
    )
    assert larger_party.budget_breakdown.tickets > low_budget.budget_breakdown.tickets
    assert larger_party.budget_breakdown.meals > low_budget.budget_breakdown.meals
    assert larger_party.budget_breakdown.hotel > low_budget.budget_breakdown.hotel
    assert all(day.hotel is not None for day in low_budget.days[:-1])
    assert low_budget.days[-1].hotel is None
    assert low_budget.travelers == 2
    assert larger_party.travelers == 3
    assert low_budget.estimated_budget == sum(
        getattr(low_budget.budget_breakdown, category)
        for category in ("transport", "hotel", "meals", "tickets", "other")
    )


def test_dynamic_itinerary_fails_when_all_meal_candidates_conflict(monkeypatch) -> None:
    candidate_pool = build_candidate_pool()
    candidate_pool.candidates[PlaceCandidateCategory.MEAL] = [
        replace(candidate, name=f"上海海鲜餐厅{index}", type_name="海鲜餐饮")
        for index, candidate in enumerate(
            candidate_pool.candidates[PlaceCandidateCategory.MEAL],
            start=1,
        )
    ]
    monkeypatch.setattr(
        trip_service,
        "generate_dynamic_planner_draft",
        lambda **_kwargs: (None, {"prompt_tokens": 0, "completion_tokens": 0}),
    )

    with pytest.raises(trip_service.DietaryCandidateUnavailableError):
        trip_service.generate_dynamic_trip_itinerary(build_request(), candidate_pool)


def test_dynamic_itinerary_rejects_planner_ids_outside_candidate_pool(monkeypatch) -> None:
    """模型只要返回一个越界 ID，整份草稿就必须回退到候选规则方案。"""
    invalid_draft = DynamicPlannerDraft(
        summary="包含越界数据的模型草稿",
        hotel_poi_id="hotel_1",
        days=[
            DynamicPlannerDayDraft(
                day_index=index,
                theme=f"第{index}天",
                spot_poi_id="outside_spot" if index == 2 else f"spot_{index}",
                spot_reason="模型推荐理由",
                meal_poi_id=f"meal_{index}",
                meal_notes="模型餐饮说明",
                daily_note="模型日程说明",
            )
            for index in range(1, 4)
        ],
    )
    monkeypatch.setattr(
        trip_service,
        "generate_dynamic_planner_draft",
        lambda **_kwargs: (
            invalid_draft,
            {"prompt_tokens": 30, "completion_tokens": 20},
        ),
    )

    itinerary = trip_service.generate_dynamic_trip_itinerary(
        build_request(),
        build_candidate_pool(),
    )

    assert all(day.spots[0].poi_id != "outside_spot" for day in itinerary.days)
    assert itinerary.summary != invalid_draft.summary
    assert any("候选池外数据" in note for note in itinerary.source_notes)
    assert itinerary.token_usage is not None
    assert itinerary.token_usage.planner_prompt_tokens == 30


def test_dynamic_itinerary_uses_valid_planner_selections(monkeypatch) -> None:
    """合法草稿应按 ID 回填候选实体，而不是信任模型输出地点名称。"""
    valid_draft = DynamicPlannerDraft(
        summary="适合轻松游览的上海三日方案。",
        tips=["提前确认景点开放时间。"],
        hotel_poi_id="hotel_3",
        days=[
            DynamicPlannerDayDraft(
                day_index=index,
                theme=f"主题{index}",
                spot_poi_id=f"spot_{4 - index}",
                spot_reason=f"理由{index}",
                meal_poi_id=f"meal_{4 - index}",
                meal_notes=f"餐饮说明{index}",
                daily_note=f"日程说明{index}",
            )
            for index in range(1, 4)
        ],
    )
    monkeypatch.setattr(
        trip_service,
        "generate_dynamic_planner_draft",
        lambda **_kwargs: (
            valid_draft,
            {"prompt_tokens": 40, "completion_tokens": 25},
        ),
    )
    monkeypatch.setattr(
        trip_service,
        "_reflect_on_planner_draft",
        lambda **kwargs: (
            kwargs["draft"],
            {"prompt_tokens": 0, "completion_tokens": 0},
            "passed",
        ),
    )

    itinerary = trip_service.generate_dynamic_trip_itinerary(
        build_request(),
        build_candidate_pool(),
    )

    assert [day.spots[0].poi_id for day in itinerary.days] == [
        "spot_3",
        "spot_2",
        "spot_1",
    ]
    assert [day.meals[0].poi_id for day in itinerary.days] == [
        "meal_3",
        "meal_2",
        "meal_1",
    ]
    assert all(
        day.hotel is not None and day.hotel.poi_id == "hotel_3"
        for day in itinerary.days[:-1]
    )
    assert itinerary.days[-1].hotel is None
    assert itinerary.summary == valid_draft.summary
    assert not any("已自动改用" in note for note in itinerary.source_notes)


def test_dynamic_itinerary_applies_valid_reflection_revision(monkeypatch) -> None:
    initial_draft = DynamicPlannerDraft(
        summary="初始动态行程。",
        hotel_poi_id="hotel_1",
        days=[
            DynamicPlannerDayDraft(
                day_index=index,
                theme=f"主题{index}",
                spot_poi_id=f"spot_{index}",
                spot_reason=f"理由{index}",
                meal_poi_id=f"meal_{index}",
                meal_notes=f"餐饮说明{index}",
                daily_note=f"日程说明{index}",
            )
            for index in range(1, 4)
        ],
    )
    revised_draft = initial_draft.model_copy(deep=True)
    revised_draft.summary = "二次评估修订后的动态行程。"
    revised_draft.days[0].spot_poi_id = "spot_4"
    monkeypatch.setattr(
        trip_service,
        "generate_dynamic_planner_draft",
        lambda **_kwargs: (
            initial_draft,
            {"prompt_tokens": 40, "completion_tokens": 25},
        ),
    )
    review_results = iter(
        [
            PlannerReview(
                issues=[
                    PlannerReviewIssue(
                        scope="local",
                        day_index=1,
                        feedback="第一天更换为候选池中的另一个景点。",
                    )
                ]
            ),
            PlannerReview(issues=[]),
        ]
    )
    monkeypatch.setattr(
        trip_service,
        "review_planner_draft",
        lambda **_kwargs: (
            next(review_results),
            {"prompt_tokens": 10, "completion_tokens": 5},
        ),
    )
    monkeypatch.setattr(
        trip_service,
        "revise_planner_draft",
        lambda **_kwargs: (
            revised_draft,
            {"prompt_tokens": 7, "completion_tokens": 3},
        ),
    )
    monkeypatch.setattr(trip_service, "ENABLE_AMAP_ENRICHMENT", False)

    itinerary = trip_service.generate_dynamic_trip_itinerary(
        build_request(), build_candidate_pool()
    )

    assert itinerary.days[0].spots[0].poi_id == "spot_4"
    assert itinerary.summary == "二次评估修订后的动态行程。"
    assert itinerary.token_usage.planner_prompt_tokens == 67
    assert itinerary.token_usage.planner_completion_tokens == 38
    assert any("完成一次修订" in note for note in itinerary.source_notes)


def test_dynamic_reflection_uses_programmatic_budget_feedback(monkeypatch) -> None:
    initial_draft = DynamicPlannerDraft(
        summary="初始预算方案。",
        hotel_poi_id="hotel_1",
        days=[
            DynamicPlannerDayDraft(
                day_index=index,
                theme=f"主题{index}",
                spot_poi_id=f"spot_{index}",
                spot_reason=f"理由{index}",
                meal_poi_id=f"meal_{index}",
                meal_notes=f"餐饮说明{index}",
                daily_note=f"日程说明{index}",
            )
            for index in range(1, 4)
        ],
    )
    revised_draft = initial_draft.model_copy(deep=True)
    revised_draft.summary = "降低门票估算后的行程。"
    for day in revised_draft.days:
        day.spot_poi_id = "spot_4"
    review_results = iter([PlannerReview(issues=[]), PlannerReview(issues=[])])
    captured_feedback = []
    monkeypatch.setattr(
        trip_service,
        "generate_dynamic_planner_draft",
        lambda **_kwargs: (
            initial_draft,
            {"prompt_tokens": 20, "completion_tokens": 10},
        ),
    )
    monkeypatch.setattr(
        trip_service,
        "review_planner_draft",
        lambda **_kwargs: (
            next(review_results),
            {"prompt_tokens": 5, "completion_tokens": 2},
        ),
    )

    def revise_with_feedback(**kwargs):
        captured_feedback.extend(issue.feedback for issue in kwargs["review"].issues)
        return revised_draft, {"prompt_tokens": 8, "completion_tokens": 4}

    monkeypatch.setattr(trip_service, "revise_planner_draft", revise_with_feedback)
    monkeypatch.setattr(
        trip_service,
        "_estimate_ticket_cost",
        lambda name, _type: 0.0 if name.endswith("地点4") else 1000.0,
    )
    monkeypatch.setattr(trip_service, "ENABLE_AMAP_ENRICHMENT", False)

    itinerary = trip_service.generate_dynamic_trip_itinerary(
        build_request().model_copy(update={"budget": 3000}),
        build_candidate_pool(),
    )

    assert any(
        "超过预算" in feedback for feedback in captured_feedback
    ), captured_feedback
    assert all(day.spots[0].poi_id == "spot_4" for day in itinerary.days)
    assert any("完成一次修订" in note for note in itinerary.source_notes)


def test_dynamic_itinerary_rejects_reflection_revision_outside_candidate_pool(monkeypatch) -> None:
    initial_draft = DynamicPlannerDraft(
        summary="初始动态行程。",
        hotel_poi_id="hotel_1",
        days=[
            DynamicPlannerDayDraft(
                day_index=index,
                theme=f"主题{index}",
                spot_poi_id=f"spot_{index}",
                spot_reason=f"理由{index}",
                meal_poi_id=f"meal_{index}",
                meal_notes=f"餐饮说明{index}",
                daily_note=f"日程说明{index}",
            )
            for index in range(1, 4)
        ],
    )
    invalid_revision = initial_draft.model_copy(deep=True)
    invalid_revision.days[0].spot_poi_id = "outside_candidate"
    monkeypatch.setattr(
        trip_service,
        "generate_dynamic_planner_draft",
        lambda **_kwargs: (
            initial_draft,
            {"prompt_tokens": 20, "completion_tokens": 10},
        ),
    )
    monkeypatch.setattr(
        trip_service,
        "review_planner_draft",
        lambda **_kwargs: (
            PlannerReview(
                issues=[
                    PlannerReviewIssue(
                        scope="local",
                        day_index=1,
                        feedback="修复第一天安排。",
                    )
                ]
            ),
            {"prompt_tokens": 5, "completion_tokens": 2},
        ),
    )
    monkeypatch.setattr(
        trip_service,
        "revise_planner_draft",
        lambda **_kwargs: (
            invalid_revision,
            {"prompt_tokens": 8, "completion_tokens": 4},
        ),
    )
    monkeypatch.setattr(trip_service, "ENABLE_AMAP_ENRICHMENT", False)

    itinerary = trip_service.generate_dynamic_trip_itinerary(
        build_request(), build_candidate_pool()
    )

    assert itinerary.days[0].spots[0].poi_id == "spot_1"
    assert itinerary.summary == "初始动态行程。"
    assert any("未通过校验" in note for note in itinerary.source_notes)


def test_dynamic_itinerary_edit_preserves_grounded_entity_names(monkeypatch) -> None:
    """后续智能调整不得把已绑定 POI 的地点名称改成模型自由文本。"""

    class FakeDayEditDraft:
        theme = "调整后的轻松行程"
        spot_name = "模型编造景点"
        spot_description = "保留真实地点，只调整游览说明。"
        meal_name = "模型编造餐厅"
        meal_notes = "保留真实餐厅，只调整用餐说明。"
        daily_note = "下午出发，放慢节奏。"

    monkeypatch.setattr(
        trip_service,
        "generate_dynamic_planner_draft",
        lambda **_kwargs: (None, {"prompt_tokens": 0, "completion_tokens": 0}),
    )
    itinerary = trip_service.generate_dynamic_trip_itinerary(
        build_request(),
        build_candidate_pool(),
    )
    original_spot = itinerary.days[0].spots[0]
    original_meal = itinerary.days[0].meals[0]
    monkeypatch.setattr(
        trip_service,
        "generate_day_edit_draft",
        lambda _request, _target_day: (
            FakeDayEditDraft(),
            {"prompt_tokens": 20, "completion_tokens": 10},
        ),
    )
    monkeypatch.setattr(trip_service, "ENABLE_AMAP_ENRICHMENT", False)

    updated = trip_service.edit_trip_itinerary(
        TripEditRequest(
            trip_id=itinerary.trip_id,
            current_itinerary=itinerary,
            user_instruction="第一天更轻松",
            edit_scope="day_1",
        )
    )

    assert updated.days[0].spots[0].name == original_spot.name
    assert updated.days[0].spots[0].poi_id == original_spot.poi_id
    assert updated.days[0].meals[0].name == original_meal.name
    assert updated.days[0].meals[0].poi_id == original_meal.poi_id
    assert updated.days[0].spots[0].description == FakeDayEditDraft.spot_description
    assert updated.days[0].meals[0].notes == FakeDayEditDraft.meal_notes


def test_dynamic_edit_removes_spot_without_map_rebinding(monkeypatch) -> None:
    """动态行程取消景点后不得再把自由活动绑定到其他地图 POI。"""
    monkeypatch.setattr(
        trip_service,
        "generate_dynamic_planner_draft",
        lambda **_kwargs: (None, {"prompt_tokens": 0, "completion_tokens": 0}),
    )
    itinerary = trip_service.generate_dynamic_trip_itinerary(
        build_request(),
        build_candidate_pool(),
    )
    monkeypatch.setattr(
        trip_service,
        "generate_day_edit_draft",
        lambda _request, _target_day: (
            None,
            {"prompt_tokens": 0, "completion_tokens": 0},
        ),
    )
    monkeypatch.setattr(trip_service, "ENABLE_AMAP_ENRICHMENT", True)

    def assert_no_spot_to_enrich(updated_itinerary, city=None):
        assert city == "上海"
        assert updated_itinerary.days[0].spots == []
        assert updated_itinerary.days[0].transport == []
        return updated_itinerary

    monkeypatch.setattr(
        trip_service,
        "enrich_itinerary_with_map_data",
        assert_no_spot_to_enrich,
    )

    updated = trip_service.edit_trip_itinerary(
        TripEditRequest(
            trip_id=itinerary.trip_id,
            current_itinerary=itinerary,
            user_instruction="第一天不要安排景点，改成自由活动",
            edit_scope="day_1",
        )
    )

    assert updated.days[0].spots == []
    assert updated.days[0].transport == []
    assert any("取消固定景点" in note for note in updated.days[0].notes)
