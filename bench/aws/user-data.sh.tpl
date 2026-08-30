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

# Harness credential: each harness authenticates differently, so pull only the
# one this run needs. A missing parameter is fatal — bench/run.sh would refuse
# to start the agent anyway, and failing here keeps the reason in the boot log.
HARNESS="__HARNESS__"
case "$HARNESS" in
    claude-code) SSM_PARAM=/mineclaude-bench/claude-code-oauth-token; CRED_VAR=CLAUDE_CODE_OAUTH_TOKEN ;;
    opencode)    SSM_PARAM=/mineclaude-bench/opencode-api-key;        CRED_VAR=OPENCODE_API_KEY ;;
    cursor)      SSM_PARAM=/mineclaude-bench/cursor-api-key;          CRED_VAR=CURSOR_API_KEY ;;
    *) echo "unknown harness $HARNESS"; shutdown -h now "bad harness" ;;
esac
CRED=$(aws ssm get-parameter --region __REGION__ --name "$SSM_PARAM" \
    --with-decryption --query Parameter.Value --output text)
export "$CRED_VAR=$CRED"

RUN_ID="__RUN_ID__"

# Gameplay recorder capture rate — compose reads it from this env.
export RECORD_FPS="__RECORD_FPS__"

# Perf probe: a 15s sample of VM load, per-container CPU, and the recorder
# ffmpeg's own share. A score is only meaningful if the VM wasn't starved, and
# this is the only place that's observable after the instance is gone.
(
    set +x  # this whole script runs under `set -x`; without this the log is 3x trace noise
    while :; do
        stats=$(docker stats --no-stream --format '{{.Name}}={{.CPUPerc}}' 2>/dev/null | tr '\n' ' ')
        rec=$(ps -eo pcpu,args --no-headers 2>/dev/null | grep '[x]11grab' | grep /recordings | awk '{print $1}' | tr '\n' ',')
        echo "$(date -u +%H:%M:%S) load=$(cut -d' ' -f1-3 /proc/loadavg) rec_ffmpeg_cpu=${rec:-none} $stats"
        sleep 15
    done
) > /var/log/bench-perf.log 2>&1 &
PERF_PID=$!

bench/run.sh \
    --seconds __RUN_SECONDS__ \
    --harness "$HARNESS" \
    --model "__MODEL__" \
    --seed "__SEED__" \
    --run-id "$RUN_ID" \
    || echo "bench run exited nonzero — uploading what we have"

kill "$PERF_PID" 2>/dev/null || true
cp /var/log/bench-userdata.log "state/bench/$RUN_ID/" || true
cp /var/log/bench-perf.log "state/bench/$RUN_ID/" || true
aws s3 cp --only-show-errors --recursive "state/bench/$RUN_ID" \
    "s3://__BUCKET__/runs/$RUN_ID/" --region __REGION__

shutdown -h now "bench complete"
