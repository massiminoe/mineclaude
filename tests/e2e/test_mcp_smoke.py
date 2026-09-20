"""Exercise real MCP dispatch, native crafting, inventory refresh and monitor."""
import ast
import asyncio
import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.e2e


def payload(result):
    assert not result.isError, result
    return json.loads(next(block.text for block in result.content if block.type == "text"))


def count(inventory, name):
    return sum(item["count"] for item in inventory if item["name"].removeprefix("minecraft:") == name)


async def test_mcp_crafts_planks(live_runtime):
    stack = live_runtime
    # RCON only sets up a known inventory; crafting itself must happen over MCP.
    for command in ("clear @a", "give @a minecraft:oak_log 1"):
        stack["compose"]("exec", "-T", "mc-server", "rcon-cli", command, capture_output=True, text=True)
    async with streamable_http_client(stack["mcp"]) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names == {"execute", "interrupt", "get_state", "screenshot", "get_handler",
                             "set_handler", "wait_for_event", "wait_for_action"}
            # Let the server's inventory update reach the client before crafting.
            for _ in range(30):
                before = payload(await session.call_tool("execute", {"code": "return await getInventory()"}))
                if count(ast.literal_eval(before.get("result") or "[]"), "oak_log") == 1:
                    break
                await asyncio.sleep(.2)
            else:
                pytest.fail(f"RCON setup did not reach the client: {before}")
            result = payload(await session.call_tool("execute", {
                "code": "return await craft('oak_planks', 4)", "timeout": 30, "wait": 40}))
            assert result["status"] == "completed", result
            after = payload(await session.call_tool("execute", {"code": "return await getInventory()"}))
            after["result"] = ast.literal_eval(after["result"])
            assert count(after["result"], "oak_planks") == 4, after
            assert count(after["result"], "oak_log") == 0, after
            state = payload(await session.call_tool("get_state", {"flush": False}))
            assert count(state["inventory"], "oak_planks") == 4, state
            screenshot = await session.call_tool("screenshot", {})
            assert not screenshot.isError
            assert any(block.type == "image" and block.data for block in screenshot.content)
    async with httpx.AsyncClient() as client:
        response = await client.get(stack["monitor"] + "/api/state")
        response.raise_for_status()
        monitor = response.json()
        assert count(monitor["game"]["inventory"], "oak_planks") == 4, monitor
    (stack["evidence"] / "result.json").write_text(json.dumps({
        "tools": sorted(names), "craft": result, "inventory": after["result"],
        "state": state, "monitor": monitor, "screenshot": "received",
    }, indent=2) + "\n")
