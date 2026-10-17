from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Callable
from threading import Lock
from time import perf_counter, sleep
from typing import Any

import httpx

from app.config import (
    AMAP_MCP_ENABLED,
    AMAP_MCP_MAX_RETRIES,
    AMAP_MCP_RETRY_BACKOFF_SECONDS,
    AMAP_MCP_TIMEOUT_SECONDS,
    AMAP_MCP_URL,
)
from app.services.map_service import (
    geocode_address as _rest_geocode_address,
    resolve_administrative_area as _rest_resolve_administrative_area,
    search_places as _rest_search_places,
)


logger = logging.getLogger(__name__)
_TOOL_CALL_STATS: dict[str, dict[str, int]] = {}
_TOOL_CALL_STATS_LOCK = Lock()


class ToolResultValidationError(ValueError):
    """工具返回值结构或业务字段无效。"""


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolResultValidationError(f"invalid {field_name}")
    return value.strip()


def _validate_adcode(value: Any, field_name: str = "adcode") -> None:
    if not isinstance(value, str) or re.fullmatch(r"\d{6}", value.strip()) is None:
        raise ToolResultValidationError(f"invalid {field_name}")


def _validate_coordinates(
    result: dict[str, Any],
    *,
    required: bool,
    context: str,
) -> None:
    latitude = result.get("latitude")
    longitude = result.get("longitude")
    if latitude is None and longitude is None:
        if required:
            raise ToolResultValidationError(f"missing {context} coordinates")
        return
    if latitude is None or longitude is None:
        raise ToolResultValidationError(f"incomplete {context} coordinates")
    if (
        isinstance(latitude, bool)
        or not isinstance(latitude, (int, float))
        or isinstance(longitude, bool)
        or not isinstance(longitude, (int, float))
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
        or not math.isfinite(latitude)
        or not math.isfinite(longitude)
    ):
        raise ToolResultValidationError(f"out-of-range {context} coordinates")


def _validate_optional_administrative_fields(
    result: dict[str, Any],
    *,
    context: str,
) -> bool:
    has_admin_info = False
    for field_name in ("province", "city", "cityname", "district", "adname"):
        value = result.get(field_name)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            _required_text(value, f"{context}.{field_name}")
            has_admin_info = True
    adcode = result.get("adcode")
    if adcode is not None and not (isinstance(adcode, str) and not adcode.strip()):
        _validate_adcode(adcode, f"{context}.adcode")
        has_admin_info = True
    return has_admin_info


def _validate_tool_result(tool_name: str, result: Any) -> Any:
    """Validate decoded MCP or REST data before exposing it to callers."""
    if tool_name == "search_places":
        if not isinstance(result, list):
            raise ToolResultValidationError("search_places result must be a list")
        for index, place in enumerate(result):
            context = f"search_places[{index}]"
            if not isinstance(place, dict):
                raise ToolResultValidationError(f"{context} must be an object")
            _required_text(place.get("name"), f"{context}.name")
            poi_id = _required_text(place.get("poi_id"), f"{context}.poi_id")
            if len(poi_id) > 128 or any(char.isspace() or ord(char) < 32 for char in poi_id):
                raise ToolResultValidationError(f"invalid {context}.poi_id")
            _validate_coordinates(place, required=False, context=context)
            if not _validate_optional_administrative_fields(place, context=context):
                raise ToolResultValidationError(f"missing {context} administrative information")
        return result

    if tool_name == "geocode_address":
        if result is None:
            return None
        if not isinstance(result, dict):
            raise ToolResultValidationError("geocode_address result must be an object or null")
        _required_text(result.get("formatted_address"), "geocode_address.formatted_address")
        _validate_coordinates(result, required=True, context="geocode_address")
        if not _validate_optional_administrative_fields(
            result,
            context="geocode_address",
        ):
            raise ToolResultValidationError(
                "missing geocode_address administrative information"
            )
        return result

    if tool_name == "resolve_administrative_area":
        if result is None:
            return None
        if not isinstance(result, dict):
            raise ToolResultValidationError(
                "resolve_administrative_area result must be an object or null"
            )
        _required_text(result.get("name"), "resolve_administrative_area.name")
        _validate_adcode(
            result.get("adcode"),
            "resolve_administrative_area.adcode",
        )
        level = _required_text(
            result.get("level"),
            "resolve_administrative_area.level",
        )
        if level not in {"province", "city", "district"}:
            raise ToolResultValidationError("invalid resolve_administrative_area.level")
        _validate_coordinates(
            result,
            required=False,
            context="resolve_administrative_area",
        )
        return result

    raise ValueError(f"unknown MCP tool: {tool_name}")


def _record_tool_call(
    tool_name: str,
    duration_ms: float,
    success: bool,
    error_type: str | None,
    mcp_error_type: str | None,
    retry_count: int,
    rest_fallback: bool,
) -> None:
    with _TOOL_CALL_STATS_LOCK:
        stats = _TOOL_CALL_STATS.setdefault(
            tool_name,
            {
                "total_calls": 0,
                "successful_calls": 0,
                "failed_calls": 0,
                "rest_fallback_calls": 0,
                "total_retries": 0,
            },
        )
        stats["total_calls"] += 1
        stats["successful_calls" if success else "failed_calls"] += 1
        stats["total_retries"] += retry_count
        if rest_fallback:
            stats["rest_fallback_calls"] += 1
        success_rate = stats["successful_calls"] / stats["total_calls"]
        total_calls = stats["total_calls"]
        successful_calls = stats["successful_calls"]
        failed_calls = stats["failed_calls"]
        total_retries = stats["total_retries"]
        rest_fallback_calls = stats["rest_fallback_calls"]

    log_level = logging.WARNING if rest_fallback or not success else logging.INFO
    logger.log(
        log_level,
        (
            "AMap tool call: tool=%s duration_ms=%.2f success=%s "
            "success_rate=%.4f error_type=%s mcp_error_type=%s "
            "retry_count=%d rest_fallback=%s rest_fallback_count=%d "
            "total_calls=%d successful_calls=%d failed_calls=%d "
            "total_retries=%d"
        ),
        tool_name,
        duration_ms,
        success,
        success_rate,
        error_type or "none",
        mcp_error_type or "none",
        retry_count,
        rest_fallback,
        rest_fallback_calls,
        total_calls,
        successful_calls,
        failed_calls,
        total_retries,
    )


