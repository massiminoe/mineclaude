#!/bin/bash
# Cloud-init user-data for one ephemeral bench VM. Placeholders (__NAME__) are
# substituted by launch.sh. The instance is launched with
# --instance-initiated-shutdown-behavior terminate, so the final `shutdown`
# (and the deadman fallback) destroy the VM — nothing survives but the S3 upload.
set -uxo pipefail
exec > /var/log/bench-userdata.log 2>&1

# Deadman switch: even if everything below wedges, the VM self-terminates.
shutdown -h +__MAX_MINUTES__ "bench deadman" || true

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq git curl unzip python3
curl -fsSL https://get.docker.com | sh
curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscli.zip
unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install

git clone https://github.com/massiminoe/mineclaude.git /opt/mineclaude
cd /opt/mineclaude
git checkout --quiet __GIT_REF__

export CLAUDE_CODE_OAUTH_TOKEN=$(aws ssm get-parameter --region __REGION__ \
    --name /mineclaude-bench/claude-code-oauth-token \
    --with-decryption --query Parameter.Value --output text)

RUN_ID="__RUN_ID__"
bench/run.sh \
    --seconds __RUN_SECONDS__ \
    --model "__MODEL__" \
    --seed "__SEED__" \
    --run-id "$RUN_ID" \
    || echo "bench run exited nonzero — uploading what we have"

cp /var/log/bench-userdata.log "state/bench/$RUN_ID/" || true
aws s3 cp --only-show-errors --recursive "state/bench/$RUN_ID" \
    "s3://__BUCKET__/runs/$RUN_ID/" --region __REGION__

shutdown -h now "bench complete"
