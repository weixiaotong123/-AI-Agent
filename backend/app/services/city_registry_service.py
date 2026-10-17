"""城市覆盖状态与目的地名称规范化。

当前仅登记已沉淀到本地攻略的城市。后续接入地图解析和动态规划后，
未登记城市会在这里被提升为 dynamic 或 insufficient_data 状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.rag.guide_catalog import GUIDE_DESTINATIONS


class CityCoverageTier(StrEnum):
    """城市在旅行规划中的当前覆盖等级。"""

    CURATED = "curated"
    DYNAMIC = "dynamic"
    INSUFFICIENT_DATA = "insufficient_data"
    UNREGISTERED = "unregistered"


class CityKnowledgeStatus(StrEnum):
    """城市知识库的当前状态。"""

    READY = "ready"
    UNREGISTERED = "unregistered"


@dataclass(frozen=True)
class CityRegistryEntry:
    """已沉淀城市的静态注册信息。"""

    city: str
    aliases: tuple[str, ...]
    guide_sources: tuple[str, ...]
    tier: CityCoverageTier = CityCoverageTier.CURATED
    knowledge_status: CityKnowledgeStatus = CityKnowledgeStatus.READY


@dataclass(frozen=True)
class CityLookupResult:
    """用户目的地在当前注册表中的查询结果。"""

    city: str
    tier: CityCoverageTier
    knowledge_status: CityKnowledgeStatus
    entry: CityRegistryEntry | None = None


def normalize_city_name(destination: str) -> str:
    """规范化城市输入，统一空白和常见的“市”后缀。"""
    normalized = "".join(destination.split())
    if not normalized:
        raise ValueError("目的地不能为空")

    if (
        normalized.endswith("市")
        and not normalized.endswith("城市")
        and len(normalized) > 1
    ):
        normalized = normalized[:-1]

    return normalized


def _build_curated_city_registry() -> dict[str, CityRegistryEntry]:
    """从现有攻略目录生成迁移期的静态城市注册表。"""
    sources_by_city: dict[str, list[str]] = {}
    for source, city in GUIDE_DESTINATIONS.items():
        sources_by_city.setdefault(city, []).append(source)

    registry: dict[str, CityRegistryEntry] = {}
    for city, sources in sources_by_city.items():
        registry[city] = CityRegistryEntry(
            city=city,
            aliases=(city, f"{city}市"),
            guide_sources=tuple(sorted(sources)),
        )
    return registry


CURATED_CITY_REGISTRY = _build_curated_city_registry()
_CITY_ALIASES = {
    alias: entry.city
    for entry in CURATED_CITY_REGISTRY.values()
    for alias in entry.aliases
}


def lookup_city(destination: str) -> CityLookupResult:
    """查询目的地是否已拥有可用的本地知识库。"""
    normalized = normalize_city_name(destination)
    canonical_city = _CITY_ALIASES.get(normalized, normalized)
    entry = CURATED_CITY_REGISTRY.get(canonical_city)

    if entry is not None:
        return CityLookupResult(
            city=entry.city,
            tier=entry.tier,
            knowledge_status=entry.knowledge_status,
            entry=entry,
        )

    return CityLookupResult(
        city=canonical_city,
        tier=CityCoverageTier.UNREGISTERED,
        knowledge_status=CityKnowledgeStatus.UNREGISTERED,
    )
