"""
Ad-hoc probe of the real JetBrains IDE MCP server (not the proxy).

Confirms: transport handshake works, tools/list shape, and the exact raw
text format analyze_calls returns, so the proxy's tree parser can be built
against ground truth instead of guesswork.

Output is written to scripts/probe_output.txt (UTF-8) because the raw
analyze_calls text contains Unicode box-drawing characters that crash on
Windows' default cp1252 console encoding if printed directly.

Usage:
    .venv/Scripts/python.exe scripts/probe_real_server.py
"""

import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REAL_SERVER_URL = "http://127.0.0.1:64342/stream"
OUT_PATH = "scripts/probe_output.txt"


async def main() -> None:
    out = open(OUT_PATH, "w", encoding="utf-8")

    def w(*args: object) -> None:
        print(*args, file=out)

    async with streamable_http_client(REAL_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            init_result = await session.initialize()
            w("=== initialize ===")
            w(init_result)

            w("\n=== tools/list ===")
            tools = await session.list_tools()
            for t in tools.tools:
                w(f"- {t.name}")

            w("\n=== search_symbol('SRCByteReader') ===")
            result = await session.call_tool(
                "search_symbol",
                {"q": "SRCByteReader", "projectPath": "C:/Users/abalaban/IdeaProjects/kwg-backend"},
            )
            for c in result.content:
                w(repr(c))

            w("\n=== analyze_calls (depth=3) raw text ===")
            result = await session.call_tool(
                "analyze_calls",
                {
                    "symbolFqn": "com.csc.conan.format.src.unmarshalling.fixedbyte.SRCByteReader.SRCByteReader",
                    "analysisKind": "INCOMING_CALLS",
                    "projectPath": "C:/Users/abalaban/IdeaProjects/kwg-backend",
                    "depth": 3,
                },
            )
            for c in result.content:
                text = getattr(c, "text", None)
                w(text if text is not None else repr(c))

    out.close()
    print(f"wrote {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
