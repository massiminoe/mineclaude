#!/usr/bin/env python3
"""Compute a bench run's score from the advancement ledger.

The headline metric is the **count** of advancements earned in the budget —
every advancement counts 1. The weighted gamerscore is currently DISABLED but
kept intact behind --gamerscore, so a run can be re-scored either way from the
same artifacts (score.py can be re-run offline against advancements.json).

Inputs:
  --advancements  JSON snapshot from the bridge's GET /advancements (ground
                  truth for WHAT was earned)
  --scoring       bench/scoring/gamerscore.json (id -> points); only consulted
                  when --gamerscore is passed
  --gamerscore    opt back in to weighted points (off by default)
  --sessions      optional dir of session-log JSONL files; supplies WHEN each
                  advancement landed (Runtime receipt timestamps)
  --t0            optional epoch seconds of harness start; earned offsets are
                  reported relative to it

Output (--out, default stdout): score.json with the earned count, a
per-advancement breakdown ordered by time, and (when --gamerscore is on) the
weighted total plus any earned ids missing from the scoring table (scored 0,
surfaced so the table can be fixed rather than silently dropping points).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_earned(path: Path) -> list[dict]:
    doc = json.loads(path.read_text())
    for candidate in (doc, doc.get("data") if isinstance(doc, dict) else None):
        if isinstance(candidate, dict) and isinstance(candidate.get("earned"), list):
            return candidate["earned"]
    raise SystemExit(f"score: no 'earned' list found in {path}")


def load_timestamps(sessions_dir: Path) -> dict[str, float]:
    """First receipt timestamp per advancement id, from session-log JSONL."""
    first_ts: dict[str, float] = {}
    for jsonl in sorted(sessions_dir.glob("*.jsonl")):
        for line in jsonl.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            data = entry.get("data") or {}
            if entry.get("event") != "event" or data.get("type") != "advancement":
                continue
            inner = data.get("data") or {}
            adv_id = inner.get("id")
            ts = entry.get("ts")
            if adv_id and isinstance(ts, (int, float)) and adv_id not in first_ts:
                first_ts[adv_id] = float(ts)
    return first_ts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--advancements", required=True, type=Path)
    ap.add_argument("--scoring", type=Path)
    ap.add_argument("--gamerscore", action="store_true", help="also compute weighted points (disabled by default)")
    ap.add_argument("--sessions", type=Path)
    ap.add_argument("--t0", type=float)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    if args.gamerscore and not args.scoring:
        raise SystemExit("score: --gamerscore requires --scoring")
    scoring_doc = json.loads(args.scoring.read_text()) if (args.gamerscore and args.scoring) else None
    table = scoring_doc["advancements"] if scoring_doc else {}
    earned = load_earned(args.advancements)
    timestamps = load_timestamps(args.sessions) if args.sessions and args.sessions.is_dir() else {}

    breakdown, unscored, total = [], [], 0
    for adv in earned:
        adv_id = adv.get("id", "")
        row = table.get(adv_id)
        points = row["points"] if row else 0
        if scoring_doc and row is None:
            unscored.append(adv_id)
        total += points
        ts = timestamps.get(adv_id)
        entry = {
            "id": adv_id,
            "title": adv.get("title") or (row or {}).get("title"),
            "ts": ts,
            "offset_s": round(ts - args.t0, 1) if ts and args.t0 else None,
        }
        if scoring_doc:
            entry["points"] = points
        breakdown.append(entry)
    # Chronological where known; untimestamped entries sink to the end.
    breakdown.sort(key=lambda e: (e["ts"] is None, e["ts"] or 0))

    result = {
        "earned_count": len(earned),
        "breakdown": breakdown,
    }
    if scoring_doc:
        result["total_points"] = total
        result["max_points"] = scoring_doc.get("total_points")
        result["unscored_ids"] = unscored
    out_text = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(out_text + "\n")
    summary = f"score: {len(earned)} advancements" + (f" ({total} points)" if scoring_doc else "")
    print(out_text if not args.out else f"{summary} -> {args.out}")
    if unscored:
        print(f"score: WARNING {len(unscored)} earned ids missing from table: {unscored}", file=sys.stderr)


if __name__ == "__main__":
    main()
