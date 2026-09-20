#!/usr/bin/env python3
"""Capture named dependency versions only; never dump env or Docker config.

Run with the release Python environment after `npm ci` in frontend. Resolves
public registry metadata; does not build harnesses or make model calls.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
IMAGES = ["node:22-slim", "python:3.13-slim", "eclipse-temurin:21-jdk",
          "3arthqu4ke/headlessmc:latest", "itzg/minecraft-server:latest"]
HARNESSES = ["@anthropic-ai/claude-code", "@cursor/sdk", "opencode-ai", "@openai/codex"]


def output(*args):
    return subprocess.check_output(args, text=True, cwd=ROOT, timeout=90).strip()


def capture(history_root=None):
    images = {}
    for image in IMAGES:
        manifest = json.loads(output("docker", "buildx", "imagetools", "inspect", image,
                                     "--format", "{{json .Manifest}}"))
        images[image] = {"digest": manifest["digest"], "platforms": [
            {"platform": m.get("platform"), "digest": m["digest"]}
            for m in manifest.get("manifests", [])
            if m.get("platform", {}).get("architecture") in {"amd64", "arm64"}
        ]}
    python = []
    for dist in importlib.metadata.distributions():
        if dist.metadata["Name"] == "mineclaude":
            continue
        python.append({"name": dist.metadata["Name"], "version": dist.version,
                       "license_expression": dist.metadata.get("License-Expression"),
                       "license_classifiers": [c for c in dist.metadata.get_all("Classifier", [])
                                               if c.startswith("License ::")]})
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text())
    frontend = [{"path": path, "version": info.get("version"), "license": info.get("license")}
                for path, info in lock["packages"].items() if path]
    historical = defaultdict(lambda: defaultdict(list))
    if history_root:
        for path in sorted(history_root.glob("*/harness/*-version.txt")):
            historical[path.name][path.read_text().strip()].append(path.parents[1].name)
    return {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Registry resolutions today, installed Python environment, frontend lock, and optional historical version logs. Not historical image reconstruction or validation of current harness versions.",
        "images": images,
        "registry_latest_harness_versions": {name: output("npm", "view", name, "version") for name in HARNESSES},
        "historical_harness_version_logs": historical,
        "python": sorted(python, key=lambda d: d["name"].lower()),
        "frontend": frontend,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.write_text(json.dumps(capture(args.history_root), indent=2) + "\n")
