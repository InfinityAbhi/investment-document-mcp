"""Deterministic MCP client demonstration; no model API or account required."""
import asyncio
import json
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent


def decode(result):
    if result.is_error:
        raise RuntimeError(str(result.content))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads("".join(block.text for block in result.content if block.type == "text"))


async def main():
    async with Client(StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")])) as client:
        listing = await client.list_tools()
        print("Discovered MCP tools:", ", ".join(tool.name for tool in listing.tools))
        results = {}
        results["search"] = decode(await client.call_tool("search_reports", {"query": "Harbor Credit"}))
        document_id = results["search"]["results"][0]["source"]["document_id"]
        results["report"] = decode(await client.call_tool("read_report", {"document_id": document_id}))
        results["metric"] = decode(await client.call_tool("get_metric", {
            "fund_id": "harbor_credit", "period": "2026Q2", "metric": "nav_usd"}))
        results["comparison"] = decode(await client.call_tool("compare_periods", {
            "fund_id": "harbor_credit", "start_period": "2026Q1", "end_period": "2026Q2"}))
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
