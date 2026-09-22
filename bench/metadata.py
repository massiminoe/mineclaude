#!/usr/bin/env python3
"""Record non-secret run settings and source hashes before launching a harness.

Only explicitly named settings and source paths are read. Never serialize the
environment, Codex home, auth files, or a full git diff into run artifacts.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

SOURCE_PATHS = (
    "minetrials", "mc-mod/src", "mc-mod/gradle.properties", "mc-mod/build.gradle.kts",
    "mc-mod/settings.gradle.kts", "mc-mod/gradle/wrapper/gradle-wrapper.properties",
    "mc-client/entrypoint.sh", "mc-client/entrypoint.arm64.sh",
    "mc-client/Dockerfile", "mc-client/Dockerfile.arm64", "mc-client/download-mods.sh",
    "skills/minetrials", "bench/harness", "bench/run.sh", "bench/metadata.py",
    "bench/validate-config.sh", "bench/compose.bench.yml", "bench/compose.codex.yml",
    "docker-compose.yml", "docker-compose.arm64.yml", "pyproject.toml", "requirements-dev.lock",
    "bench/score.py", "bench/usage.py", "bench/minetrials.Dockerfile",
    "mc-client/options.txt", "mc-client/baritone-settings.txt", "mc-server/ops.json",
)


def make_metadata(root, settings, t0, local, compose=None):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root)

    files = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *SOURCE_PATHS)
    hashes = {}
    for name in sorted(set(files.decode().split("\0")) - {""}):
        path = root / name
        if path.is_file():
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = {
        "schema_version": 2,
        "run_id": settings["RUN_ID"], "harness": settings["BENCH_HARNESS"],
        "model": settings["BENCH_MODEL"], "budget_seconds": int(settings["BENCH_RUN_SECONDS"]),
        "seed": settings["BENCH_SEED"], "git_sha": git("rev-parse", "HEAD").decode().strip(),
        "git_dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
        "t0_epoch": t0, "started_utc": datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "clock_basis": "runner_before_harness_container_start",
        "reasoning_effort": settings.get("BENCH_REASONING_EFFORT") or None,
        "harness_version_requested": settings.get("BENCH_HARNESS_VERSION") or "latest",
        "execute_wait_s": float(settings.get("BENCH_EXECUTE_WAIT_S") or 50),
        "record_fps": int(settings.get("RECORD_FPS") or 15),
        "difficulty": settings.get("BENCH_DIFFICULTY") or "normal",
        "client_platform": "linux/arm64" if local else "linux/amd64",
        "source_sha256": hashes,
    }
    if compose is not None:
        # Compose can read .env values absent from this process's environment.
        # Select only these non-secret fields; never save the resolved config.
        services = compose["services"]
        result.update({
            "execute_wait_s": float(services["minetrials"]["environment"]["MINETRIALS_EXECUTE_WAIT_S"]),
            "record_fps": int(services["mc-client"]["environment"]["RECORD_FPS"]),
            "difficulty": services["mc-server"]["environment"]["DIFFICULTY"],
            "client_platform": services["mc-client"]["platform"],
            "harness_version_requested": services["harness"]["build"]["args"]["BENCH_HARNESS_VERSION"],
        })
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--t0", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--compose-config-stdin", action="store_true")
    args = parser.parse_args()
    compose = json.load(sys.stdin) if args.compose_config_stdin else None
    args.out.write_text(json.dumps(make_metadata(Path.cwd(), os.environ, args.t0, args.local, compose), indent=2) + "\n")


if __name__ == "__main__":
    main()
