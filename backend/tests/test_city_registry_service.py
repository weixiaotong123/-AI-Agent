from pathlib import Path
import sys

import pytest


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.city_registry_service import (  # noqa: E402
    CURATED_CITY_REGISTRY,
    CityCoverageTier,
    CityKnowledgeStatus,
    lookup_city,
    normalize_city_name,
)


def test_curated_city_registry_matches_current_guides() -> None:
    """迁移期注册表必须覆盖当前六份本地攻略。"""
    assert set(CURATED_CITY_REGISTRY) == {"北京", "成都", "大理", "三亚", "厦门", "西安"}


@pytest.mark.parametrize(
    ("raw_destination", "expected_city"),
    [
        ("北京市", "北京"),
        (" 北京 ", "北京"),
        ("西安市", "西安"),
        ("大理市", "大理"),
    ],
)
def test_lookup_city_normalizes_known_city_aliases(
    raw_destination: str,
    expected_city: str,
) -> None:
    """常见城市后缀和空白不应导致已沉淀城市漏检。"""
    result = lookup_city(raw_destination)

    assert result.city == expected_city
    assert result.tier is CityCoverageTier.CURATED
    assert result.knowledge_status is CityKnowledgeStatus.READY
    assert result.entry is not None


def test_lookup_city_preserves_unregistered_city_for_next_stage() -> None:
    """未登记城市先返回规范化名称，后续交给地图解析决定 B/C 级。"""
    result = lookup_city(" 上海市 ")

    assert result.city == "上海"
    assert result.tier is CityCoverageTier.UNREGISTERED
    assert result.knowledge_status is CityKnowledgeStatus.UNREGISTERED
    assert result.entry is None


def test_normalize_city_name_rejects_blank_input() -> None:
    """空目的地不能进入后续城市解析流程。"""
    with pytest.raises(ValueError, match="目的地不能为空"):
        normalize_city_name("   ")


def test_normalize_city_name_preserves_city_as_common_word() -> None:
    """普通文本里的“城市”不能被当作行政区后缀截断。"""
    assert normalize_city_name("不存在的旅游城市") == "不存在的旅游城市"
