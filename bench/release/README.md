# Release evidence audit

The audit inventories every available run but defines a conservative **candidate
cloud cohort**: one-hour runs with readable metadata, matching ledger/score IDs,
session timing, a usage-health record, no recorded throttling, and no fatal
harness log. It keeps local-only experiments, short runs, failures, and missing
artifacts visible. It never infers a model or budget from a run name.

`candidate` is not publication approval. Reasoning defaults may be unknown;
different commits and harnesses remain separate groups. Recorded source hashes,
installed harness versions, platform, difficulty, and tool-wait settings also
separate groups when those fields are available. The manifest records
those limitations rather than silently filling them in. Video presence means a
file exists, not that playback or full-run coverage has been verified.

## Refresh the evidence

Use the dedicated AWS profile. These commands read S3 and write ignored local
files; they do not start instances, change credentials, or upload anything.

```sh
export AWS_PROFILE=mineclaude-sso
aws sso login --profile mineclaude-sso
mkdir -p state/release-audit
BENCH_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BENCH_BUCKET="mineclaude-bench-${BENCH_ACCOUNT}"
aws s3api list-objects-v2 --bucket "$BENCH_BUCKET" --prefix runs/ \
  --output json > state/release-audit/s3-objects.json
aws s3 sync "s3://${BENCH_BUCKET}/runs/" state/release-audit/s3-runs/ \
  --exclude '*' --include '*/metadata.json' --include '*/score.json' \
  --include '*/usage.json' --include '*/advancements.json' \
  --include '*/harness/*version*' --include '*/harness/harness.log' \
  --include '*/sessions/*.jsonl' --only-show-errors
.venv/bin/python bench/inventory.py \
  --s3-inventory state/release-audit/s3-objects.json \
  --annotations bench/release/annotations.json \
  --out state/release-audit
```

AWS CLI pagination must remain enabled. The file listing is a point-in-time
inventory, not a checksum verification of all objects. Full download and
publication checks happen separately.

Outputs:

- `manifest.json` and `manifest.csv`: one row per run, status, raw and timed
  score, settings, artifact paths, locations, and issues. JSON retains source
  fingerprints and duplicate locations; CSV is the compact view.
- `advancement-events.json`: earned ledger entries with their first session
  receipt timestamp and exact offset from recorded `t0_epoch`.
- `candidate-cohorts.json`: per-group counts, mean, range, and exact run IDs.
  This uses the strict timed count, not the post-exit snapshot count.

The inventory gives cloud summary copies priority, fingerprints every available
summary copy, and requires review when metadata/score/ledger copies conflict.
Usage-only differences are retained as a warning because historical usage was
backfilled. Session files come from one source directory to avoid pooling copies.
Missing timestamps yield a null timed count. The raw artifacts are never edited.

## Historical Astra patch

The six `astra-low-*` cloud runs record base commit `21de4d9`, whose checked-in
model allowlist did not admit Astra. A retained local launcher at
`state/bench/astra-low-launch/user-data.sh.tpl` patches two files after checkout:
it admits `gpt-6-astra` and writes `model_reasoning_effort = "low"` into the
Codex configuration template.

`legacy-astra-low.patch` reconstructs those two edits against that base. Cloud
boot logs show the patch's Python invocation, but do not contain the resulting
configuration file. `annotations.json` labels low reasoning as reconstructed
launcher evidence, not a field present in the original metadata. Preserve this
qualification in the dataset card and article.

## Proposed selection policy

Keep the exact candidate run IDs with every figure. Do not pool different Git
commits or infer that equal model names imply equal reasoning settings. Review
the environment and shared prompt changes before combining groups.

For the showcase, choose the highest strict one-hour Astra count; break ties by
the earliest timestamp of the final counted advancement, then run ID. This
selects `astra-low-20260917-three-t3` (24 advancements, final at about 3238.1 s)
over `astra-low-20260917-three-t1` (24, final at about 3576.0 s). The recording is
`video/play-20260917-155455.mp4`. Playback and duration were checked during video preparation. The 43 commentary
captions use inferred timing bounds; they are not exact message timestamps.
Keep that timing qualification with the published recording.

The existing notebook predates this manifest and uses raw snapshot scores from
local sweep folders. Do not publish its cached figures as release results.
Regenerate publication figures from the reviewed manifest, keeping the cohort
definition and timing convention explicit.
