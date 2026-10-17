from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from app.config import AMAP_MCP_HOST, AMAP_MCP_PORT
from app.services.map_service import (
    geocode_address as _geocode_address,
    resolve_administrative_area as _resolve_administrative_area,
    search_places as _search_places,
)


mcp = FastMCP("amap-tools", host=AMAP_MCP_HOST, port=AMAP_MCP_PORT)


@mcp.tool()
def search_places(
    keyword: str = "",
    city: str | None = None,
    page_size: int = 5,
    types: str | None = None,
    page: int = 1,
    city_limit: bool = False,
) -> str:
    """Search Gaode POIs by keyword/type, optionally restricted to a city."""
    result = _search_places(
        keyword=keyword,
        city=city,
        page_size=page_size,
        types=types,
        page=page,
        city_limit=city_limit,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def resolve_administrative_area(keyword: str) -> str:
    """Resolve a Chinese place name to its Gaode administrative area."""
    result = _resolve_administrative_area(keyword)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def geocode_address(address: str, city: str | None = None) -> str:
    """Geocode an address and return its coordinates and administrative codes."""
    result = _geocode_address(address, city=city)
    return json.dumps(result, ensure_ascii=False)


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()