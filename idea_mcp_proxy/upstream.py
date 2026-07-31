"""Long-lived connection to the real JetBrains IDE MCP server.

Held open for the proxy process's lifetime via the Server's lifespan hook,
rather than reconnecting per-request.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

DEFAULT_UPSTREAM_URL = "http://127.0.0.1:64342/stream"


@dataclass
class UpstreamConnection:
    session: ClientSession


@asynccontextmanager
async def connect(url: str = DEFAULT_UPSTREAM_URL) -> AsyncIterator[UpstreamConnection]:
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield UpstreamConnection(session=session)
