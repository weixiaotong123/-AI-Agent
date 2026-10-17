from pathlib import Path
import sys
import json
import logging
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.services.amap_mcp_client as amap_mcp_client  # noqa: E402


def test_decode_tool_result_unwraps_json_string() -> None:
    expected = {"name": "Shanghai", "adcode": "310000"}
    result = SimpleNamespace(
        isError=False,
        structuredContent={"result": json.dumps(expected)},
        content=[],
    )

    assert amap_mcp_client._decode_tool_result(result) == expected


def test_search_places_uses_rest_when_mcp_is_disabled(monkeypatch) -> None:
    expected = [
        {
            "name": "测试地点",
            "poi_id": "B001",
            "cityname": "上海市",
        }
    ]
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_ENABLED", False)
    monkeypatch.setattr(
        amap_mcp_client,
        "_rest_search_places",
        lambda **_kwargs: expected,
    )

    result = amap_mcp_client.search_places(keyword="景点", city="上海")

    assert result == expected


def test_tool_call_metrics_record_duration_and_success_rate(monkeypatch, caplog) -> None:
    monkeypatch.setattr(amap_mcp_client, "_TOOL_CALL_STATS", {})
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_ENABLED", False)
    monkeypatch.setattr(amap_mcp_client, "perf_counter", iter([10.0, 10.125]).__next__)

    with caplog.at_level(logging.INFO, logger=amap_mcp_client.__name__):
        result = amap_mcp_client._call_with_rest_fallback(
            "search_places",
            {},
            lambda: [{"name": "地点", "poi_id": "B001", "cityname": "上海"}],
        )

    metrics = amap_mcp_client._get_tool_call_metrics()["search_places"]
    assert result == [{"name": "地点", "poi_id": "B001", "cityname": "上海"}]
    assert metrics == {
        "total_calls": 1,
        "successful_calls": 1,
        "failed_calls": 0,
        "rest_fallback_calls": 0,
        "total_retries": 0,
        "success_rate": 1.0,
        "rest_fallback_count": 0,
    }
    assert "duration_ms=125.00" in caplog.text
    assert "error_type=none" in caplog.text
    assert "rest_fallback=False" in caplog.text


def test_resolve_area_uses_mcp_when_enabled(monkeypatch, caplog) -> None:
    expected = {
        "name": "上海市",
        "adcode": "310000",
        "level": "province",
        "latitude": 31.230416,
        "longitude": 121.473701,
    }
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(amap_mcp_client, "_TOOL_CALL_STATS", {})
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_ENABLED", True)
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_MAX_RETRIES", 0)
    monkeypatch.setattr(
        amap_mcp_client,
        "_invoke_mcp_tool",
        lambda name, arguments: captured.append((name, arguments)) or expected,
    )
    monkeypatch.setattr(
        amap_mcp_client,
        "_rest_resolve_administrative_area",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("REST fallback used")),
    )

    with caplog.at_level(logging.INFO, logger=amap_mcp_client.__name__):
        result = amap_mcp_client.resolve_administrative_area("上海")

    assert result == expected
    assert captured == [("resolve_administrative_area", {"keyword": "上海"})]
    assert "tool=resolve_administrative_area" in caplog.text
    assert "success_rate=1.0000" in caplog.text
    assert "rest_fallback=False" in caplog.text


