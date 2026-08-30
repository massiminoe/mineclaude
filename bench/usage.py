#!/usr/bin/env python3
"""Fold a bench run's harness transcripts into one usage.json.

The token ledger of a run — what it cost to earn its advancements. Reads
whatever transcript the run's harness left in `harness/` and writes a compact
summary next to score.json, so cost analysis never has to re-download hundreds
of MB of transcript.

One output schema, three parsers — a benchmark entry is (harness, model), and
cost per advancement has to be comparable across harnesses. Which parser runs is
decided by `--kind` (default: the `harness` field of the run's metadata.json,
falling back to claude-code, so every pre-existing run re-parses identically).

**claude-code** (`claude-*.jsonl`, stream-json). Two things about the source
format are load-bearing:

1. **Only the `result` event is authoritative.** The per-message `usage` blocks
   on `assistant` events are mid-stream snapshots — there are more of them than
   there are turns, and summing them overcounts cache reads while undercounting
   output (measured on one run: 37.8M/2.4k summed vs the result event's
   23.0M/91.8k). So we take the LAST `result` event in each file and ignore the
   rest.
2. **Each invocation's result is its own, not cumulative.** The harness loop
   re-enters with `--continue`, so every claude-N.jsonl shares a session id but
   reports only the tokens that invocation spent. Summing across files is
   therefore correct, not double-counting.

**opencode** (`opencode-*.jsonl`, `--format json`). Usage rides on `step_finish`
events: `part.cost` in USD plus `part.tokens {input, output, reasoning,
cache:{read, write}}`. These are per-step (one LLM request), so they sum — the
`steps` and `last_step` fields on each per-invocation entry exist so that
assumption stays checkable against a real transcript rather than trusted. The
cost figure is the same number opencode Zen meters the Go plan's dollar caps
against, so this file doubles as quota tracking.

**cursor** (`cursor-usage.json`, written by the harness from `agent.getUsage()`).
The Cursor CLI's own stream-json carries no usage at all, which is why that
harness drives the SDK instead. `rawCostCents` is the undiscounted model cost —
the right analogue of Claude Code's list-price number, and the one that stays
meaningful under a subscription; `chargedCents` (0 for plan-included usage) is
kept alongside it as `charged_usd`.

Health matters as much as the totals. A harness that got throttled scores low
for reasons that have nothing to do with the model, so rate-limit signals are
tallied and a hard rejection sets `health.throttled` — the flag analysis uses to
quarantine a trial instead of silently averaging it in.

`cost_basis` names what the cost figure actually is: a list-price equivalent, a
gateway's metered charge, or a vendor's raw cost. Runs authenticated with a
subscription are not billed per token; the token counts are real either way.

Usage:
    bench/usage.py --harness <run>/harness [--kind claude-code] [--out <run>/usage.json]
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

# result.usage -> our flatter names. Cache write is split 1h/5m below.
TOKEN_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_write": "cache_creation_input_tokens",
    "cache_read": "cache_read_input_tokens",
}

TOKEN_KEYS = ("input", "output", "thinking", "cache_write", "cache_read")

# Provider-agnostic rate-limit signals, for the harnesses that don't emit a
# structured rate_limit_event the way Claude Code does.
#
# Only ever matched against *error* payloads and stderr — never a whole
# transcript. A transcript carries every tool result the agent saw, and the
# mineclaude skill alone is enough to poison a loose match: the first pilot run
# was wrongly quarantined because `primitives.md` has a line numbered 429 and
# because epoch timestamps like 1788054294 contain "429". Hence \b429\b (digits
# are word characters, so a timestamp can no longer match) plus a narrow scope.
RATE_LIMIT_RE = re.compile(
    r"\b429\b|rate[ _-]?limit|too many requests|quota exceeded|overloaded|insufficient[ _]credit",
    re.IGNORECASE,
)


def empty_tokens() -> dict:
    return {"input": 0, "output": 0, "thinking": 0, "cache_write": 0, "cache_read": 0}


def blank_health() -> dict:
    return {
        "rate_limit_events": 0,
        "warnings": 0,
        "rejections": 0,
        "out_of_credits": 0,
        "errors": 0,
        "throttled": False,
        "invocations_without_result": [],
    }


def invocation_order(path: Path) -> int:
    m = re.search(r"-(\d+)\.jsonl$", path.name)
    return int(m.group(1)) if m else 0


def iter_events(path: Path):
    """Yield parsed JSON objects from a JSONL transcript, skipping junk lines."""
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def scan_stderr_rate_limits(paths: list[Path]) -> int:
    """Count rate-limit signals in harness stderr.

    Stderr only — it holds the CLI's own failures, not the agent's tool output.
    """
    hits = 0
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            if RATE_LIMIT_RE.search(line):
                hits += 1
    return hits


# --------------------------------------------------------------------------- #
# claude-code
# --------------------------------------------------------------------------- #
def last_result(path: Path) -> dict | None:
    """The final `result` event in a transcript — the invocation's own totals."""
    found = None
    for event in iter_events(path):
        if event.get("type") == "result":
            found = event
    return found


