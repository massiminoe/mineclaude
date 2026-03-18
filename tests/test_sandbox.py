"""Tests for the sandbox code executor."""

import pytest

from agent.bridge import MockBridgeClient
from agent.primitives import make_primitives
from agent.sandbox import SandboxError, execute


@pytest.fixture
def primitives():
    bridge = MockBridgeClient()
    return make_primitives(bridge)


@pytest.mark.asyncio
async def test_simple_return(primitives):
    result = await execute('return "hello"', primitives)
    assert result == "hello"


@pytest.mark.asyncio
async def test_math(primitives):
    result = await execute("return str(2 + 2)", primitives)
    assert result == "4"


@pytest.mark.asyncio
async def test_await_primitive(primitives):
    result = await execute(
        'result = await goToPosition(10, 64, 20)\nreturn result',
        primitives,
    )
    assert "Moved to" in result


@pytest.mark.asyncio
async def test_log_output(primitives):
    result = await execute(
        'log("hello")\nlog("world")\nreturn "done"',
        primitives,
    )
    assert "done" in result
    assert "hello" in result
    assert "world" in result


@pytest.mark.asyncio
async def test_print_redirects_to_log(primitives):
    result = await execute(
        'print("printed")\nreturn "done"',
        primitives,
    )
    assert "printed" in result


@pytest.mark.asyncio
async def test_no_return(primitives):
    result = await execute('x = 1 + 1', primitives)
    assert "no return value" in result.lower()


@pytest.mark.asyncio
async def test_reject_import(primitives):
    with pytest.raises(SandboxError, match="Imports are not allowed"):
        await execute("import os", primitives)


@pytest.mark.asyncio
async def test_reject_from_import(primitives):
    with pytest.raises(SandboxError, match="Imports are not allowed"):
        await execute("from os import path", primitives)


@pytest.mark.asyncio
async def test_reject_dunder_access(primitives):
    with pytest.raises(SandboxError, match="dunder"):
        await execute('x = "".__class__', primitives)


@pytest.mark.asyncio
async def test_syntax_error(primitives):
    with pytest.raises(SandboxError, match="Syntax error"):
        await execute("def foo(:", primitives)


@pytest.mark.asyncio
async def test_runtime_error(primitives):
    with pytest.raises(SandboxError, match="Action error"):
        await execute("x = 1 / 0", primitives)


@pytest.mark.asyncio
async def test_no_builtins_escape(primitives):
    with pytest.raises(SandboxError):
        await execute('__import__("os")', primitives)


@pytest.mark.asyncio
async def test_collect_block(primitives):
    result = await execute(
        'result = await collectBlock("oak_log", 2)\nreturn result',
        primitives,
    )
    assert "Collected" in result
