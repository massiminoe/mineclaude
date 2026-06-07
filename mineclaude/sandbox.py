"""Restricted Python code executor for LLM-generated action code."""

from __future__ import annotations

import ast
import math
import traceback
from typing import Any

from mineclaude.primitives import _log_buffer


class SandboxError(Exception):
    pass


# Safe builtins exposed to sandbox code
SAFE_BUILTINS = {
    # Types
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "frozenset": frozenset,
    "bytes": bytes,
    "bytearray": bytearray,
    "type": type,
    "None": None,
    "True": True,
    "False": False,
    # Iteration
    "range": range,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "reversed": reversed,
    "sorted": sorted,
    "iter": iter,
    "next": next,
    "any": any,
    "all": all,
    # Math
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
    "len": len,
    "pow": pow,
    # String/repr
    "repr": repr,
    "isinstance": isinstance,
    "hasattr": hasattr,
    "getattr": getattr,
    # Exceptions (so user code can catch them)
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "RuntimeError": RuntimeError,
}


_BLOCKED_NODES = (ast.Import, ast.ImportFrom)


def _validate_ast(code: str) -> None:
    """Reject dangerous AST patterns."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SandboxError(f"Syntax error: {e}")

    for node in ast.walk(tree):
        if isinstance(node, _BLOCKED_NODES):
            raise SandboxError("Imports are not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            raise SandboxError(f"Access to dunder attribute '{node.attr}' is not allowed")


async def execute(code: str, primitives: dict[str, Any]) -> str:
    """Execute sandboxed code with the given primitives.

    The code is wrapped in an async function so `await` works.
    Returns the result as a string, with any log output appended.
    """
    _validate_ast(code)

    # Clear log buffer
    _log_buffer.clear()

    # Redirect print to log
    log_fn = primitives.get("log", lambda msg: _log_buffer.append(str(msg)))

    safe_builtins = dict(SAFE_BUILTINS)
    safe_builtins["print"] = lambda *args, **kwargs: log_fn(" ".join(str(a) for a in args))

    # Wrap code in async function
    indented = "\n".join("    " + line for line in code.splitlines())
    wrapped = f"async def __action__():\n{indented}\n"

    # Build restricted globals
    restricted_globals = {"__builtins__": safe_builtins}
    restricted_globals["math"] = math
    restricted_globals.update(primitives)

    try:
        exec(compile(wrapped, "<action>", "exec"), restricted_globals)
        action_fn = restricted_globals["__action__"]
        result = await action_fn()
    except SandboxError:
        raise
    except Exception as e:
        # Sanitize traceback — only show the <action> frames
        tb_lines = traceback.format_exception(type(e), e, e.__traceback__)
        sanitized = []
        for line in tb_lines:
            if "<action>" in line or not line.startswith("  File"):
                sanitized.append(line)
        raise SandboxError(f"Action error: {type(e).__name__}: {e}")

    # Build result string
    result_str = str(result) if result is not None else "Action completed (no return value)"

    if _log_buffer:
        log_output = "\n".join(_log_buffer)
        result_str += f"\n\n[Log]\n{log_output}"

    return result_str