def _get_tool_call_metrics() -> dict[str, dict[str, int | float]]:
    """Return a thread-safe snapshot of process-local tool call metrics."""
    with _TOOL_CALL_STATS_LOCK:
        return {
            tool_name: {
                **stats,
                "success_rate": stats["successful_calls"] / stats["total_calls"],
                "rest_fallback_count": stats["rest_fallback_calls"],
            }
            for tool_name, stats in _TOOL_CALL_STATS.items()
        }


def _decode_tool_result(result: Any) -> Any:
    if result.isError:
        raise RuntimeError("MCP tool returned an error")

    structured_content = getattr(result, "structuredContent", None)
    if structured_content is not None:
        if isinstance(structured_content, dict) and set(structured_content) == {"result"}:
            structured_content = structured_content["result"]
        if isinstance(structured_content, str):
            try:
                return json.loads(structured_content)
            except json.JSONDecodeError:
                return structured_content
        return structured_content

    text = "".join(
        str(getattr(item, "text", ""))
        for item in result.content
    )
    return json.loads(text)


async def _call_mcp_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        AMAP_MCP_URL,
        timeout=AMAP_MCP_TIMEOUT_SECONDS,
        sse_read_timeout=AMAP_MCP_TIMEOUT_SECONDS,
    ) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
    try:
        return _decode_tool_result(result)
    except RuntimeError as exc:
        raise RuntimeError(f"MCP tool {tool_name} returned an error") from exc


def _invoke_mcp_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    async def _call_with_timeout() -> Any:
        return await asyncio.wait_for(
            _call_mcp_tool(tool_name, arguments),
            timeout=AMAP_MCP_TIMEOUT_SECONDS,
        )

    return asyncio.run(_call_with_timeout())


def _is_transient_mcp_error(exc: Exception) -> bool:
    if isinstance(
        exc,
        (httpx.TimeoutException, httpx.NetworkError, TimeoutError, ConnectionError),
    ):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
        408,
        425,
        429,
        500,
        502,
        503,
        504,
    }


def _call_with_rest_fallback(
    tool_name: str,
    arguments: dict[str, Any],
    rest_call: Callable[..., Any],
) -> Any:
    started_at = perf_counter()
    rest_fallback = False
    error_type: str | None = None
    mcp_error_type: str | None = None
    success = False
    retry_count = 0
    try:
        if not AMAP_MCP_ENABLED:
            result = _validate_tool_result(tool_name, rest_call(**arguments))
        else:
            while True:
                try:
                    result = _validate_tool_result(
                        tool_name,
                        _invoke_mcp_tool(tool_name, arguments),
                    )
                    break
                except Exception as exc:
                    mcp_error_type = type(exc).__name__
                    if (
                        _is_transient_mcp_error(exc)
                        and retry_count < AMAP_MCP_MAX_RETRIES
                    ):
                        retry_count += 1
                        delay = min(
                            AMAP_MCP_RETRY_BACKOFF_SECONDS * (2 ** (retry_count - 1)),
                            2.0,
                        )
                        logger.warning(
                            (
                                "Retrying transient AMap MCP failure: tool=%s "
                                "error_type=%s retry_count=%d max_retries=%d "
                                "backoff_seconds=%.2f"
                            ),
                            tool_name,
                            type(exc).__name__,
                            retry_count,
                            AMAP_MCP_MAX_RETRIES,
                            delay,
                        )
                        if delay:
                            sleep(delay)
                        continue
                    rest_fallback = True
                    try:
                        result = _validate_tool_result(
                            tool_name,
                            rest_call(**arguments),
                        )
                    except Exception as rest_exc:
                        error_type = type(rest_exc).__name__
                        raise
                    error_type = mcp_error_type
                    break
        success = True
        return result
    except Exception as exc:
        if error_type is None:
            error_type = type(exc).__name__
        raise
    finally:
        _record_tool_call(
            tool_name=tool_name,
            duration_ms=(perf_counter() - started_at) * 1000,
            success=success,
            error_type=error_type,
            mcp_error_type=mcp_error_type,
            retry_count=retry_count,
            rest_fallback=rest_fallback,
        )


def search_places(
    keyword: str = "",
    city: str | None = None,
    page_size: int = 5,
    types: str | None = None,
    page: int = 1,
    city_limit: bool = False,
) -> list[dict[str, Any]]:
    arguments = {
        "keyword": keyword,
        "city": city,
        "page_size": page_size,
        "types": types,
        "page": page,
        "city_limit": city_limit,
    }
    return _call_with_rest_fallback("search_places", arguments, _rest_search_places)


def resolve_administrative_area(keyword: str) -> dict[str, Any] | None:
    arguments = {"keyword": keyword}
    return _call_with_rest_fallback(
        "resolve_administrative_area",
        arguments,
        _rest_resolve_administrative_area,
    )


def geocode_address(
    address: str,
    city: str | None = None,
) -> dict[str, Any] | None:
    arguments = {"address": address, "city": city}
    return _call_with_rest_fallback(
        "geocode_address",
        arguments,
        _rest_geocode_address,
    )