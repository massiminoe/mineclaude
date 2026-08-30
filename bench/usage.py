#!/usr/bin/env python3
"""Fold a bench run's harness transcripts into one usage.json.

The token ledger of a run — what it cost to earn its advancements. Reads the
Claude Code stream-json transcripts (`harness/claude-*.jsonl`) the run already
collects and writes a compact summary next to score.json, so cost analysis
never has to re-download hundreds of MB of transcript.

Two things about the source format are load-bearing:

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

Health matters as much as the totals. A harness that got throttled scores low
for reasons that have nothing to do with the model, so `rate_limit_events` are
tallied and any hard rejection sets `health.throttled` — the flag analysis uses
to quarantine a trial instead of silently averaging it in.

`cost_usd` is what the CLI computed at list prices. Runs authenticated with a
subscription token (the bench default) are not billed per token, so treat it as
a list-price equivalent — hence `cost_basis`.

Usage:
    bench/usage.py --harness <run>/harness [--out <run>/usage.json]
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


def invocation_order(path: Path) -> int:
    m = re.search(r"-(\d+)\.jsonl$", path.name)
    return int(m.group(1)) if m else 0


def last_result(path: Path) -> dict | None:
    """The final `result` event in a transcript — the invocation's own totals."""
    found = None
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line or '"type"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
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


def summarize(harness_dir: Path) -> dict:
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", required=True, type=Path, help="the run's harness/ dir of claude-*.jsonl")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    if not args.harness.is_dir():
        raise SystemExit(f"usage: no such dir {args.harness}")
    result = summarize(args.harness)
    out_text = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(out_text + "\n")
        t = result["tokens"]
        flag = "  THROTTLED" if result["health"]["throttled"] else ""
        print(f"usage: {result['turns']} turns, {t['output']:,} out / {t['cache_read']:,} cache-read, "
              f"${result['cost_usd']:.2f} (list) -> {args.out}{flag}")
    else:
        print(out_text)


if __name__ == "__main__":
    main()
