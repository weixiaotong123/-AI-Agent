from __future__ import annotations

from typing import Literal

from app.models.schemas import DayPlan, Itinerary, SpotItem


OUTDOOR_KEYWORDS = (
    "洱海", "古城", "公园", "海滩", "湖", "山", "森林",
    "徒步", "露营", "骑行", "观景台", "廊道", "村", "街区",
)
INDOOR_KEYWORDS = (
    "博物馆", "美术馆", "展览", "商场", "室内", "影院", "书店", "客栈", "咖啡",
)
RAIN_WEATHER_KEYWORDS = ("雨", "阵雨", "雷阵雨", "小雨", "中雨", "大雨", "暴雨")

SpotEnvironment = Literal["outdoor", "indoor", "unknown"]


def classify_spot_environment(spot: SpotItem) -> SpotEnvironment:
    """基于关键词粗判景点的室内/户外属性，后续可替换为规则引擎或模型分类。"""
    text = f"{spot.name} {spot.description or ''}"
    if any(keyword in text for keyword in INDOOR_KEYWORDS):
        return "indoor"
    if any(keyword in text for keyword in OUTDOOR_KEYWORDS):
        return "outdoor"
    return "unknown"


def _day_has_rain(weather_day: dict | None) -> bool:
    """判断某天的天气预报里是否包含降雨关键词。"""
    if weather_day is None:
        return False
    text = f"{weather_day.get('day_weather') or ''}{weather_day.get('night_weather') or ''}"
    return any(keyword in text for keyword in RAIN_WEATHER_KEYWORDS)


def _find_weather_for_day(day: DayPlan, weather_days: list[dict]) -> dict | None:
    """按日期把行程中的某一天匹配到对应的天气预报。"""
    if day.date is None:
        return None
    day_date_text = day.date.isoformat()
    return next(
        (item for item in weather_days if item.get("date") == day_date_text),
        None,
    )


def _detect_weather_conflict(day: DayPlan, weather_days: list[dict]) -> dict | None:
    """检测某一天是否存在“天气不适合当前安排”的冲突。"""
    weather_day = _find_weather_for_day(day, weather_days)
    if not _day_has_rain(weather_day):
        return None

    outdoor_spots = [
        spot for spot in day.spots if classify_spot_environment(spot) == "outdoor"
    ]
    if not outdoor_spots:
        return None

    rain_text = f"{weather_day.get('day_weather') or ''}"
    severity = "high" if any(k in rain_text for k in ("大雨", "暴雨", "雷阵雨")) else "medium"

    return {
        "day_index": day.day_index,
        "trigger": "weather_change",
        "severity": severity,
        "reason": (
            f"预计{rain_text or '有降雨'}，"
            f"当前安排以户外景点（{outdoor_spots[0].name}）为主"
        ),
    }


def _find_invalid_pois(day: DayPlan) -> list[str]:
    """收集某一天里缺少有效地址/POI的条目名称。"""
    invalid: list[str] = []

    for spot in day.spots:
        if not spot.poi_id or not spot.address:
            invalid.append(spot.name)

    for meal in day.meals:
        if not meal.poi_id or not meal.address:
            invalid.append(meal.name)

    if day.hotel is not None:
        if not day.hotel.poi_id or not day.hotel.address:
            invalid.append(day.hotel.name)

    return invalid


def _detect_poi_conflict(day: DayPlan) -> dict | None:
    """检测某一天是否存在无法确认有效地址的 POI。"""
    invalid_items = _find_invalid_pois(day)
    if not invalid_items:
        return None

    severity = "high" if len(invalid_items) >= 2 else "medium"
    item_text = "、".join(invalid_items[:3])

    return {
        "day_index": day.day_index,
        "trigger": "poi_invalid",
        "severity": severity,
        "reason": f"第{day.day_index}天存在无法确认有效地址的地点：{item_text}",
        "affected_items": invalid_items,
    }


def detect_replan_targets(
    itinerary: Itinerary,
    weather_forecast: dict | None = None,
) -> list[dict]:
    """检测行程中可能需要重新规划的天数，返回带 severity 和 reason 的列表。"""
    weather_days = (weather_forecast or {}).get("days", [])
    targets: list[dict] = []

    for day in itinerary.days:
        weather_conflict = _detect_weather_conflict(day, weather_days)
        if weather_conflict is not None:
            targets.append(weather_conflict)

        poi_conflict = _detect_poi_conflict(day)
        if poi_conflict is not None:
            targets.append(poi_conflict)

    return targets