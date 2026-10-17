from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path
import sys
from typing import Any


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import LLM_MODEL, SessionLocal  # noqa: E402
from app.models.db_models import (  # noqa: E402
    DestinationPreferenceRecord,
    GlobalConstraintRecord,
    GlobalPreferenceRecord,
)
from app.models.schemas import (  # noqa: E402
    BudgetBreakdown,
    DayPlan,
    HotelItem,
    Itinerary,
    MealItem,
    SpotItem,
    TripEditRequest,
    TripRequest,
)
from app.services.memory_service import (  # noqa: E402
    extract_preferences_from_instruction,
    resolve_preferences,
    save_extracted_preferences,
)
import app.services.trip_service as trip_service  # noqa: E402
from app.services.dietary_safety import dietary_conflicts  # noqa: E402
from app.services.trip_service import (  # noqa: E402
    edit_trip_itinerary,
    generate_trip_itinerary,
)


CASES_PATH = BACKEND_DIR / "eval" / "reliability_cases.json"


def _contains_term(value: str, terms: list[str]) -> bool:
    return any(term in value for term in terms)


def _cleanup_memory(user_id: str) -> None:
    session = SessionLocal()
    try:
        for model in (
            GlobalConstraintRecord,
            GlobalPreferenceRecord,
            DestinationPreferenceRecord,
        ):
            session.query(model).filter(model.user_id == user_id).delete()
        session.commit()
    finally:
        session.close()


def _memory_case(case: dict[str, Any]) -> dict[str, Any]:
    user_id = f"reliability-eval-{uuid.uuid4().hex}"
    extracted = extract_preferences_from_instruction(case["instruction"])
    matching = [
        item
        for item in extracted
        if item.category == case["category"]
        and item.scope == case["scope"]
        and _contains_term(item.value, case["value_terms"])
    ]
    try:
        save_extracted_preferences(user_id, case["destination"], extracted)
        resolved = resolve_preferences(user_id, case["destination"])
        if case["category"] == "constraint":
            retained_values = [item["value"] for item in resolved.hard_constraints]
            retained = any(
                _contains_term(value, case["value_terms"])
                for value in retained_values
            )
        elif case["scope"] == "global":
            stored = [
                value
                for value in resolved.effective_preferences.values()
                if value["source"] == "global"
            ]
            retained = any(
                _contains_term(item["value"], case["value_terms"])
                for item in stored
            )
        else:
            stored = [
                value
                for value in resolved.effective_preferences.values()
                if value["source"] == "destination"
            ]
            retained = any(
                _contains_term(item["value"], case["value_terms"])
                for item in stored
            )
        return {
            "id": case["id"],
            "passed": bool(matching) and retained,
            "extracted": [item.model_dump() for item in extracted],
            "retained": retained,
        }
    finally:
        _cleanup_memory(user_id)


def _constraint_case(case: dict[str, Any]) -> dict[str, Any]:
    request = TripRequest(
        user_id=f"reliability-eval-{uuid.uuid4().hex}",
        destination="大理",
        start_date=date(2026, 10, 10),
        end_date=date(2026, 10, 10),
        travelers=1,
        budget=2000,
        preferences=["美食"],
        pace="适中",
        dietary_preferences=case["dietary_preferences"],
        special_notes=f"严格遵守饮食限制：{case['constraint']}。",
    )
    previous_enrichment_setting = trip_service.ENABLE_AMAP_ENRICHMENT
    trip_service.ENABLE_AMAP_ENRICHMENT = False
    try:
        itinerary = generate_trip_itinerary(request)
    finally:
        trip_service.ENABLE_AMAP_ENRICHMENT = previous_enrichment_setting
        _cleanup_memory(request.user_id)

    meals = [meal for day in itinerary.days for meal in day.meals]
    meal_text = " ".join(f"{meal.name} {meal.notes or ''}" for meal in meals)
    violations = dietary_conflicts(meal_text, case["dietary_preferences"])
    safely_withheld = not meals and any(
        "符合饮食限制" in note for note in itinerary.source_notes
    )
    return {
        "id": case["id"],
        "passed": not violations and (bool(meals) or safely_withheld),
        "meal_count": len(meals),
        "violations": violations,
        "meal_text": meal_text,
        "safely_withheld": safely_withheld,
    }


