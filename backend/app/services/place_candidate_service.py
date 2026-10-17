"""为动态城市规划采集并校验景点、餐饮和住宿 POI 候选。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

import httpx

from app.services.city_registry_service import normalize_city_name
from app.services.amap_mcp_client import search_places
from app.services.map_service import AmapServiceError


logger = logging.getLogger(__name__)


class CandidateCollectionUnavailableError(RuntimeError):
    """地图服务不可用，无法完成动态候选采集。"""

    def __init__(
        self,
        message: str,
        reason: str | None = None,
        category: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.category = category


class PlaceCandidateCategory(StrEnum):
    """动态规划当前支持的候选类别。"""

    SPOT = "spot"
    MEAL = "meal"
    HOTEL = "hotel"


@dataclass(frozen=True)
class PlaceCandidate:
    """可进入动态 Planner 候选池的地图实体。"""

    poi_id: str
    name: str
    category: PlaceCandidateCategory
    address: str | None
    city: str | None
    district: str | None
    type_name: str | None
    latitude: float
    longitude: float
    image_url: str | None
    source_type: str = "amap_poi"


@dataclass
class CityCandidatePool:
    """指定城市的三类候选以及覆盖校验结果。"""

    city: str
    adcode: str | None
    administrative_level: str | None = None
    candidates: dict[PlaceCandidateCategory, list[PlaceCandidate]] = field(
        default_factory=dict
    )
    minimum_counts: dict[PlaceCandidateCategory, int] = field(default_factory=dict)

    def candidates_for(
        self,
        category: PlaceCandidateCategory,
    ) -> list[PlaceCandidate]:
        return self.candidates.get(category, [])

    @property
    def shortages(self) -> dict[PlaceCandidateCategory, int]:
        return {
            category: minimum - len(self.candidates_for(category))
            for category, minimum in self.minimum_counts.items()
            if len(self.candidates_for(category)) < minimum
        }

    @property
    def meets_minimum(self) -> bool:
        return not self.shortages


DEFAULT_MINIMUM_COUNTS: dict[PlaceCandidateCategory, int] = {
    PlaceCandidateCategory.SPOT: 8,
    PlaceCandidateCategory.MEAL: 12,
    PlaceCandidateCategory.HOTEL: 8,
}

_CATEGORY_SEARCHES: dict[PlaceCandidateCategory, tuple[str, str]] = {
    PlaceCandidateCategory.SPOT: ("景点", "风景名胜"),
    PlaceCandidateCategory.MEAL: ("美食", "餐饮服务"),
    PlaceCandidateCategory.HOTEL: ("酒店", "住宿服务"),
}

_CATEGORY_LABELS: dict[PlaceCandidateCategory, str] = {
    PlaceCandidateCategory.SPOT: "景点",
    PlaceCandidateCategory.MEAL: "餐饮",
    PlaceCandidateCategory.HOTEL: "住宿",
}


def _place_text(value: object) -> str:
    if value is None or isinstance(value, (list, dict)):
        return ""
    return str(value).strip()


def _valid_adcode(value: object) -> str | None:
    adcode = _place_text(value)
    return adcode if len(adcode) == 6 and adcode.isdigit() else None


def _belongs_to_administrative_area(
    place_adcode: str,
    target_adcode: str,
    administrative_level: str | None,
) -> bool:
    """按行政区层级比较 POI adcode，支持地级市和区县级目的地。"""
    level = administrative_level
    if level is None:
        if target_adcode.endswith("0000"):
            level = "province"
        elif target_adcode.endswith("00"):
            level = "city"
        else:
            level = "district"

    if level == "province":
        return place_adcode[:2] == target_adcode[:2]
    if level == "city":
        return place_adcode[:4] == target_adcode[:4]
    if level == "district":
        return place_adcode == target_adcode
    return False


def _belongs_to_city_name(place: Mapping[str, object], city: str) -> bool:
    """在 adcode 缺失时，用城市名做保守的范围校验。"""
    place_city = _place_text(place.get("cityname"))
    if not place_city:
        return True

    normalized_city = normalize_city_name(city)
    normalized_place_city = normalize_city_name(place_city)
    return (
        normalized_city == normalized_place_city
        or normalized_city in normalized_place_city
        or normalized_place_city in normalized_city
    )


def _belongs_to_target_area(
    place: Mapping[str, object],
    city: str,
    adcode: str | None,
    administrative_level: str | None,
) -> bool:
    target_adcode = _valid_adcode(adcode)
    place_adcode = _valid_adcode(place.get("adcode"))
    if target_adcode is not None and place_adcode is not None:
        return _belongs_to_administrative_area(
            place_adcode,
            target_adcode,
            administrative_level,
        )
    return _belongs_to_city_name(place, city)


def _to_candidate(
    place: Mapping[str, object],
    city: str,
    category: PlaceCandidateCategory,
    adcode: str | None = None,
    administrative_level: str | None = None,
) -> PlaceCandidate | None:
    poi_id = _place_text(place.get("poi_id"))
    name = _place_text(place.get("name"))
    latitude = place.get("latitude")
    longitude = place.get("longitude")

    if not poi_id or not name or not _belongs_to_target_area(
        place,
        city=city,
        adcode=adcode,
        administrative_level=administrative_level,
    ):
        return None
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None

    return PlaceCandidate(
        poi_id=poi_id,
        name=name,
        category=category,
        address=_place_text(place.get("address")) or None,
        city=_place_text(place.get("cityname")) or None,
        district=_place_text(place.get("adname")) or None,
        type_name=_place_text(place.get("type")) or None,
        latitude=float(latitude),
        longitude=float(longitude),
        image_url=_place_text(place.get("image_url")) or None,
    )


def _filter_candidates(
    places: list[dict[str, object]],
    city: str,
    category: PlaceCandidateCategory,
    adcode: str | None = None,
    administrative_level: str | None = None,
) -> list[PlaceCandidate]:
    candidates: list[PlaceCandidate] = []
    seen_keys: set[str] = set()

    for place in places:
        candidate = _to_candidate(
            place,
            city=city,
            category=category,
            adcode=adcode,
            administrative_level=administrative_level,
        )
        if candidate is None:
            continue

        dedupe_key = candidate.poi_id or (
            f"{candidate.name}:{candidate.longitude:.6f}:{candidate.latitude:.6f}"
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        candidates.append(candidate)

    return candidates


def collect_city_candidate_pool(
    city: str,
    adcode: str | None = None,
    administrative_level: str | None = None,
    page_size: int = 25,
    minimum_counts: Mapping[PlaceCandidateCategory, int] | None = None,
) -> CityCandidatePool:
    """采集城市三类候选，并计算是否达到动态规划最低覆盖。"""
    normalized_city = normalize_city_name(city)
    resolved_minimums = dict(minimum_counts or DEFAULT_MINIMUM_COUNTS)
    candidates: dict[PlaceCandidateCategory, list[PlaceCandidate]] = {}

    for category, (keyword, type_name) in _CATEGORY_SEARCHES.items():
        try:
            places = search_places(
                keyword=keyword,
                city=adcode or normalized_city,
                page_size=page_size,
                types=type_name,
                city_limit=True,
            )
        except AmapServiceError as exc:
            logger.warning(
                "candidate collection failed: city=%s category=%s reason=%s message=%s",
                normalized_city,
                category.value,
                exc.reason or "amap_error",
                str(exc),
            )
            raise CandidateCollectionUnavailableError(
                f"暂时无法获取“{normalized_city}”的{_CATEGORY_LABELS[category]}候选，请稍后重试。",
                reason=exc.reason or "amap_error",
                category=category.value,
            ) from exc
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning(
                "candidate collection failed: city=%s category=%s reason=map_service_unavailable",
                normalized_city,
                category.value,
            )
            raise CandidateCollectionUnavailableError(
                f"暂时无法获取“{normalized_city}”的{_CATEGORY_LABELS[category]}候选，请稍后重试。",
                reason="map_service_unavailable",
                category=category.value,
            ) from exc
        candidates[category] = _filter_candidates(
            places,
            city=normalized_city,
            category=category,
            adcode=adcode,
            administrative_level=administrative_level,
        )

    return CityCandidatePool(
        city=normalized_city,
        adcode=adcode,
        administrative_level=administrative_level,
        candidates=candidates,
        minimum_counts=resolved_minimums,
    )