def test_geocode_falls_back_to_rest_when_mcp_call_fails(monkeypatch, caplog) -> None:
    expected = {
        "formatted_address": "上海市黄浦区外滩",
        "adcode": "310101",
        "latitude": 31.23,
        "longitude": 121.47,
    }
    monkeypatch.setattr(amap_mcp_client, "_TOOL_CALL_STATS", {})
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_ENABLED", True)
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_MAX_RETRIES", 0)
    monkeypatch.setattr(
        amap_mcp_client,
        "_invoke_mcp_tool",
        lambda *_args: (_ for _ in ()).throw(ConnectionError("MCP unavailable")),
    )
    monkeypatch.setattr(
        amap_mcp_client,
        "_rest_geocode_address",
        lambda **_kwargs: expected,
    )

    with caplog.at_level(logging.WARNING, logger=amap_mcp_client.__name__):
        result = amap_mcp_client.geocode_address("外滩", city="上海")

    assert result == expected
    assert "success=True" in caplog.text
    assert "error_type=ConnectionError" in caplog.text
    assert "mcp_error_type=ConnectionError" in caplog.text
    assert "rest_fallback=True" in caplog.text
    assert amap_mcp_client._get_tool_call_metrics()["geocode_address"] == {
        "total_calls": 1,
        "successful_calls": 1,
        "failed_calls": 0,
        "rest_fallback_calls": 1,
        "total_retries": 0,
        "success_rate": 1.0,
        "rest_fallback_count": 1,
    }


