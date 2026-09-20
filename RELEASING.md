# Release preparation

The first release consists of a versioned code checkout, a benchmark results
article, and a separately versioned dataset with gameplay video. Raw runs,
credentials, and downloaded audit evidence remain under ignored `state/` paths.

## Evidence and result selection

Use [the release inventory workflow](bench/release/README.md) to regenerate the
run manifest, event data, and candidate groups. Every public figure must name its
metric, time budget, exact run IDs, model/harness settings, and exclusions.
Keep failures in the dataset even when held out of performance means.

Historical final-ledger scores can exceed the advertised deadline. Publish the
separately derived in-budget score with its clock convention, retaining the raw
score. Source commits do not capture the historical Astra patch; preserve the
reconstructed patch and its provenance qualification.

Before freezing the headline:

- Review the 61 candidate cloud runs and the unresolved cloud directory in the
  September 20 audit; refresh these counts if more runs are added.
- Confirm whether to combine the two Codex Luna source-commit groups. Keep them
  separate until that decision is documented. Shared runtime/bridge/skill source
  is unchanged across the main historical groups, but harness setup differs.
- Regenerate publication figures from the reviewed manifest, not cached notebook
  outputs. Keep sample counts and individual trials visible.
- Verify the selected Astra recording's playback and timeline against its events.

## Repository checks

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -c requirements-dev.lock -e '.[dev]'
make test
cd frontend && npm ci && cd ..
make frontend-build
make skill-docs
git diff --exit-code -- skills/mineclaude
cd mc-mod && ./gradlew --no-daemon build && cd ..
```

The bridge build needs Java 21 when run on the host. Docker builds provide it.
CI covers Python tests, generated skill documentation, shell syntax, the
frontend production build, and the bridge build. GitHub CI results are only
available after the branch is pushed.

Run `make test-e2e` for a real Minecraft smoke test. It uses an isolated Compose
project and random local bridge ports, crafts four planks over MCP, checks the
monitor inventory, and captures a screenshot. No model credentials are needed.
Logs are retained under `state/e2e/`; only that test session's containers and
volumes are removed. This is separate from unit tests and mock startup.

## Remaining publication decisions

- Original code uses MIT, with the root `LICENSE` included in the Python package
  and bridge JAR. Keep third-party asset and dataset terms separate.
- Dependency notices and asset scope are documented in [THIRD_PARTY.md](THIRD_PARTY.md).
  The unverified skin is removed and icons are generated locally. Choose dataset
  terms separately, including the historical recordings' third-party content.
- Resolved base-image digests, current registry harness versions, installed Python
  dependencies, frontend lock metadata, and historical harness version logs are
  captured in `bench/release/environment-2026-09-20.json`. Refresh it with
  `bench/release/capture_environment.py --out <path>` using the release Python
  environment (optionally `--history-root state/release-audit/s3-runs`).
  Docker/harness defaults still float; this observation does not reconstruct old images.
- Record a passing real Minecraft smoke test for the release checkout. Unit
  tests and mock startup do not replace that test.
- Review the release branch and require green GitHub CI and smoke checks
  before merging or tagging.

## Artifact publication

Inventory bytes and files before the full download; use resumable transfers and
verify checksums/completeness. Preserve originals and prepare a public copy with
documented redactions. The repository-history pattern scan does not cover ignored
run transcripts, boot logs, videos, or credential directories.

Publish a small run/event index alongside the larger per-run artifacts on Hugging
Face. Link every article claim and the selected YouTube video to stable run IDs
and a fixed dataset revision. Stage the code release, article, dataset, and video,
verify the links and download examples, then publish together.
