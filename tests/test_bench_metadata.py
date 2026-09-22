"""Configuration provenance without copying environment credentials."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("metadata", ROOT / "bench/metadata.py")
metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata)


def test_metadata_records_edits_and_omits_secrets(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "bench/harness").mkdir(parents=True)
    source = tmp_path / "bench/harness/prompt.md"
    source.write_text("original")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    settings = {"RUN_ID": "trial", "BENCH_HARNESS": "codex", "BENCH_MODEL": "gpt-6-astra",
                "BENCH_RUN_SECONDS": "3600", "BENCH_SEED": "seed", "BENCH_REASONING_EFFORT": "low",
                "OPENAI_API_KEY": "private-test-value"}
    first = metadata.make_metadata(tmp_path, settings, 100, False)
    assert first["git_dirty"] is False
    source.write_text("changed setting")
    (tmp_path / "auth.json").write_text("another-private-value")
    second = metadata.make_metadata(tmp_path, settings, 100, False)
    assert second["git_dirty"] is True
    assert first["source_sha256"] != second["source_sha256"]
    assert second["reasoning_effort"] == "low"
    assert "private" not in json.dumps(second)
    assert "auth.json" not in second["source_sha256"]
    del settings["BENCH_REASONING_EFFORT"]
    assert metadata.make_metadata(tmp_path, settings, 100, False)["reasoning_effort"] is None
    resolved = {"services": {
        "minetrials": {"environment": {"MINETRIALS_EXECUTE_WAIT_S": "40"}},
        "mc-client": {"environment": {"RECORD_FPS": "5"}, "platform": "linux/arm64"},
        "mc-server": {"environment": {"DIFFICULTY": "hard", "RCON_PASSWORD": "secret-value"}},
        "harness": {"build": {"args": {"BENCH_HARNESS_VERSION": "0.153.4"}},
                    "environment": {"ANTHROPIC_API_KEY": "secret-value"}},
    }}
    effective = metadata.make_metadata(tmp_path, settings, 100, False, resolved)
    assert effective["record_fps"] == 5
    assert effective["difficulty"] == "hard"
    assert effective["client_platform"] == "linux/arm64"
    assert "secret-value" not in json.dumps(effective)


@pytest.mark.parametrize("harness,model,effort,code", [
    ("codex", "gpt-6-astra", "low", 0),
    ("codex", "gpt-5.6-sol", "", 0),
    ("codex", "gpt-6-astra", 'low"\nunsafe', 2),
    ("claude-code", "claude-sonnet-5", "low", 2),
    ("codex", "unknown-model", "low", 2),
])
def test_config_validation(harness, model, effort, code):
    result = subprocess.run(["bash", "-c", 'source "$1"', "validate", str(ROOT / "bench/validate-config.sh")],
                            env={**os.environ, "HARNESS": harness, "MODEL": model, "REASONING_EFFORT": effort},
                            capture_output=True)
    assert result.returncode == code