def rate_limit_health(paths: list[Path]) -> dict:
    """Tally rate-limit events; a hard rejection quarantines the trial."""
    events = warnings = rejections = out_of_credits = 0
    for path in paths:
        with path.open() as fh:
            for line in fh:
                if '"rate_limit_event"' not in line:
                    continue
                try:
                    info = json.loads(line).get("rate_limit_info") or {}
                except json.JSONDecodeError:
                    continue
                events += 1
                status, overage = info.get("status"), info.get("overageStatus")
                if status == "allowed_warning":
                    warnings += 1
                if status == "rejected":
                    rejections += 1
                if info.get("overageDisabledReason") == "out_of_credits" or overage == "rejected":
                    out_of_credits += 1
    return {
        "rate_limit_events": events,
        "warnings": warnings,
        "rejections": rejections,
        "out_of_credits": out_of_credits,
        # A rejected request means the harness was denied capacity mid-run: the
        # trial measured the rate limiter, not the model. Overage-rejected on its
        # own still let the request through, so it warns rather than quarantines.
        "throttled": rejections > 0,
    }


def summarize_claude_code(harness_dir: Path) -> dict:
    paths = sorted(harness_dir.glob("claude-*.jsonl"), key=invocation_order)
    tokens = dict.fromkeys(TOKEN_FIELDS, 0)
    tokens.update(thinking=0, cache_write_1h=0, cache_write_5m=0)
    per_invocation, terminal, by_model = [], Counter(), {}
    cost = 0.0
    turns = 0
    no_result = []

    for path in paths:
        result = last_result(path)
        if result is None:
            # An invocation killed before it could emit a result — its tokens are
            # unrecoverable. Named, not silently dropped, so a partial total is
            # visible as partial.
            no_result.append(path.name)
            continue
        usage = result.get("usage") or {}
        for name, src in TOKEN_FIELDS.items():
            tokens[name] += usage.get(src, 0)
        tokens["thinking"] += (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0)
        creation = usage.get("cache_creation") or {}
        tokens["cache_write_1h"] += creation.get("ephemeral_1h_input_tokens", 0)
        tokens["cache_write_5m"] += creation.get("ephemeral_5m_input_tokens", 0)
        cost += result.get("total_cost_usd", 0.0) or 0.0
        turns += result.get("num_turns", 0) or 0
        terminal[f"{result.get('subtype')}/{result.get('terminal_reason')}"] += 1
        for model, stats in (result.get("modelUsage") or {}).items():
            acc = by_model.setdefault(model, {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "cost_usd": 0.0})
            acc["input"] += stats.get("inputTokens", 0)
            acc["output"] += stats.get("outputTokens", 0)
            acc["cache_write"] += stats.get("cacheCreationInputTokens", 0)
            acc["cache_read"] += stats.get("cacheReadInputTokens", 0)
            acc["cost_usd"] = round(acc["cost_usd"] + (stats.get("costUSD") or 0.0), 6)
        per_invocation.append({
            "file": path.name,
            "session_id": result.get("session_id"),
            "turns": result.get("num_turns"),
            "duration_ms": result.get("duration_ms"),
            "cost_usd": round(result.get("total_cost_usd", 0.0) or 0.0, 6),
            "terminal": f"{result.get('subtype')}/{result.get('terminal_reason')}",
            "tokens": {
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "cache_write": usage.get("cache_creation_input_tokens", 0),
                "cache_read": usage.get("cache_read_input_tokens", 0),
            },
        })

    health = rate_limit_health(paths)
    health["invocations_without_result"] = no_result
    return {
        "invocations": len(paths),
        "turns": turns,
        "tokens": tokens,
        "total_tokens": sum(tokens[k] for k in ("input", "output", "cache_write", "cache_read")),
        "cost_usd": round(cost, 6),
        # The CLI's list-price computation. The bench authenticates with a
        # subscription token, which is not billed per token — so this is an
        # equivalent, not an invoice.
        "cost_basis": "list_price_estimate",
        "by_model": by_model,
        "terminal_reasons": dict(terminal),
        "health": health,
        "per_invocation": per_invocation,
    }


