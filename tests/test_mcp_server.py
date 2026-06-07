"""Tests for the MCP server wiring (P4).

Exercises the FastMCP tool surface in-process against a real Runtime over the
mock bridge — confirming the 7 tools list, delegate to Runtime, and convert
returns (structured dicts + an image block) correctly. The streamable-HTTP
transport itself is the SDK's concern; here we pin OUR wiring.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")  # optional [mcp] extra

from mineclaude.bridge import MockBridgeClient
from mineclaude.runtime import Runtime
from mineclaude.mcp_server import build_mcp


def _split(res):
    """FastMCP.call_tool returns [content] for unstructured tools and
    (content, structured) for structured ones. Normalize to (content, structured)."""
    if isinstance(res, tuple):
        return res[0], res[1]
    return res, None


def _make():
    rt = Runtime(MockBridgeClient())
    rt.start()
    return rt, build_mcp(rt)


async def test_tools_list_is_the_seven():
    _rt, mcp = _make()
    names = {t.name for t in await mcp.list_tools()}
    assert names == {
        "execute", "interrupt", "get_state", "screenshot",
        "get_handler", "set_handler", "wait_for_event",
    }


async def test_execute_tool_returns_completed_structured():
    _rt, mcp = _make()
    _content, structured = _split(await mcp.call_tool("execute", {"code": "return 6 * 7"}))
    assert structured["status"] == "completed"
    assert structured["result"] == "42"
    assert structured["action_id"]


async def test_execute_tool_reports_code_failure():
    _rt, mcp = _make()
    _content, structured = _split(await mcp.call_tool("execute", {"code": "return nope"}))
    assert structured["status"] == "failed"
    assert "Action error" in (structured["error"] or "")


async def test_get_state_tool_structured_shape():
    _rt, mcp = _make()
    _content, structured = _split(await mcp.call_tool("get_state", {}))
    assert "player" in structured
    assert "pos" in structured["player"]
    assert "inventory" in structured
    assert structured["action"]["state"] in ("idle", "running", "completed")


async def test_interrupt_tool_ok():
    _rt, mcp = _make()
    _content, structured = _split(await mcp.call_tool("interrupt", {}))
    assert structured == {"ok": True}


async def test_get_and_set_handler_tools():
    rt, mcp = _make()
    _c, default = _split(await mcp.call_tool("get_handler", {"event_type": "chat"}))
    assert default["source"] == "default"
    assert default["code"] is None

    _c, authored = _split(await mcp.call_tool(
        "set_handler", {"event_type": "chat", "code": "log('hi')", "cooldown_s": 2.0}
    ))
    assert authored["source"] == "authored"
    assert authored["code"] == "log('hi')"
    assert authored["cooldown_s"] == 2.0
    assert "chat" in rt.reflexes.known_types()


async def test_set_handler_tool_rejects_import():
    _rt, mcp = _make()
    with pytest.raises(Exception):
        await mcp.call_tool("set_handler", {"event_type": "chat", "code": "import os"})


async def test_wait_for_event_tool_timeout_shape():
    _rt, mcp = _make()
    _content, structured = _split(
        await mcp.call_tool("wait_for_event", {"types": ["never"], "timeout": 0.05})
    )
    assert structured == {"timed_out": True, "event": None}


async def test_screenshot_tool_returns_image_block():
    _rt, mcp = _make()
    content, _structured = _split(await mcp.call_tool("screenshot", {}))
    assert len(content) == 1
    block = content[0]
    assert block.type == "image"
    assert block.data  # base64 payload
    assert block.mimeType.startswith("image/")
