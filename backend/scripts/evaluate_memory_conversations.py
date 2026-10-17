from __future__ import annotations

import json
import re
import uuid
from datetime import date
from pathlib import Path
import sys
from typing import Any


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.trip_planner_agent import generate_planner_draft  # noqa: E402
from app.config import LLM_MODEL, SessionLocal  # noqa: E402
from app.models.db_models import (  # noqa: E402
    DestinationPreferenceRecord,
    GlobalConstraintRecord,
    GlobalPreferenceRecord,
)
from app.models.schemas import TripRequest  # noqa: E402
from app.services.memory_service import (  # noqa: E402
    ExtractedPreference,
    extract_preferences_from_instruction,
    resolve_preferences,
    save_extracted_preferences,
)
from app.services.trip_service import _request_preference_overrides  # noqa: E402


CASES_PATH = BACKEND_DIR / "eval" / "memory_conversation_cases.json"
GUIDE_PATHS = {
    "北京": "beijing_guide.md",
    "成都": "chengdu_guide.md",
    "大理": "dali_guide.md",
    "三亚": "sanya_guide.md",
    "厦门": "xiamen_guide.md",
    "西安": "xian_guide.md",
}


def _has_terms(value: str, terms: list[str]) -> bool:
    return any(term in value for term in terms)


def _find_item(
    items: list[ExtractedPreference],
    scope: str,
    value_terms: list[str],
    destination: str | None = None,
) -> ExtractedPreference | None:
    return next(
        (
            item
            for item in items
            if item.category == "preference"
            and item.scope == scope
            and (destination is None or item.destination == destination)
            and _has_terms(item.value, value_terms)
        ),
        None,
    )


def _cleanup_user(user_id: str) -> None:
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


def _memory_values_match(
    resolved,
    scope: str,
    value_terms: list[str],
) -> bool:
    return any(
        item["source"] == scope
        and _has_terms(item["value"], value_terms)
        for item in resolved.effective_preferences.values()
    )


def _hotel_section_for_name(guide: str, hotel_name: str) -> str | None:
    parts = guide.split("## 4. 住宿区域建议", maxsplit=1)
    if len(parts) != 2:
        return None
    hotel_text = parts[1].split("## 5.", maxsplit=1)[0]
    headings = list(re.finditer(r"(?m)^###\s*(.+?)\s*$", hotel_text))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(hotel_text)
        if hotel_name in hotel_text[heading.end() : end]:
            title = heading.group(1)
            return next(
                (level for level in ("经济型", "舒适型", "高端型") if level in title),
                None,
            )
    return None


def _evaluate_chain(case: dict[str, Any]) -> dict[str, Any]:
    user_id = f"memory-chain-eval-{uuid.uuid4().hex}"
    checks: dict[str, bool] = {}
    result: dict[str, Any] = {"id": case["id"]}
    try:
        global_items = extract_preferences_from_instruction(
            case["global_instruction"]
        )
        global_item = _find_item(
            global_items,
            "global",
            case["global_value_terms"],
        )
        checks["turn1_global_extracted"] = global_item is not None
        save_extracted_preferences(user_id, case["other_destination"], global_items)

        destination_items = extract_preferences_from_instruction(
            case["destination_instruction"]
        )
        destination_item = _find_item(
            destination_items,
            "destination",
            case["destination_value_terms"],
            case["destination"],
        )
        checks["turn2_destination_extracted"] = destination_item is not None
        save_extracted_preferences(
            user_id,
            case["other_destination"],
            destination_items,
        )

        target_memory = resolve_preferences(user_id, case["destination"])
        other_memory = resolve_preferences(user_id, case["other_destination"])
        checks["turn2_global_inherited"] = _memory_values_match(
            target_memory, "global", case["global_value_terms"]
        ) and _memory_values_match(
            other_memory, "global", case["global_value_terms"]
        )
        checks["turn2_destination_isolated"] = (
            destination_item is not None
            and _memory_values_match(
                target_memory,
                "destination",
                case["destination_value_terms"],
            )
            and not _memory_values_match(
                other_memory,
                "destination",
                case["destination_value_terms"],
            )
        )

        request = TripRequest(
            user_id=user_id,
            destination=case["destination"],
            start_date=date(2026, 10, 10),
            end_date=date(2026, 10, 10),
            travelers=1,
            budget=3000,
            preferences=["城市游"],
            pace="适中",
            hotel_level=case["current_hotel_level"],
            special_notes=(
                f"本次明确覆盖长期酒店偏好：{case['current_hotel_level']}。"
            ),
        )
        overrides = _request_preference_overrides(request)
        resolved = resolve_preferences(
            user_id,
            case["destination"],
            current_instruction_overrides=overrides,
        )
        current_hotel = resolved.effective_preferences.get("hotel_level", {})
        checks["turn3_current_override_wins"] = current_hotel == {
            "value": case["current_hotel_level"],
            "source": "current",
        }

        guide_path = BACKEND_DIR / "data" / GUIDE_PATHS[case["destination"]]
        guide = guide_path.read_text(encoding="utf-8")
        draft, usage = generate_planner_draft(
            request=request,
            rag_contexts=[guide],
            day_count=1,
            resolved_preferences=resolved,
        )
        generated_hotel = (
            draft.days[0].hotel_name
            if draft is not None and draft.days
            else ""
        )
        generated_hotel_section = _hotel_section_for_name(guide, generated_hotel)
        checks["turn3_generated_result_matches_override"] = (
            generated_hotel_section == case["expected_hotel_section"]
        )
        result.update(
            {
                "passed": all(checks.values()),
                "checks": checks,
                "destination_memory_value": (
                    destination_item.value if destination_item else None
                ),
                "other_city_destination_memory_leaked": _memory_values_match(
                    other_memory,
                    "destination",
                    case["destination_value_terms"],
                ),
                "current_hotel_override": current_hotel,
                "generated_hotel": generated_hotel,
                "generated_hotel_section": generated_hotel_section,
                "planner_prompt_tokens": usage.get("prompt_tokens", 0),
                "planner_completion_tokens": usage.get("completion_tokens", 0),
            }
        )
    except Exception as exc:
        result.update(
            {
                "passed": False,
                "checks": checks,
                "error": type(exc).__name__,
            }
        )
    finally:
        _cleanup_user(user_id)
    return result


def main() -> int:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    print(f"model={LLM_MODEL}")
    print("chains=10; turns_per_chain=3; total_turns=30")
    print("turn1=extract and persist global preference")
    print("turn2=extract destination preference from another active-city context")
    print("turn3=apply current hotel-level override and generate planner draft")
    results = [_evaluate_chain(case) for case in cases]
    passed = sum(result["passed"] for result in results)
    total_prompt = sum(result.get("planner_prompt_tokens", 0) for result in results)
    total_completion = sum(
        result.get("planner_completion_tokens", 0) for result in results
    )
    print(f"chain_success_rate: {passed}/{len(results)} ({passed / len(results) * 100:.1f}%)")
    print(f"extraction_calls: {len(results) * 2}")
    print(f"planner_calls: {len(results)}")
    print(f"planner_tokens: prompt={total_prompt} completion={total_completion}")
    for result in results:
        print("  " + json.dumps(result, ensure_ascii=False))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())