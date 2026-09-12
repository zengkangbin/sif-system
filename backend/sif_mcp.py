"""Small MCP HTTP client for the SIF streamable HTTP endpoint.

The SIF URL and secret are deliberately kept server-side. The browser never
receives either value.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx


class SifMcpError(RuntimeError):
    pass


def _with_secret(url: str, secret_key: str) -> str:
    if not secret_key:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("secret-key", secret_key)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _decode_sse(text: str) -> dict[str, Any]:
    """Decode either a JSON response or an SSE response containing JSON data."""
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        value = json.loads(stripped)
        return value if isinstance(value, dict) else {"result": value}
    except json.JSONDecodeError:
        pass

    events: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            current.append(line[5:].lstrip())
        elif not line.strip() and current:
            events.append("\n".join(current))
            current = []
    if current:
        events.append("\n".join(current))

    for event in reversed(events):
        if event == "[DONE]":
            continue
        try:
            value = json.loads(event)
            return value if isinstance(value, dict) else {"result": value}
        except json.JSONDecodeError:
            continue
    raise SifMcpError(f"无法解析 SIF MCP 返回内容: {text[:500]}")


def _unwrap_tool_result(payload: dict[str, Any]) -> Any:
    if payload.get("error"):
        error = payload["error"]
        raise SifMcpError(str(error))
    result = payload.get("result", payload)
    if isinstance(result, dict) and result.get("isError"):
        raise SifMcpError(str(result))
    if isinstance(result, dict) and result.get("structuredContent") is not None:
        return result["structuredContent"]
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        text_values = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("text")]
        for text in text_values:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
        if text_values:
            return {"text": "\n".join(text_values)}
    return result


class SifMcpClient:
    def __init__(self, base_url: str | None = None, secret_key: str | None = None, timeout: float = 90.0):
        self.base_url = base_url or os.getenv("SIF_MCP_URL", "https://mcp.sif.com/mcp")
        self.secret_key = secret_key if secret_key is not None else os.getenv("SIF_MCP_SECRET_KEY", "")
        self.timeout = timeout
        self.session_id: str | None = None
        self._request_id = 0
        self._initialized = False
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "SifMcpClient":
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _post(self, message: dict[str, Any]) -> dict[str, Any]:
        if not self._client:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        response = await self._client.post(_with_secret(self.base_url, self.secret_key), headers=headers, json=message)
        if response.status_code >= 400:
            raise SifMcpError(f"SIF MCP HTTP {response.status_code}: {response.text[:500]}")
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self.session_id = session_id
        return _decode_sse(response.text)

    async def initialize(self) -> None:
        if self._initialized:
            return
        self._request_id += 1
        await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "sif-comprehensive", "version": "0.1.0"},
                },
            }
        )
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self._initialized = True

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        await self.initialize()
        self._request_id += 1
        payload = await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return _unwrap_tool_result(payload)

    async def collect(self, asins: list[str], country: str, top_n: int = 100) -> dict[str, Any]:
        """Collect only the SIF data needed by the Listing analysis MVP.

        Calls are intentionally sequential. SIF keys are account-scoped and
        the first version should stay below normal rate limits.
        """
        profiles = await self.call_tool("market_get_asin_profile", {"asins": asins, "country": country})
        items: list[dict[str, Any]] = []
        for asin in asins:
            signals = await self.call_tool(
                "market_get_asin_keyword_signals",
                {
                    "asin": asin,
                    "country": country,
                    "time_type": "lately",
                    "time_value": "30",
                    "listingSearch": False,
                    "topN": min(max(top_n, 20), 300),
                },
            )
            footprint = await self.call_tool(
                "market_get_asin_aba_footprint",
                {"asin": asin, "country": country, "topN": 500},
            )
            items.append({"asin": asin, "signals": signals, "footprint": footprint})
        return {"profiles": profiles, "items": items}


def demo_data(asins: list[str], country: str) -> dict[str, Any]:
    """Deterministic local data so the UI can be reviewed before credentials exist."""
    profiles = {
        "list": [
            {
                "asin": asin,
                "title": "Portable Wireless Charger Stand",
                "brand": "Demo Brand",
                "price": 29.99,
                "star_rating": 4.5,
                "rating_num": 1280,
                "bought_in_past_month": 900,
                "item_highlights": "Portable, foldable, fast charging, travel friendly",
            }
            for asin in asins
        ]
    }
    base = [
        ("wireless charger", 21000, 0.24, 0.82),
        ("portable wireless charger", 7600, 0.15, 0.78),
        ("foldable charging stand", 4100, 0.11, 0.73),
        ("fast charger for travel", 2900, 0.08, 0.69),
        ("wireless charger for office", 1800, 0.05, 0.66),
        ("charger for iphone and android", 1500, 0.04, 0.61),
    ]
    return {
        "profiles": profiles,
        "items": [
            {
                "asin": asin,
                "signals": {"top_keywords": [{"keyword": k, "search_volume": v, "traffic_share": s, "natural_ratio": n} for k, v, s, n in base]},
                "footprint": {"keywords": [{"keyword": k, "search_volume": v, "rank": 1 if i < 2 else 2} for i, (k, v, _, _) in enumerate(base)]},
            }
            for asin in asins
        ],
        "country": country,
        "source": "demo",
    }

