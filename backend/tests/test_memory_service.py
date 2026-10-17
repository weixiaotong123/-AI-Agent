from pathlib import Path
import sys


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.memory_service import (  # noqa: E402
    ExtractedPreference,
    _normalize_preference_scopes,
)
import app.services.memory_service as memory_service  # noqa: E402


def test_destination_cue_corrects_global_soft_preference() -> None:
    item = ExtractedPreference(
        category="preference",
        key="location",
        value="古城附近",
        scope="global",
    )

    normalized = _normalize_preference_scopes("去大理时我更喜欢住在古城附近。", [item])

    assert normalized[0].scope == "destination"
    assert normalized[0].destination == "大理"


def test_destination_preference_is_saved_to_mentioned_city(monkeypatch) -> None:
    item = ExtractedPreference(
        category="preference",
        key="hotel_location",
        value="古城附近",
        scope="destination",
        destination="厦门",
    )
    captured: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(
        memory_service,
        "upsert_destination_preference",
        lambda user_id, destination, key, value: captured.append(
            (user_id, destination, key, value)
        ),
    )

    memory_service.save_extracted_preferences("user-1", "大理", [item])

    assert captured == [("user-1", "厦门", "hotel_location", "古城附近")]


def test_destination_cue_does_not_localize_hard_constraint() -> None:
    item = ExtractedPreference(
        category="constraint",
        key="dietary",
        value="海鲜过敏",
        scope="global",
    )

    normalized = _normalize_preference_scopes("在大理期间我对海鲜过敏。", [item])

    assert normalized[0].scope == "global"


def test_global_preference_without_destination_cue_stays_global() -> None:
    item = ExtractedPreference(
        category="preference",
        key="hotel_level",
        value="经济型",
        scope="global",
    )

    normalized = _normalize_preference_scopes("以后旅行优先住经济型酒店。", [item])

    assert normalized[0].scope == "global"