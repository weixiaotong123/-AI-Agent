from pathlib import Path
import sys


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.dietary_safety import dietary_conflicts  # noqa: E402


def test_detects_fish_in_seafood_allergy_recommendation() -> None:
    assert "鱼" in dietary_conflicts("推荐酸辣鱼。", ["海鲜过敏"])


def test_ignores_explicit_service_warning() -> None:
    assert dietary_conflicts(
        "推荐云南菜。请告知服务员您对花生过敏。",
        ["花生过敏"],
    ) == []


def test_warning_does_not_hide_preceding_unsafe_recommendation() -> None:
    assert "鱼" in dietary_conflicts(
        "推荐酸辣鱼，但请注意避免海鲜。",
        ["海鲜过敏"],
    )


def test_respects_explicit_no_spice_negation() -> None:
    assert dietary_conflicts("这道菜不含辣味。", ["不吃辣"]) == []


def test_flags_spicy_dish_even_if_claimed_mild() -> None:
    assert "酸辣" in dietary_conflicts("酸辣鱼（不辣版本）。", ["不吃辣"])


def test_unknown_restriction_is_not_claimed_as_verified() -> None:
    assert dietary_conflicts("推荐当地特色菜。", ["无麸质"] ) == []