"""Release counts must not silently pool copies, failures, or late events."""
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("inventory", Path(__file__).resolve().parents[1] / "bench/inventory.py")
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


def write_run(root, offsets=(10.0, 3600.04)):
    run = root / "trial"
    (run / "sessions").mkdir(parents=True)
    (run / "harness").mkdir()
    earned = [{"id": f"adv-{i}", "title": f"Advancement {i}"} for i in range(len(offsets))]
    docs = {
        "metadata.json": {"run_id": "trial", "harness": "codex", "model": "gpt-6-astra",
                          "budget_seconds": 3600, "t0_epoch": 100, "seed": "seed", "git_sha": "sha"},
        "score.json": {"earned_count": len(earned), "breakdown": [
            dict(adv, ts=100 + offset, offset_s=round(offset, 1)) for adv, offset in zip(earned, offsets)]},
        "advancements.json": {"earned": earned}, "usage.json": {"health": {"throttled": False}},
    }
    for name, data in docs.items():
        (run / name).write_text(json.dumps(data))
    (run / "sessions/events.jsonl").write_text("".join(json.dumps({
        "event": "event", "ts": 100 + offset,
        "data": {"type": "advancement", "data": adv},
    }) + "\n" for adv, offset in zip(earned, offsets)))
    (run / "harness/harness.log").write_text("budget exhausted\n")
    return run


def audit(tmp_path):
    return inventory.build_inventory(tmp_path / "local", tmp_path / "cloud",
                                     [{"Key": "runs/trial/score.json", "Size": 10}], {})


def test_deduplicates_and_uses_unrounded_session_time(tmp_path):
    write_run(tmp_path / "local")
    write_run(tmp_path / "cloud")
    rows, events = audit(tmp_path)
    assert len(rows) == 1
    assert rows[0]["raw_earned_count"] == 2
    assert rows[0]["timed_earned_count"] == 1
    assert rows[0]["post_budget_ids"] == ["adv-1"]
    assert rows[0]["status"] == "candidate"
    assert len(events) == 2


def test_conflicting_copy_requires_review(tmp_path):
    write_run(tmp_path / "local", (10,))
    write_run(tmp_path / "cloud", (10, 20))
    rows, _ = audit(tmp_path)
    assert rows[0]["raw_earned_count"] == 2  # cloud preferred, conflict retained
    assert rows[0]["status"] == "review"
    assert "conflicting_copies:score.json" in rows[0]["issues"]


def test_missing_timestamp_is_unknown_not_zero(tmp_path):
    run = write_run(tmp_path / "cloud", (10,))
    (run / "sessions/events.jsonl").write_text("")
    rows, _ = audit(tmp_path)
    assert rows[0]["timed_earned_count"] is None
    assert rows[0]["status"] == "review"


def test_harness_failure_excluded_even_when_not_throttled(tmp_path):
    run = write_run(tmp_path / "cloud", ())
    (run / "harness/harness.log").write_text("FATAL: failed authentication\n")
    rows, _ = audit(tmp_path)
    assert rows[0]["status"] == "excluded"
    assert rows[0]["throttled"] is False
    assert "harness_fatal" in rows[0]["exclusion_reasons"]


def test_cloud_run_without_metadata_is_unresolved(tmp_path):
    rows, _ = audit(tmp_path)
    assert rows[0]["status"] == "review"
    assert "missing_metadata" in rows[0]["issues"]
    assert "not_one_hour" not in rows[0]["exclusion_reasons"]


def test_zero_and_deadline_boundaries_are_inclusive(tmp_path):
    write_run(tmp_path / "cloud", (-0.1, 0, 3600, 3600.01))
    rows, _ = audit(tmp_path)
    assert rows[0]["timed_earned_count"] == 2
    assert rows[0]["pre_start_ids"] == ["adv-0"]
    assert rows[0]["status"] == "review"