# --------------------------------------------------------------------------- #
# opencode
# --------------------------------------------------------------------------- #
def summarize_opencode(harness_dir: Path, model: str | None) -> dict:
    paths = sorted(harness_dir.glob("opencode-*.jsonl"), key=invocation_order)
    tokens = empty_tokens()
    per_invocation, terminal = [], Counter()
    cost = 0.0
    steps_total = 0
    no_result = []
    errors: list[str] = []

    for path in paths:
        inv_tokens = empty_tokens()
        inv_cost = 0.0
        steps = 0
        last_step = None
        for event in iter_events(path):
            if event.get("type") == "error":
                # Structured errors are the only trustworthy failure signal; the
                # rest of the stream is agent chatter and tool output.
                errors.append(json.dumps(event))
                continue
            if event.get("type") != "step_finish":
                continue
            part = event.get("part") or event
            step = part.get("tokens") or {}
            cache = step.get("cache") or {}
            step_tokens = {
                "input": step.get("input", 0) or 0,
                "output": step.get("output", 0) or 0,
                "thinking": step.get("reasoning", 0) or 0,
                "cache_write": cache.get("write", 0) or 0,
                "cache_read": cache.get("read", 0) or 0,
            }
            for key in TOKEN_KEYS:
                inv_tokens[key] += step_tokens[key]
            inv_cost += part.get("cost", 0.0) or 0.0
            steps += 1
            last_step = step_tokens
            if part.get("reason"):
                terminal[str(part["reason"])] += 1
        if steps == 0:
            # No step_finish at all: either the invocation died before its first
            # model call, or `opencode run --format json` exited before flushing
            # the final event. Named rather than silently dropped.
            no_result.append(path.name)
        for key in TOKEN_KEYS:
            tokens[key] += inv_tokens[key]
        cost += inv_cost
        steps_total += steps
        per_invocation.append({
            "file": path.name,
            "steps": steps,
            "cost_usd": round(inv_cost, 6),
            "tokens": inv_tokens,
            # step_finish tokens are read as per-step and summed. Keeping the
            # final step alongside the sum makes that assumption falsifiable from
            # the artifact itself: under cumulative semantics last_step would
            # equal the invocation total.
            "last_step": last_step,
        })

    hits = sum(1 for e in errors if RATE_LIMIT_RE.search(e))
    hits += scan_stderr_rate_limits([harness_dir / f"{p.stem}.err" for p in paths])
    health = blank_health()
    health.update(
        rate_limit_events=hits,
        # opencode surfaces provider throttling as an ordinary error event rather
        # than a structured rate_limit_event, so a rate-limited error is all the
        # signal there is — treat it as a quarantine, same as a Claude Code
        # rejection. Errors that aren't rate limits are counted, not quarantined.
        rejections=hits,
        errors=len(errors),
        throttled=hits > 0,
        invocations_without_result=no_result,
    )
    by_model = {}
    if model:
        by_model[model] = {
            "input": tokens["input"],
            "output": tokens["output"],
            "cache_write": tokens["cache_write"],
            "cache_read": tokens["cache_read"],
            "cost_usd": round(cost, 6),
        }
    return {
        "invocations": len(paths),
        # One step is one model request; the closest analogue of a Claude Code turn.
        "turns": steps_total,
        "tokens": tokens,
        "total_tokens": sum(tokens[k] for k in ("input", "output", "cache_write", "cache_read")),
        "cost_usd": round(cost, 6),
        # What opencode Zen metered — the same figure the Go plan's dollar caps
        # are charged against, so this is an invoice, not an estimate.
        "cost_basis": "gateway_metered",
        "by_model": by_model,
        "terminal_reasons": dict(terminal),
        "health": health,
        "per_invocation": per_invocation,
    }