def test_fallback_metrics_capture_mcp_error_and_rest_failure(monkeypatch, caplog) -> None:
    monkeypatch.setattr(amap_mcp_client, "_TOOL_CALL_STATS", {})
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_ENABLED", True)
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_MAX_RETRIES", 0)
    monkeypatch.setattr(
        amap_mcp_client,
        "_invoke_mcp_tool",
        lambda *_args: (_ for _ in ()).throw(ConnectionError("MCP unavailable")),
    )
    monkeypatch.setattr(
        amap_mcp_client,
        "perf_counter",
        iter([20.0, 20.25]).__next__,
    )

    with caplog.at_level(logging.WARNING, logger=amap_mcp_client.__name__):
        try:
            amap_mcp_client._call_with_rest_fallback(
                "geocode_address",
                {},
                lambda: (_ for _ in ()).throw(TimeoutError("REST unavailable")),
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("REST fallback error should propagate")

    metrics = amap_mcp_client._get_tool_call_metrics()["geocode_address"]
    assert metrics == {
        "total_calls": 1,
        "successful_calls": 0,
        "failed_calls": 1,
        "rest_fallback_calls": 1,
        "total_retries": 0,
        "success_rate": 0.0,
        "rest_fallback_count": 1,
    }
    assert "duration_ms=250.00" in caplog.text
    assert "error_type=TimeoutError" in caplog.text
    assert "mcp_error_type=ConnectionError" in caplog.text
    assert "rest_fallback=True" in caplog.text


def test_transient_mcp_errors_retry_with_bounded_backoff(monkeypatch, caplog) -> None:
    monkeypatch.setattr(amap_mcp_client, "_TOOL_CALL_STATS", {})
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_ENABLED", True)
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_MAX_RETRIES", 2)
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_RETRY_BACKOFF_SECONDS", 0.25)
    attempts = 0
    delays: list[float] = []

    def flaky_mcp_call(*_args):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError("temporary timeout")
        return [{"name": "地点", "poi_id": "B001", "cityname": "上海"}]

    monkeypatch.setattr(amap_mcp_client, "_invoke_mcp_tool", flaky_mcp_call)
    monkeypatch.setattr(amap_mcp_client, "sleep", delays.append)

    with caplog.at_level(logging.WARNING, logger=amap_mcp_client.__name__):
        result = amap_mcp_client._call_with_rest_fallback(
            "search_places",
            {},
            lambda: (_ for _ in ()).throw(AssertionError("REST fallback used")),
        )

    metrics = amap_mcp_client._get_tool_call_metrics()["search_places"]
    assert result == [{"name": "地点", "poi_id": "B001", "cityname": "上海"}]
    assert attempts == 3
    assert delays == [0.25, 0.5]
    assert metrics["total_retries"] == 2
    assert metrics["rest_fallback_count"] == 0
    assert "retry_count=2" in caplog.text


def test_retries_are_capped_before_rest_fallback(monkeypatch, caplog) -> None:
    monkeypatch.setattr(amap_mcp_client, "_TOOL_CALL_STATS", {})
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_ENABLED", True)
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_MAX_RETRIES", 2)
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_RETRY_BACKOFF_SECONDS", 0)
    attempts = 0

    def unavailable_mcp(*_args):
        nonlocal attempts
        attempts += 1
        raise ConnectionError("unavailable")

    monkeypatch.setattr(amap_mcp_client, "_invoke_mcp_tool", unavailable_mcp)

    with caplog.at_level(logging.WARNING, logger=amap_mcp_client.__name__):
        result = amap_mcp_client._call_with_rest_fallback(
            "search_places",
            {},
            lambda: [{"name": "地点", "poi_id": "B001", "cityname": "上海"}],
        )

    metrics = amap_mcp_client._get_tool_call_metrics()["search_places"]
    assert result == [{"name": "地点", "poi_id": "B001", "cityname": "上海"}]
    assert attempts == 3
    assert metrics["total_retries"] == 2
    assert metrics["rest_fallback_count"] == 1
    assert "retry_count=2" in caplog.text
    assert "rest_fallback_count=1" in caplog.text


def test_non_transient_mcp_error_skips_retries(monkeypatch) -> None:
    monkeypatch.setattr(amap_mcp_client, "_TOOL_CALL_STATS", {})
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_ENABLED", True)
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_MAX_RETRIES", 3)
    attempts = 0

    def invalid_tool_call(*_args):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("invalid tool request")

    monkeypatch.setattr(amap_mcp_client, "_invoke_mcp_tool", invalid_tool_call)

    assert amap_mcp_client._call_with_rest_fallback(
        "search_places",
        {},
        lambda: [{"name": "地点", "poi_id": "B001", "cityname": "上海"}],
    ) == [{"name": "地点", "poi_id": "B001", "cityname": "上海"}]
    assert attempts == 1
    assert amap_mcp_client._get_tool_call_metrics()["search_places"][
        "total_retries"
    ] == 0


def test_mcp_client_configures_request_and_stream_timeouts(monkeypatch) -> None:
    import mcp
    import mcp.client.streamable_http as streamable_http

    seen: dict[str, object] = {}

    @asynccontextmanager
    async def fake_streamablehttp_client(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        yield (object(), object(), None)

    class FakeClientSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, *_args):
            return SimpleNamespace(
                isError=False,
                structuredContent={"result": json.dumps({"ok": True})},
                content=[],
            )

    monkeypatch.setattr(
        streamable_http,
        "streamablehttp_client",
        fake_streamablehttp_client,
    )
    monkeypatch.setattr(mcp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_TIMEOUT_SECONDS", 0.5)

    result = asyncio.run(amap_mcp_client._call_mcp_tool("timeout_test", {}))

    assert result == {"ok": True}
    assert seen == {
        "url": amap_mcp_client.AMAP_MCP_URL,
        "timeout": 0.5,
        "sse_read_timeout": 0.5,
    }


def test_total_mcp_call_is_cancelled_at_timeout(monkeypatch) -> None:
    async def blocked_mcp_call(*_args):
        await asyncio.Event().wait()

    monkeypatch.setattr(amap_mcp_client, "_call_mcp_tool", blocked_mcp_call)
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_TIMEOUT_SECONDS", 0.01)

    try:
        amap_mcp_client._invoke_mcp_tool("timeout_test", {})
    except TimeoutError:
        pass
    else:
        raise AssertionError("MCP call should be cancelled at its timeout")


def test_http_transient_statuses_are_retryable() -> None:
    request = httpx.Request("GET", "https://example.test/mcp")

    for status_code in (408, 425, 429, 500, 502, 503, 504):
        response = httpx.Response(status_code, request=request)
        error = httpx.HTTPStatusError("temporary", request=request, response=response)
        assert amap_mcp_client._is_transient_mcp_error(error)

    response = httpx.Response(400, request=request)
    permanent_error = httpx.HTTPStatusError(
        "bad request",
        request=request,
        response=response,
    )
    assert not amap_mcp_client._is_transient_mcp_error(permanent_error)


def test_invalid_mcp_poi_result_falls_back_to_valid_rest(monkeypatch, caplog) -> None:
    invalid_mcp_result = [
        {
            "name": "地点",
            "poi_id": "",
            "cityname": "上海",
            "latitude": 31.23,
            "longitude": 121.47,
        }
    ]
    valid_rest_result = [
        {
            "name": "地点",
            "poi_id": "B001",
            "cityname": "上海",
            "latitude": 31.23,
            "longitude": 121.47,
            "adcode": "310101",
        }
    ]
    monkeypatch.setattr(amap_mcp_client, "_TOOL_CALL_STATS", {})
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_ENABLED", True)
    monkeypatch.setattr(amap_mcp_client, "_invoke_mcp_tool", lambda *_args: invalid_mcp_result)
    monkeypatch.setattr(amap_mcp_client, "_rest_search_places", lambda **_kwargs: valid_rest_result)

    with caplog.at_level(logging.WARNING, logger=amap_mcp_client.__name__):
        result = amap_mcp_client.search_places(keyword="景点", city="上海")

    assert result == valid_rest_result
    assert "mcp_error_type=ToolResultValidationError" in caplog.text
    assert "rest_fallback=True" in caplog.text


def test_poi_validation_checks_identity_coordinates_and_administrative_fields() -> None:
    valid_place = {
        "name": "地点",
        "poi_id": "B001",
        "cityname": "上海",
        "adcode": "310101",
        "latitude": 31.23,
        "longitude": 121.47,
    }
    invalid_places = [
        {**valid_place, "poi_id": "bad poi id"},
        {**valid_place, "latitude": 91},
        {**valid_place, "longitude": float("inf")},
        {**valid_place, "adcode": "31010"},
        {key: value for key, value in valid_place.items() if key not in {"cityname", "adcode"}},
    ]

    for place in invalid_places:
        try:
            amap_mcp_client._validate_tool_result("search_places", [place])
        except amap_mcp_client.ToolResultValidationError:
            continue
        raise AssertionError("invalid POI result must be rejected")


def test_invalid_rest_result_is_rejected_and_counted_as_failure(monkeypatch) -> None:
    monkeypatch.setattr(amap_mcp_client, "_TOOL_CALL_STATS", {})
    monkeypatch.setattr(amap_mcp_client, "AMAP_MCP_ENABLED", False)
    monkeypatch.setattr(
        amap_mcp_client,
        "_rest_geocode_address",
        lambda **_kwargs: {
            "formatted_address": "上海",
            "latitude": 31.23,
            "longitude": 181,
        },
    )

    try:
        amap_mcp_client.geocode_address("外滩", city="上海")
    except amap_mcp_client.ToolResultValidationError:
        pass
    else:
        raise AssertionError("out-of-range REST coordinates must be rejected")

    metrics = amap_mcp_client._get_tool_call_metrics()["geocode_address"]
    assert metrics["failed_calls"] == 1
    assert metrics["success_rate"] == 0


def test_invalid_administrative_result_is_rejected() -> None:
    invalid_results = [
        {"name": "上海市", "adcode": "31000", "level": "city"},
        {"name": "上海市", "adcode": "310000", "level": "unknown"},
        {
            "name": "上海市",
            "adcode": "310000",
            "level": "city",
            "latitude": 31.23,
        },
    ]

    for result in invalid_results:
        try:
            amap_mcp_client._validate_tool_result(
                "resolve_administrative_area",
                result,
            )
        except amap_mcp_client.ToolResultValidationError:
            continue
        raise AssertionError("invalid administrative data must be rejected")


def test_geocode_requires_in_range_coordinates() -> None:
    invalid_results = [
        {
            "formatted_address": "上海市黄浦区外滩",
            "latitude": float("nan"),
            "longitude": 121.47,
            "adcode": "310101",
        },
        {
            "formatted_address": "上海市黄浦区外滩",
            "latitude": 31.23,
            "longitude": 121.47,
        },
    ]

    for invalid_result in invalid_results:
        try:
            amap_mcp_client._validate_tool_result("geocode_address", invalid_result)
        except amap_mcp_client.ToolResultValidationError:
            continue
        raise AssertionError(
            "invalid coordinates or missing administrative data must be rejected"
        )