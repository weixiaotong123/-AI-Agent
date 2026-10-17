"""将用户目的地解析为已沉淀、可动态规划或资料不足城市。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.services.city_registry_service import (
    CityCoverageTier,
    CityKnowledgeStatus,
    lookup_city,
    normalize_city_name,
)
from app.services.amap_mcp_client import resolve_administrative_area
from app.services.map_service import AmapServiceError


logger = logging.getLogger(__name__)


class CityResolutionUnavailableError(RuntimeError):
    """地图服务不可用，暂时无法判断未登记目的地的覆盖等级。"""

    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class CityResolutionResult:
    """城市解析结果，供后续生成接口选择规划路径。"""

    requested_city: str
    city: str
    tier: CityCoverageTier
    knowledge_status: CityKnowledgeStatus
    source_type: str
    adcode: str | None = None
    administrative_level: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    resolution_reason: str | None = None


DIRECT_ADMINISTERED_MUNICIPALITIES = {"北京", "上海", "天津", "重庆"}


def resolve_city(destination: str) -> CityResolutionResult:
    """解析目的地；外部地图异常会继续抛出，由 API 层区分为服务故障。"""
    requested_city = normalize_city_name(destination)
    registry_result = lookup_city(requested_city)

    if registry_result.knowledge_status is CityKnowledgeStatus.READY:
        return CityResolutionResult(
            requested_city=requested_city,
            city=registry_result.city,
            tier=CityCoverageTier.CURATED,
            knowledge_status=CityKnowledgeStatus.READY,
            source_type="local_knowledge",
        )

    try:
        administrative_area = resolve_administrative_area(requested_city)
    except AmapServiceError as exc:
        logger.warning(
            "city resolution failed: destination=%s reason=%s message=%s",
            requested_city,
            exc.reason or "amap_error",
            str(exc),
        )
        raise CityResolutionUnavailableError(
            f"暂时无法确认目的地“{requested_city}”，请稍后重试。",
            reason=exc.reason or "amap_error",
        ) from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning(
            "city resolution failed: destination=%s reason=map_service_unavailable",
            requested_city,
        )
        raise CityResolutionUnavailableError(
            f"暂时无法确认目的地“{requested_city}”，请稍后重试。",
            reason="map_service_unavailable",
        ) from exc
    if administrative_area is None:
        return CityResolutionResult(
            requested_city=requested_city,
            city=requested_city,
            tier=CityCoverageTier.INSUFFICIENT_DATA,
            knowledge_status=CityKnowledgeStatus.UNREGISTERED,
            source_type="amap_district",
        )

    resolved_city = normalize_city_name(
        str(administrative_area.get("name") or requested_city)
    )
    administrative_level = (
        str(administrative_area.get("level") or "") or None
    )
    if (
        administrative_level == "province"
        and resolved_city not in DIRECT_ADMINISTERED_MUNICIPALITIES
    ):
        return CityResolutionResult(
            requested_city=requested_city,
            city=resolved_city,
            tier=CityCoverageTier.INSUFFICIENT_DATA,
            knowledge_status=CityKnowledgeStatus.UNREGISTERED,
            source_type="amap_district",
            adcode=str(administrative_area.get("adcode") or "") or None,
            administrative_level=administrative_level,
            latitude=administrative_area.get("latitude"),
            longitude=administrative_area.get("longitude"),
            resolution_reason="province_requires_city",
        )

    return CityResolutionResult(
        requested_city=requested_city,
        city=resolved_city,
        tier=CityCoverageTier.DYNAMIC,
        knowledge_status=CityKnowledgeStatus.UNREGISTERED,
        source_type="amap_district",
        adcode=str(administrative_area.get("adcode") or "") or None,
        administrative_level=administrative_level,
        latitude=administrative_area.get("latitude"),
        longitude=administrative_area.get("longitude"),
    )