def _build_replan_fixture() -> Itinerary:
    days = []
    for day_index in range(1, 4):
        days.append(
            DayPlan(
                day_index=day_index,
                date=date(2026, 10, 9 + day_index),
                theme=f"大理第{day_index}天主题",
                spots=[
                    SpotItem(
                        name=f"大理景点{day_index}",
                        description="按原行程游览。",
                    )
                ],
                meals=[
                    MealItem(
                        name=f"大理餐饮{day_index}",
                        meal_type="午餐",
                        notes="按原计划用餐。",
                    )
                ],
                hotel=HotelItem(name="大理测试酒店"),
                notes=[f"第{day_index}天原备注"],
            )
        )
    return Itinerary(
        trip_id=f"reliability-eval-{uuid.uuid4().hex}",
        destination="大理",
        summary="三天大理测试行程。",
        days=days,
        estimated_budget=2000,
        budget_breakdown=BudgetBreakdown(
            transport=400,
            hotel=700,
            meals=500,
            tickets=300,
            other=100,
            total=2000,
        ),
    )


def _replanning_case(case: dict[str, Any]) -> dict[str, Any]:
    original = _build_replan_fixture()
    request = TripEditRequest(
        user_id=f"reliability-eval-{uuid.uuid4().hex}",
        trip_id=original.trip_id,
        current_itinerary=original,
        user_instruction=case["instruction"],
        edit_scope="day_2",
        preserve_constraints=["保留预算结构"],
    )
    import app.services.trip_service as trip_service

    previous_enrichment_setting = trip_service.ENABLE_AMAP_ENRICHMENT
    trip_service.ENABLE_AMAP_ENRICHMENT = False
    try:
        updated = edit_trip_itinerary(request)
    finally:
        trip_service.ENABLE_AMAP_ENRICHMENT = previous_enrichment_setting
        _cleanup_memory(request.user_id)

    def arrangement(day: DayPlan) -> tuple[Any, ...]:
        return (
            day.date,
            day.theme,
            tuple(
                (spot.name, spot.start_time, spot.end_time, spot.description, spot.poi_id)
                for spot in day.spots
            ),
            tuple(
                (meal.name, meal.meal_type, meal.notes, meal.poi_id)
                for meal in day.meals
            ),
            (day.hotel.name, day.hotel.poi_id) if day.hotel is not None else None,
            tuple(day.notes),
            tuple(
                (item.mode, item.from_place, item.to_place)
                for item in day.transport
            ),
        )

    target_changed = arrangement(original.days[1]) != arrangement(updated.days[1])
    other_days_preserved = all(
        arrangement(original.days[index]) == arrangement(updated.days[index])
        for index in (0, 2)
    )
    return {
        "id": case["id"],
        "passed": target_changed and other_days_preserved,
        "target_changed": target_changed,
        "other_days_preserved": other_days_preserved,
    }


def _print_results(name: str, results: list[dict[str, Any]]) -> None:
    passed = sum(result["passed"] for result in results)
    print(f"{name}: {passed}/{len(results)} ({passed / len(results) * 100:.1f}%)")
    for result in results:
        print("  " + json.dumps(result, ensure_ascii=False))


def main() -> int:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    print(f"model={LLM_MODEL}")
    print("Evaluation is a small live-model pilot; dietary checks inspect final itinerary text.")
    _print_results("memory_retention", [_memory_case(case) for case in cases["memory"]])
    _print_results(
        "hard_constraint_final_itinerary_compliance",
        [_constraint_case(case) for case in cases["constraints"]],
    )
    _print_results(
        "user_directed_replanning",
        [_replanning_case(case) for case in cases["replanning"]],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())