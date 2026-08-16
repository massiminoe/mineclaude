#!/usr/bin/env bash
# Launch ONE ephemeral bench VM on EC2, wait for its score, pull the results.
#
#   bench/aws/launch.sh [--seconds 1800] [--model <id>] [--run-id <id>]
#                       [--seed <s>] [--type c7i.2xlarge] [--spot]
#                       [--git-ref <sha|branch>] [--record-fps 5] [--no-wait]
#
# The VM clones the repo at --git-ref (default: current HEAD — push first!),
# runs bench/run.sh, uploads state/bench/<run-id>/ to S3, and self-terminates.
# Requires bench/aws/setup.sh to have been run once.
set -euo pipefail
cd "$(dirname "$0")/../.."

REGION="${AWS_REGION:-us-east-1}"
SECONDS_BUDGET=1800
MODEL="claude-haiku-4-5-20251001"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
SEED="mineclaude-bench-1"
ITYPE="c7i.2xlarge"
SPOT=0
GIT_REF="$(git rev-parse HEAD)"
RECORD_FPS=5
WAIT=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --seconds) SECONDS_BUDGET="$2"; shift 2 ;;
        --model)   MODEL="$2"; shift 2 ;;
        --run-id)  RUN_ID="$2"; shift 2 ;;
        --seed)    SEED="$2"; shift 2 ;;
        --type)    ITYPE="$2"; shift 2 ;;
        --git-ref) GIT_REF="$2"; shift 2 ;;
        --record-fps) RECORD_FPS="$2"; shift 2 ;;
        --spot)    SPOT=1; shift ;;
        --no-wait) WAIT=0; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="mineclaude-bench-${ACCOUNT}"

if [[ -z "$(git branch -r --contains "$GIT_REF" 2>/dev/null)" ]]; then
    echo "WARN: $GIT_REF is not on any remote branch — the VM clones from GitHub. Push first." >&2
fi

# World gen + client join can eat ~10 min before the budget even starts.
MAX_MINUTES=$(( SECONDS_BUDGET / 60 + 45 ))

AMI=$(aws ssm get-parameter --region "$REGION" \
    --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
    --query Parameter.Value --output text)
SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
    --filters Name=group-name,Values=mineclaude-bench \
    --query 'SecurityGroups[0].GroupId' --output text)

UD=$(mktemp)
sed -e "s|__REGION__|$REGION|g" \
    -e "s|__BUCKET__|$BUCKET|g" \
    -e "s|__RUN_ID__|$RUN_ID|g" \
    -e "s|__RUN_SECONDS__|$SECONDS_BUDGET|g" \
    -e "s|__MODEL__|$MODEL|g" \
    -e "s|__SEED__|$SEED|g" \
    -e "s|__GIT_REF__|$GIT_REF|g" \
    -e "s|__RECORD_FPS__|$RECORD_FPS|g" \
    -e "s|__MAX_MINUTES__|$MAX_MINUTES|g" \
    bench/aws/user-data.sh.tpl > "$UD"

MARKET_ARGS=()
if [[ $SPOT -eq 1 ]]; then
    MARKET_ARGS=(--instance-market-options 'MarketType=spot,SpotOptions={SpotInstanceType=one-time,InstanceInterruptionBehavior=terminate}')
fi

IID=$(aws ec2 run-instances --region "$REGION" \
    --image-id "$AMI" \
    --instance-type "$ITYPE" \
    --key-name mineclaude-bench \
    --security-group-ids "$SG_ID" \
    --iam-instance-profile Name=mineclaude-bench-ec2 \
    --instance-initiated-shutdown-behavior terminate \
    --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=60,VolumeType=gp3,DeleteOnTermination=true}' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=mineclaude-bench-${RUN_ID}},{Key=bench-run,Value=${RUN_ID}}]" \
    --user-data "file://$UD" \
    ${MARKET_ARGS[@]+"${MARKET_ARGS[@]}"} \
    --query 'Instances[0].InstanceId' --output text)
rm -f "$UD"

echo "launched $IID ($ITYPE$( [[ $SPOT -eq 1 ]] && echo ', spot')) run=$RUN_ID model=$MODEL budget=${SECONDS_BUDGET}s"
echo "  ssh: ssh -i ~/.ssh/mineclaude-bench.pem ubuntu@\$(aws ec2 describe-instances --region $REGION --instance-ids $IID --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
echo "  s3:  s3://$BUCKET/runs/$RUN_ID/"

[[ $WAIT -eq 0 ]] && exit 0

echo "waiting for score.json (checks every 60s, up to ${MAX_MINUTES}m)..."
DEST="state/bench/${RUN_ID}-remote"
for (( i = 0; i < MAX_MINUTES + 10; i++ )); do
    sleep 60
    if aws s3 ls "s3://$BUCKET/runs/$RUN_ID/score.json" --region "$REGION" >/dev/null 2>&1; then
        mkdir -p "$DEST"
        aws s3 cp --only-show-errors "s3://$BUCKET/runs/$RUN_ID/score.json" "$DEST/" --region "$REGION"
        aws s3 cp --only-show-errors "s3://$BUCKET/runs/$RUN_ID/metadata.json" "$DEST/" --region "$REGION" 2>/dev/null || true
        echo; python3 -c "
import json
d = json.load(open('$DEST/score.json'))
print(f\"SCORE: {d['total_points']} points ({d['earned_count']} advancements)\")
for e in d['breakdown']:
    off = f\"+{e['offset_s']:.0f}s\" if e['offset_s'] is not None else '     '
    print(f\"  {off:>8}  {e['points']:>3}G  {e['title'] or e['id']}\")
"
        echo "full artifacts: aws s3 cp --recursive s3://$BUCKET/runs/$RUN_ID/ $DEST/"
        exit 0
    fi
    state=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$IID" \
        --query 'Reservations[0].Instances[0].State.Name' --output text || echo unknown)
    echo "  [$(date +%H:%M:%S)] instance=$state, no score yet"
    if [[ "$state" == "terminated" ]]; then
        echo "instance terminated without score.json — check s3://$BUCKET/runs/$RUN_ID/ for bench-userdata.log"
        exit 1
    fi
done
echo "timed out waiting; instance $IID may still be running — check the console"
exit 1