# --------------------------------------------------------------------------- #
# cursor
# --------------------------------------------------------------------------- #
def summarize_cursor(harness_dir: Path, model: str | None) -> dict:
    ledger_path = harness_dir / "cursor-usage.json"
    transcripts = sorted(harness_dir.glob("cursor-*.jsonl"), key=invocation_order)
    tokens = empty_tokens()
    ledger = {}
    if ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text())
        except json.JSONDecodeError:
            ledger = {}

    billed = ledger.get("billed") or {}
    usage = billed.get("usage") or {}
    cost_block = billed.get("cost") or {}
    # `getUsage()` is entitlement-gated — an individual Pro account gets
    # `feature_unavailable`, and the event stream carries no usage either. When
    # there is no ledger the tokens are UNKNOWN, not zero: a dict of zeros would
    # read as a free run and drag any total or mean toward zero. Report None and
    # say why, so analysis drops the run instead of averaging a fiction.
    usage_available = bool(usage)
    if usage_available:
        tokens.update(
            input=usage.get("inputTokens", 0),
            output=usage.get("outputTokens", 0),
            thinking=usage.get("reasoningTokens", 0) or 0,
            cache_write=usage.get("cacheWriteTokens", 0),
            cache_read=usage.get("cacheReadTokens", 0),
        )
    # rawCostCents is the undiscounted model cost — the analogue of Claude Code's
    # list-price number, and the only one that stays non-zero under a plan.
    # chargedCents is what the account was actually billed (0 for plan-included).
    raw_cents = cost_block.get("rawCostCents")
    charged_cents = cost_block.get("chargedCents")
    invocations = ledger.get("invocations") or []

    health = blank_health()
    # The driver records each invocation's terminal error; that plus structured
    # error events is the whole failure signal. Never scan the transcript body —
    # it is full of tool output that trips any loose pattern.
    errors = [str(inv["error"]) for inv in invocations if inv.get("error")]
    errors += [
        json.dumps(event)
        for path in transcripts
        for event in iter_events(path)
        if event.get("type") == "error"
    ]
    hits = sum(1 for e in errors if RATE_LIMIT_RE.search(e))
    health.update(rate_limit_events=hits, rejections=hits, errors=len(errors), throttled=hits > 0)
    if not ledger_path.exists():
        # No ledger means getUsage() never landed: the totals below are empty and
        # must not be read as "this run was free".
        health["invocations_without_result"] = [p.name for p in transcripts]

    # Assistant messages are the turn analogue that survives a missing ledger;
    # the billed per-turn entries are preferred when they exist.
    assistant_turns = sum(
        1 for path in transcripts for event in iter_events(path) if event.get("type") == "assistant"
    )
    health["usage_available"] = usage_available
    if not usage_available:
        health["usage_error"] = ledger.get("usage_error") or "no billed usage reported"

    return {
        "invocations": len(transcripts),
        # Local agents report usage per turn, so the per-turn entries are the
        # turn count.
        "turns": len(billed.get("runs") or []) or assistant_turns,
        "tool_calls": sum(inv.get("tool_calls") or 0 for inv in invocations),
        "tokens": tokens if usage_available else None,
        "total_tokens": usage.get("totalTokens") if usage_available else None,
        "cost_usd": round(raw_cents / 100, 6) if raw_cents is not None else None,
        "charged_usd": round(charged_cents / 100, 6) if charged_cents is not None else None,
        "cost_basis": "cursor_raw_cost" if usage_available else "unavailable",
        "by_model": {
            (model or ledger.get("model") or "unknown"): {
                "input": tokens["input"],
                "output": tokens["output"],
                "cache_write": tokens["cache_write"],
                "cache_read": tokens["cache_read"],
                "cost_usd": round(raw_cents / 100, 6) if raw_cents is not None else None,
            }
        } if usage_available else {},
        "terminal_reasons": dict(Counter(
            "error" if inv.get("error") else "ok" for inv in invocations
        )),
        "health": health,
        "per_invocation": invocations,
        # The stream's own per-turn tallies, kept as a cross-check on the billed
        # totals (which are server-derived and eventually consistent).
        "streamed_tokens": ledger.get("streamed"),
    }


PARSERS = {
    "claude-code": lambda d, m: summarize_claude_code(d),
    "opencode": summarize_opencode,
    "cursor": summarize_cursor,
}


def run_metadata(harness_dir: Path) -> dict:
    """The run's metadata.json sits one level up from harness/."""
    path = harness_dir.parent / "metadata.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def summarize(harness_dir: Path, kind: str | None = None, model: str | None = None) -> dict:
    meta = run_metadata(harness_dir)
    # Default to claude-code so every run recorded before harnesses were
    # selectable parses exactly as it always did.
    kind = kind or meta.get("harness") or "claude-code"
    if kind not in PARSERS:
        raise SystemExit(f"usage: unknown harness kind {kind!r} (have {', '.join(sorted(PARSERS))})")
    result = PARSERS[kind](harness_dir, model or meta.get("model"))
    result["harness"] = kind
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", required=True, type=Path, help="the run's harness/ dir of transcripts")
    ap.add_argument("--kind", help="harness that produced them (default: metadata.json, else claude-code)")
    ap.add_argument("--model", help="model id, for the by_model breakdown (default: metadata.json)")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    if not args.harness.is_dir():
        raise SystemExit(f"usage: no such dir {args.harness}")
    result = summarize(args.harness, args.kind, args.model)
    out_text = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(out_text + "\n")
        t = result["tokens"]
        cost = result["cost_usd"]
        cost_str = f"${cost:.2f} ({result['cost_basis']})" if cost is not None else "cost unavailable"
        tok_str = (
            f"{t['output']:,} out / {t['cache_read']:,} cache-read"
            if t is not None
            else "tokens unavailable"
        )
        flag = "  THROTTLED" if result["health"]["throttled"] else ""
        print(f"usage[{result['harness']}]: {result['turns']} turns, {tok_str}, "
              f"{cost_str} -> {args.out}{flag}")
    else:
        print(out_text)


if __name__ == "__main__":
    main()
