#!/usr/bin/env python3
"""Build a read-only release inventory from local and downloaded S3 artifacts.

Original scores are preserved. The derived timed count uses unrounded session
receipt timestamps and the runner's recorded t0, never the rounded score offsets.
Candidate means mechanically eligible for review, not a published leaderboard.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics

SUMMARY_FILES = ("metadata.json", "score.json", "usage.json", "advancements.json")
COHORT_FIELDS = ("harness", "model", "reasoning_effort", "budget_seconds", "seed", "git_sha",
                 "source_fingerprint", "client_platform", "difficulty", "execute_wait_s",
                 "harness_version_signature")


def read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, UnicodeError):
        return None


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def session_times(paths: list[Path]) -> tuple[dict[str, float], int]:
    times = {}
    malformed = 0
    for path in paths:
        with path.open() as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    malformed += 1
                    continue
                if not isinstance(entry, dict):
                    malformed += 1
                    continue
                data = entry.get("data") or {}
                if entry.get("event") != "event" or data.get("type") != "advancement":
                    continue
                adv_id = (data.get("data") or {}).get("id")
                ts = entry.get("ts")
                if adv_id and isinstance(ts, (float, int)):
                    times[adv_id] = min(times.get(adv_id, ts), ts)
    return times, malformed


def build_inventory(local_root: Path, cloud_root: Path, objects: list[dict],
                    annotations: dict) -> tuple[list[dict], list[dict]]:
    remote = defaultdict(dict)
    for obj in objects:
        parts = obj["Key"].split("/", 2)
        if len(parts) == 3 and parts[0] == "runs" and parts[2]:
            remote[parts[1]][parts[2]] = obj["Size"]

    copies = defaultdict(list)
    for root, source in ((cloud_root, "s3"), (local_root, "local")):
        directories = {p.parent for name in SUMMARY_FILES for p in root.rglob(name)}
        for directory in sorted(directories):
            metadata = read_json(directory / "metadata.json") or {}
            run_id = metadata.get("run_id")
            if not run_id:
                name = directory.name.removesuffix("-remote")
                run_id = name if name in remote else "unresolved:" + directory.name
            copies[run_id].append((source, directory))
    for run_id in remote:
        copies.setdefault(run_id, [])

    rows, events = [], []
    for run_id, locations in sorted(copies.items()):
        # Prefer the cloud copy; keep fingerprints of every conflicting local copy.
        locations.sort(key=lambda pair: (pair[0] != "s3", str(pair[1])))
        issues, sources, documents = [], {}, {}
        for name in SUMMARY_FILES:
            found = [(directory / name, read_json(directory / name))
                     for _, directory in locations if (directory / name).exists()]
            valid = [(path, doc) for path, doc in found if isinstance(doc, dict)]
            if len(valid) != len(found):
                issues.append("invalid_json:" + name)
            fingerprints = {json.dumps(doc, sort_keys=True) for _, doc in valid}
            if len(fingerprints) > 1:
                issues.append("conflicting_copies:" + name)
            documents[name] = valid[0][1] if valid else {}
            sources[name] = [{"path": str(path), "sha256": digest(path)} for path, _ in found]

        metadata, score, usage, ledger = (documents[name] for name in SUMMARY_FILES)
        if not metadata:
            issues.append("missing_metadata")
        if not score:
            issues.append("missing_score")
        if not usage:
            issues.append("missing_usage")
        if not ledger:
            issues.append("missing_ledger")
        earned = ledger.get("earned", (ledger.get("data") or {}).get("earned"))
        breakdown = score.get("breakdown", [])
        ids = [adv.get("id") for adv in earned] if isinstance(earned, list) else None
        if ids is not None and (len(ids) != score.get("earned_count") or
                                set(ids) != {adv.get("id") for adv in breakdown} or
                                len(ids) != len(set(ids))):
            issues.append("ledger_score_mismatch")

        # Select one complete source directory, avoiding double-counting copies.
        sessions = next((sorted(directory.glob("sessions/*.jsonl")) for _, directory in locations
                         if list(directory.glob("sessions/*.jsonl"))), [])
        times, malformed = session_times(sessions)
        sources["sessions"] = [{"path": str(p), "sha256": digest(p)} for p in sessions]
        if not sessions:
            issues.append("missing_sessions")
        if malformed:
            issues.append("malformed_session_lines")
        budget, t0 = metadata.get("budget_seconds"), metadata.get("t0_epoch")
        timed_count, late, early, unknown = None, [], [], []
        if ids is not None and isinstance(budget, (int, float)) and isinstance(t0, (int, float)):
            count = 0
            for adv in earned:
                adv_id = adv.get("id")
                ts = times.get(adv_id)
                offset = ts - t0 if ts is not None else None
                if offset is None:
                    unknown.append(adv_id)
                elif offset < 0:
                    early.append(adv_id)
                elif offset > budget:
                    late.append(adv_id)
                else:
                    count += 1
                events.append({"run_id": run_id, "id": adv_id, "title": adv.get("title"),
                               "ts": ts, "offset_s": offset,
                               "within_budget": None if offset is None else 0 <= offset <= budget})
            timed_count = count if not unknown and sessions else None
        if unknown:
            issues.append("missing_advancement_timestamps")
        if early:
            issues.append("pre_start_advancements")
        if late:
            issues.append("post_budget_advancements")
        for entry in breakdown:
            ts = times.get(entry.get("id"))
            if ts is not None and entry.get("ts") != ts:
                issues.append("score_session_timestamp_mismatch")
                break

        health = usage.get("health") or {}
        throttled = health.get("throttled")
        if throttled is True:
            issues.append("throttled")
        elif throttled is not False:
            issues.append("unknown_throttle_health")
        logs = next((directory / "harness/harness.log" for _, directory in locations
                     if (directory / "harness/harness.log").exists()), None)
        if logs and "FATAL:" in logs.read_text(errors="replace"):
            issues.append("harness_fatal")
        if not logs:
            issues.append("missing_harness_log")
        versions = {}
        for _, directory in reversed(locations):
            for path in sorted(directory.glob("harness/*version*.txt")):
                versions[path.name] = path.read_text().strip()
        note = annotations.get(run_id, {})
        reasoning = metadata.get("reasoning_effort") or note.get("reasoning_effort")
        if not reasoning:
            issues.append("reasoning_not_recorded")
        if not versions:
            issues.append("harness_version_not_recorded")
        local_files = {str(path.relative_to(directory)): path.stat().st_size
                       for _, directory in locations for path in directory.rglob("*") if path.is_file()}
        remote_files = remote.get(run_id, {})
        available = remote_files.keys() | local_files.keys()
        video_files = sorted(name for name in available if name.endswith(".mp4"))
        if not video_files:
            issues.append("missing_video")
        reasons = []
        if run_id not in remote:
            reasons.append("outside_cloud_cohort")
        if isinstance(budget, (int, float)) and budget != 3600:
            reasons.append("not_one_hour")
        for problem in ("throttled", "harness_fatal"):
            if problem in issues:
                reasons.append(problem)
        if note.get("exclude_reason"):
            reasons.append(note["exclude_reason"])
        nonblocking = {"reasoning_not_recorded", "harness_version_not_recorded", "missing_video",
                       "post_budget_advancements", "conflicting_copies:usage.json"}
        blocking = [issue for issue in issues if issue not in nonblocking]
        status = "excluded" if reasons else "review" if blocking else "candidate"
        row = {
            "run_id": run_id, "status": status, "exclusion_reasons": reasons,
            "harness": metadata.get("harness"), "model": metadata.get("model"),
            "reasoning_effort": reasoning,
            "reasoning_source": "metadata.json" if metadata.get("reasoning_effort") else note.get("reasoning_source"),
            "budget_seconds": budget, "seed": metadata.get("seed"), "git_sha": metadata.get("git_sha"),
            "git_dirty": metadata.get("git_dirty"),
            "source_fingerprint": hashlib.sha256(json.dumps(metadata["source_sha256"], sort_keys=True).encode()).hexdigest()
                if metadata.get("source_sha256") else note.get("source_patch_sha256"),
            "client_platform": metadata.get("client_platform"), "difficulty": metadata.get("difficulty"),
            "execute_wait_s": metadata.get("execute_wait_s"),
            "started_utc": metadata.get("started_utc"), "harness_versions": versions,
            "harness_version_signature": json.dumps(versions, sort_keys=True),
            "raw_earned_count": score.get("earned_count"), "timed_earned_count": timed_count,
            "throttled": throttled, "cost_usd": usage.get("cost_usd"), "cost_basis": usage.get("cost_basis"),
            "post_budget_ids": late, "pre_start_ids": early, "unknown_time_ids": unknown,
            "issues": sorted(set(issues)), "notes": note.get("notes"),
            "on_s3": run_id in remote, "s3_object_count": len(remote_files),
            "s3_bytes": sum(remote_files.values()), "video_files": video_files,
            "available_artifact_paths": sorted(available),
            "locations": [str(directory) for _, directory in locations], "sources": sources,
        }
        rows.append(row)
    return rows, events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-root", type=Path, default=Path("state/bench"))
    parser.add_argument("--cloud-root", type=Path, default=Path("state/release-audit/s3-runs"))
    parser.add_argument("--s3-inventory", type=Path)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    objects = (read_json(args.s3_inventory) or {}).get("Contents", []) if args.s3_inventory else []
    annotations = (read_json(args.annotations) or {}) if args.annotations else {}
    rows, events = build_inventory(args.local_root, args.cloud_root, objects, annotations)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "manifest.json").write_text(json.dumps(rows, indent=2) + "\n")
    (args.out / "advancement-events.json").write_text(json.dumps(events, indent=2) + "\n")
    fields = [key for key in rows[0] if key not in {"sources", "locations", "available_artifact_paths"}] if rows else ["run_id"]
    with (args.out / "manifest.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in row.items()})
    groups = defaultdict(list)
    for row in rows:
        if row["status"] == "candidate" and row["timed_earned_count"] is not None:
            key = tuple(row[k] for k in COHORT_FIELDS)
            groups[key].append(row)
    cohorts = []
    for key, group in sorted(groups.items(), key=lambda item: str(item[0])):
        counts = [r["timed_earned_count"] for r in group]
        cohorts.append(dict(zip(COHORT_FIELDS, key)) |
                       {"n": len(group), "mean": statistics.mean(counts), "min": min(counts), "max": max(counts),
                        "run_ids": [r["run_id"] for r in group], "counts": counts})
    (args.out / "candidate-cohorts.json").write_text(json.dumps(cohorts, indent=2) + "\n")
    print(f"Inventoried {len(rows)} unique run records; {len(cohorts)} candidate cohorts -> {args.out}")


if __name__ == "__main__":
    main()
