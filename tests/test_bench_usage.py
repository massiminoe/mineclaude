"""Parser tests for bench/usage.py.

The bench's three harnesses report usage in three different shapes, and the only
way a wrong parser shows up in practice is as a plausible-looking but wrong
number in a cost-per-advancement table. These fixtures pin each shape, including
the two that can't be produced without the real CLI + a paid subscription.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bench_usage", REPO / "bench" / "usage.py")
bench_usage = importlib.util.module_from_spec(spec)
sys.modules["bench_usage"] = bench_usage
spec.loader.exec_module(bench_usage)


def write_run(tmp_path: Path, harness: str, model: str, files: dict[str, str]) -> Path:
    """Lay out a run dir (metadata.json + harness/) and return the harness dir."""
    run = tmp_path / "run"
    harness_dir = run / "harness"
    harness_dir.mkdir(parents=True)
    (run / "metadata.json").write_text(json.dumps({"harness": harness, "model": model}))
    for name, body in files.items():
        (harness_dir / name).write_text(body)
    return harness_dir


def jsonl(*events: dict) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def test_defaults_to_claude_code_without_metadata(tmp_path: Path) -> None:
    """Runs recorded before harnesses were selectable must still parse."""
    harness_dir = tmp_path / "harness"
    harness_dir.mkdir()
    (harness_dir / "claude-1.jsonl").write_text(
        jsonl({"type": "result", "usage": {"input_tokens": 5, "output_tokens": 7}, "num_turns": 1})
    )
    result = bench_usage.summarize(harness_dir)
    assert result["harness"] == "claude-code"
    assert result["tokens"]["output"] == 7


def test_unknown_kind_is_refused(tmp_path: Path) -> None:
    harness_dir = tmp_path / "harness"
    harness_dir.mkdir()
    with pytest.raises(SystemExit):
        bench_usage.summarize(harness_dir, kind="aider")


# --------------------------------------------------------------------------- #
# opencode
# --------------------------------------------------------------------------- #
def step(cost: float, inp: int, out: int, read: int, write: int = 0, reason: str = "tool-calls") -> dict:
    return {
        "type": "step_finish",
        "part": {
            "type": "step-finish",
            "cost": cost,
            "reason": reason,
            "tokens": {"input": inp, "output": out, "reasoning": 0, "cache": {"read": read, "write": write}},
        },
    }


def test_opencode_sums_step_finish_across_invocations(tmp_path: Path) -> None:
    harness_dir = write_run(
        tmp_path,
        "opencode",
        "opencode-go/qwen3.8-flash",
        {
            "opencode-1.jsonl": jsonl(
                {"type": "step_start"},
                step(0.01, 600, 40, 20_000, write=1_000),
                {"type": "text", "text": "mining"},
                step(0.02, 700, 50, 30_000, reason="stop"),
            ),
            "opencode-2.jsonl": jsonl(step(0.03, 800, 60, 40_000)),
        },
    )
    result = bench_usage.summarize(harness_dir)

    assert result["harness"] == "opencode"
    assert result["invocations"] == 2
    assert result["turns"] == 3  # one step == one model request
    assert result["tokens"] == {
        "input": 2_100,
        "output": 150,
        "thinking": 0,
        "cache_write": 1_000,
        "cache_read": 90_000,
    }
    assert result["total_tokens"] == 93_250
    assert result["cost_usd"] == pytest.approx(0.06)
    assert result["cost_basis"] == "gateway_metered"
    assert result["terminal_reasons"] == {"tool-calls": 2, "stop": 1}
    assert result["by_model"]["opencode-go/qwen3.8-flash"]["cache_read"] == 90_000
    # last_step is what makes the per-step assumption checkable from the artifact.
    assert result["per_invocation"][0]["steps"] == 2
    assert result["per_invocation"][0]["last_step"]["cache_read"] == 30_000
    assert not result["health"]["throttled"]


def test_opencode_flags_a_transcript_with_no_steps(tmp_path: Path) -> None:
    """`opencode run --format json` can exit before flushing its last event."""
    harness_dir = write_run(
        tmp_path,
        "opencode",
        "opencode-go/gpt-5.6-luna",
        {"opencode-1.jsonl": jsonl(step(0.01, 10, 2, 100)), "opencode-2.jsonl": jsonl({"type": "step_start"})},
    )
    result = bench_usage.summarize(harness_dir)
    assert result["health"]["invocations_without_result"] == ["opencode-2.jsonl"]


def test_opencode_quarantines_a_rate_limited_trial(tmp_path: Path) -> None:
    harness_dir = write_run(
        tmp_path,
        "opencode",
        "opencode-go/qwen3.8-max",
        {"opencode-1.jsonl": jsonl(step(0.01, 10, 2, 100), {"type": "error", "error": "429 Too Many Requests"})},
    )
    result = bench_usage.summarize(harness_dir)
    assert result["health"]["throttled"] is True
    assert result["health"]["rate_limit_events"] == 1


def test_opencode_does_not_quarantine_on_transcript_text(tmp_path: Path) -> None:
    """A transcript is full of tool output; only error payloads may quarantine.

    Both shapes below sank the first real pilot run: the skill's primitives.md
    has a line numbered 429, and every world event carries an epoch timestamp
    containing the digits 429.
    """
    harness_dir = write_run(
        tmp_path,
        "opencode",
        "opencode-go/qwen3.8-flash",
        {
            "opencode-1.jsonl": jsonl(
                {"type": "text", "text": "429: Right-click an entity - the use-key twin of attack()."},
                {"type": "text", "text": '{"type": "block_broken", "ts": 1788054294.237}'},
                {"type": "text", "text": "the server said rate limit in a tool result, not an error"},
                step(0.01, 10, 2, 100),
            )
        },
    )
    result = bench_usage.summarize(harness_dir)
    assert result["health"]["throttled"] is False
    assert result["health"]["rate_limit_events"] == 0
    assert result["turns"] == 1


def test_opencode_counts_non_rate_limit_errors_without_quarantining(tmp_path: Path) -> None:
    harness_dir = write_run(
        tmp_path,
        "opencode",
        "opencode-go/qwen3.8-flash",
        {
            "opencode-1.jsonl": jsonl(
                {"type": "error", "error": {"name": "UnknownError", "data": {"message": "server error"}}},
                step(0.01, 10, 2, 100),
            )
        },
    )
    result = bench_usage.summarize(harness_dir)
    assert result["health"]["errors"] == 1
    assert result["health"]["throttled"] is False


def test_opencode_reads_rate_limits_from_stderr_too(tmp_path: Path) -> None:
    harness_dir = write_run(
        tmp_path, "opencode", "opencode-go/glm-5.3-flash", {"opencode-1.jsonl": jsonl(step(0.01, 10, 2, 100))}
    )
    (harness_dir / "opencode-1.err").write_text("ERROR provider rate limit exceeded\n")
    assert bench_usage.summarize(harness_dir)["health"]["throttled"] is True


# --------------------------------------------------------------------------- #
# cursor
# --------------------------------------------------------------------------- #
def cursor_ledger(**overrides) -> dict:
    ledger = {
        "agent_id": "local-abc",
        "model": "composer-2.5",
        "billed": {
            "usage": {
                "inputTokens": 1_200,
                "outputTokens": 3_400,
                "cacheReadTokens": 5_000_000,
                "cacheWriteTokens": 90_000,
                "totalTokens": 5_094_600,
                "reasoningTokens": 800,
            },
            # chargedCents is 0 under a plan; rawCostCents is the model cost.
            "cost": {"rawCostCents": 812.5, "chargedCents": 0},
            "runs": [{"runId": "t1"}, {"runId": "t2"}],
        },
        "streamed": {"inputTokens": 1_200, "outputTokens": 3_400},
        "invocations": [{"file": "cursor-1.jsonl", "events": 40, "tool_calls": 12, "error": None}],
    }
    ledger.update(overrides)
    return ledger


def test_cursor_reads_the_billed_ledger(tmp_path: Path) -> None:
    harness_dir = write_run(
        tmp_path,
        "cursor",
        "composer-2.5",
        {
            "cursor-1.jsonl": jsonl({"type": "assistant"}, {"type": "usage", "usage": {"inputTokens": 1_200}}),
            "cursor-usage.json": json.dumps(cursor_ledger()),
        },
    )
    result = bench_usage.summarize(harness_dir)

    assert result["harness"] == "cursor"
    assert result["turns"] == 2
    assert result["tokens"]["cache_read"] == 5_000_000
    assert result["tokens"]["thinking"] == 800
    assert result["total_tokens"] == 5_094_600
    # Raw model cost is the list-price analogue; charged is 0 under the plan.
    assert result["cost_usd"] == pytest.approx(8.125)
    assert result["charged_usd"] == pytest.approx(0.0)
    assert result["cost_basis"] == "cursor_raw_cost"
    assert result["by_model"]["composer-2.5"]["output"] == 3_400
    assert not result["health"]["throttled"]


def test_cursor_missing_ledger_is_not_a_free_run(tmp_path: Path) -> None:
    """No getUsage() result must read as unavailable, never as zero cost."""
    harness_dir = write_run(tmp_path, "cursor", "grok-4.6", {"cursor-1.jsonl": jsonl({"type": "assistant"})})
    result = bench_usage.summarize(harness_dir)
    assert result["cost_usd"] is None
    assert result["tokens"] is None
    assert result["total_tokens"] is None
    assert result["health"]["invocations_without_result"] == ["cursor-1.jsonl"]


def test_cursor_entitlement_gated_usage_reports_why(tmp_path: Path) -> None:
    """The real shape from an individual Pro account: a ledger with no `billed`.

    Zeros here would read as a free run and drag any mean toward zero, so the
    tokens must come back None with the reason attached.
    """
    ledger = {
        "agent_id": "agent-1",
        "model": "composer-2.5",
        "billed": None,
        "usage_error": "[feature_unavailable] This feature is not available for your account",
        "streamed": {"inputTokens": 0, "outputTokens": 0},
        "invocations": [{"file": "cursor-1.jsonl", "events": 544, "tool_calls": 103, "error": None}],
    }
    harness_dir = write_run(
        tmp_path,
        "cursor",
        "composer-2.5",
        {
            "cursor-1.jsonl": jsonl({"type": "assistant"}, {"type": "tool_call"}, {"type": "assistant"}),
            "cursor-usage.json": json.dumps(ledger),
        },
    )
    result = bench_usage.summarize(harness_dir)
    assert result["tokens"] is None
    assert result["total_tokens"] is None
    assert result["cost_usd"] is None
    assert result["cost_basis"] == "unavailable"
    assert result["by_model"] == {}
    assert result["health"]["usage_available"] is False
    assert "feature_unavailable" in result["health"]["usage_error"]
    # Turn/tool counts still land — they are what a Cursor entry can be compared on.
    assert result["turns"] == 2
    assert result["tool_calls"] == 103


def test_cursor_does_not_quarantine_on_transcript_text(tmp_path: Path) -> None:
    """Same trap as opencode: tool output must never trip the throttle flag."""
    harness_dir = write_run(
        tmp_path,
        "cursor",
        "composer-2.5",
        {
            "cursor-1.jsonl": jsonl(
                {"type": "assistant", "message": {"text": "429: see primitives.md; ts 1788054294"}}
            ),
            "cursor-usage.json": json.dumps(cursor_ledger()),
        },
    )
    result = bench_usage.summarize(harness_dir)
    assert result["health"]["throttled"] is False


def test_cursor_rate_limit_error_quarantines(tmp_path: Path) -> None:
    ledger = cursor_ledger(
        invocations=[{"file": "cursor-1.jsonl", "events": 3, "tool_calls": 0, "error": "RateLimitError: 429"}]
    )
    harness_dir = write_run(tmp_path, "cursor", "grok-4.6", {"cursor-usage.json": json.dumps(ledger)})
    result = bench_usage.summarize(harness_dir)
    assert result["health"]["throttled"] is True
    assert result["terminal_reasons"] == {"error": 1}
