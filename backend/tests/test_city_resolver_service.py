from pathlib import Path
import sys

import pytest


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.services.city_resolver_service as city_resolver  # noqa: E402
import app.services.map_service as map_service  # noqa: E402
from app.services.city_registry_service import (  # noqa: E402
    CityCoverageTier,
    CityKnowledgeStatus,
)


def test_resolve_city_uses_local_registry_before_amap(monkeypatch) -> None:
    """已沉淀城市不应额外调用高德行政区接口。"""
    monkeypatch.setattr(
        city_resolver,
        "resolve_administrative_area",
        lambda _keyword: pytest.fail("已沉淀城市不应调用高德"),
    )

    result = city_resolver.resolve_city(" 北京市 ")

    assert result.city == "北京"
    assert result.tier is CityCoverageTier.CURATED
    assert result.knowledge_status is CityKnowledgeStatus.READY
    assert result.source_type == "local_knowledge"


def test_resolve_city_marks_amap_city_as_dynamic(monkeypatch) -> None:
    """高德能确认的未登记城市应进入动态规划等级。"""
    monkeypatch.setattr(
        city_resolver,
        "resolve_administrative_area",
        lambda keyword: {
            "name": "上海市",
            "adcode": "310000",
            "level": "province",
            "latitude": 31.230416,
            "longitude": 121.473701,
        }
        if keyword == "上海"
        else None,
    )

    result = city_resolver.resolve_city("上海市")

    assert result.requested_city == "上海"
    assert result.city == "上海"
    assert result.tier is CityCoverageTier.DYNAMIC
    assert result.knowledge_status is CityKnowledgeStatus.UNREGISTERED
    assert result.adcode == "310000"
    assert result.administrative_level == "province"
    assert result.latitude == 31.230416
    assert result.longitude == 121.473701


def test_resolve_city_rejects_non_municipality_province(monkeypatch) -> None:
    """普通省份不能误入只支持单城市的动态候选链路。"""
    monkeypatch.setattr(
        city_resolver,
        "resolve_administrative_area",
        lambda keyword: {
            "name": "青海省",
            "adcode": "630000",
            "level": "province",
            "latitude": 36.6171,
            "longitude": 101.7782,
        }
        if keyword == "青海"
        else None,
    )

    result = city_resolver.resolve_city("青海")

    assert result.city == "青海省"
    assert result.tier is CityCoverageTier.INSUFFICIENT_DATA
    assert result.administrative_level == "province"
    assert result.resolution_reason == "province_requires_city"


def test_resolve_city_marks_missing_area_as_insufficient_data(monkeypatch) -> None:
    """高德无法确认的输入应明确标记为资料不足。"""
    monkeypatch.setattr(
        city_resolver,
        "resolve_administrative_area",
        lambda _keyword: None,
    )

    result = city_resolver.resolve_city("不存在的旅游城市")

    assert result.city == "不存在的旅游城市"
    assert result.tier is CityCoverageTier.INSUFFICIENT_DATA
    assert result.knowledge_status is CityKnowledgeStatus.UNREGISTERED


def test_resolve_city_propagates_amap_failure(monkeypatch) -> None:
    """地图服务异常不能被误判成用户输入的城市不存在。"""
    monkeypatch.setattr(
        city_resolver,
        "resolve_administrative_area",
        lambda _keyword: (_ for _ in ()).throw(RuntimeError("高德服务不可用")),
    )

    with pytest.raises(
        city_resolver.CityResolutionUnavailableError,
        match="暂时无法确认目的地“上海”",
    ) as exc_info:
        city_resolver.resolve_city("上海")

    assert exc_info.value.reason == "map_service_unavailable"


def test_resolve_city_preserves_safe_amap_reason(monkeypatch) -> None:
    """城市解析应保留脱敏后的高德 infocode，便于定位 503。"""
    monkeypatch.setattr(
        city_resolver,
        "resolve_administrative_area",
        lambda _keyword: (_ for _ in ()).throw(
            map_service.AmapServiceError(
                "高德地图接口调用失败：访问已超出日访问量",
                reason="10003",
            )
        ),
    )

    with pytest.raises(
        city_resolver.CityResolutionUnavailableError,
    ) as exc_info:
        city_resolver.resolve_city("杭州")

    assert exc_info.value.reason == "10003"


def test_resolve_administrative_area_parses_matching_district(monkeypatch) -> None:
    """地图适配层应选中匹配城市并正确拆分中心坐标。"""
    captured: dict[str, object] = {}

    monkeypatch.setattr(map_service, "get_cached_json", lambda _key: None)
    monkeypatch.setattr(map_service, "set_cached_json", lambda *_args, **_kwargs: None)

    def fake_request(path: str, params: dict[str, object]) -> dict[str, object]:
        captured["path"] = path
        captured["params"] = params
        return {
            "districts": [
                {
                    "name": "上海市",
                    "adcode": "310000",
                    "citycode": "021",
                    "level": "province",
                    "center": "121.473701,31.230416",
                }
            ]
        }

    monkeypatch.setattr(map_service, "_request_amap", fake_request)

    result = map_service.resolve_administrative_area("上海")

    assert captured == {
        "path": "/config/district",
        "params": {
            "keywords": "上海",
            "subdistrict": 0,
            "extensions": "base",
        },
    }
    assert result == {
        "name": "上海市",
        "adcode": "310000",
        "citycode": "021",
        "level": "province",
        "latitude": 31.230416,
        "longitude": 121.473701,
    }


def test_resolve_administrative_area_rejects_unrelated_result(monkeypatch) -> None:
    """高德返回的模糊但无关行政区不能被当作目标城市。"""
    monkeypatch.setattr(map_service, "get_cached_json", lambda _key: None)
    monkeypatch.setattr(
        map_service,
        "_request_amap",
        lambda _path, _params: {
            "districts": [
                {
                    "name": "北京市",
                    "adcode": "110000",
                    "level": "province",
                    "center": "116.407387,39.904179",
                }
            ]
        },
    )

    assert map_service.resolve_administrative_area("不存在的旅游城市") is None
