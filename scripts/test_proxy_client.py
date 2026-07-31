"""
End-to-end test: acts as an MCP client against the PROXY (not the real
server), repeats the SRCByteReader incoming-calls query, and prints output
size to compare against the raw baseline captured in probe_output.txt.

Usage:
    .venv/Scripts/python.exe scripts/test_proxy_client.py
"""

import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

PROXY_URL = "http://127.0.0.1:8765/stream"
OUT_PATH = "scripts/proxy_test_output.txt"


async def main() -> None:
    out = open(OUT_PATH, "w", encoding="utf-8")

    def w(*args: object) -> None:
        print(*args, file=out)

    async with streamable_http_client(PROXY_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            w(f"=== tools/list ({len(tools.tools)} tools) ===")
            for t in tools.tools:
                if t.name == "analyze_calls":
                    w(f"- {t.name}: {t.description}")
                    w(f"  schema properties: {sorted(t.input_schema.get('properties', {}).keys())}")
                else:
                    w(f"- {t.name}")

            base_args = {
                "symbolFqn": "com.csc.conan.format.src.unmarshalling.fixedbyte.SRCByteReader.SRCByteReader",
                "analysisKind": "INCOMING_CALLS",
                "projectPath": "C:/Users/abalaban/IdeaProjects/kwg-backend",
                "depth": 5,
            }

            for label, extra in [
                ("mode=summary (default)", {}),
                ("mode=tree, sourceScope=production", {"mode": "tree", "sourceScope": "production"}),
                ("mode=summary, sourceScope=test", {"mode": "summary", "sourceScope": "test"}),
            ]:
                w(f"\n=== analyze_calls via proxy: {label} ===")
                result = await session.call_tool("analyze_calls", {**base_args, **extra})
                text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
                w(f"[size: {len(text)} chars]")
                w(text)

            # Sanity check a passthrough tool still works unchanged.
            w("\n=== search_symbol via proxy (passthrough check) ===")
            result = await session.call_tool(
                "search_symbol",
                {"q": "SRCByteReader", "projectPath": "C:/Users/abalaban/IdeaProjects/kwg-backend"},
            )
            for c in result.content:
                if hasattr(c, "text"):
                    w(c.text)

    out.close()
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
