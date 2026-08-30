#!/usr/bin/env bash
# One-time (idempotent) AWS setup for the bench:
#   - S3 artifacts bucket        mineclaude-bench-<account-id>
#   - IAM role+instance profile  mineclaude-bench-ec2 (S3 write, SSM token read)
#   - Security group             mineclaude-bench (inbound: SSH from your IP)
#   - Key pair                   mineclaude-bench (-> ~/.ssh/mineclaude-bench.pem)
#   - SSM SecureStrings          one per harness credential:
#       /mineclaude-bench/claude-code-oauth-token  (claude-code)
#       /mineclaude-bench/opencode-api-key         (opencode)
#       /mineclaude-bench/cursor-api-key           (cursor)
#
# Prereqs: `aws configure` done; the harness credentials you intend to run in
# env or repo .env. Only the ones you have are uploaded — a missing one warns
# and skips, so you can add a harness later by re-running.
# Re-run any time — everything is create-if-missing, and token parameters are
# overwritten (so re-run after rotating a credential).
set -euo pipefail
cd "$(dirname "$0")/../.."

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="mineclaude-bench-${ACCOUNT}"
ROLE=mineclaude-bench-ec2
SG=mineclaude-bench
KEY=mineclaude-bench

echo "account=$ACCOUNT region=$REGION"

# --- S3 bucket ---
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    if [[ "$REGION" == "us-east-1" ]]; then
        aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
    else
        aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
            --create-bucket-configuration "LocationConstraint=$REGION"
    fi
    echo "created bucket s3://$BUCKET"
else
    echo "bucket s3://$BUCKET exists"
fi

# --- IAM role + instance profile ---
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
    aws iam create-role --role-name "$ROLE" --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]
    }' >/dev/null
    echo "created role $ROLE"
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name bench-access --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": [\"s3:PutObject\", \"s3:GetObject\"], \"Resource\": \"arn:aws:s3:::${BUCKET}/*\"},
    {\"Effect\": \"Allow\", \"Action\": \"s3:ListBucket\", \"Resource\": \"arn:aws:s3:::${BUCKET}\"},
    {\"Effect\": \"Allow\", \"Action\": \"ssm:GetParameter\", \"Resource\": \"arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/mineclaude-bench/*\"}
  ]
}"
if ! aws iam get-instance-profile --instance-profile-name "$ROLE" >/dev/null 2>&1; then
    aws iam create-instance-profile --instance-profile-name "$ROLE" >/dev/null
    aws iam add-role-to-instance-profile --instance-profile-name "$ROLE" --role-name "$ROLE"
    echo "created instance profile $ROLE"
fi

# --- security group (SSH from current IP only) ---
VPC=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=is-default,Values=true \
    --query 'Vpcs[0].VpcId' --output text)
SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=group-name,Values=$SG" "Name=vpc-id,Values=$VPC" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo None)
if [[ "$SG_ID" == "None" || -z "$SG_ID" ]]; then
    SG_ID=$(aws ec2 create-security-group --region "$REGION" --vpc-id "$VPC" \
        --group-name "$SG" --description "mineclaude bench (SSH only)" \
        --query GroupId --output text)
    echo "created security group $SG_ID"
fi
MYIP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
    --protocol tcp --port 22 --cidr "${MYIP}/32" 2>/dev/null \
    && echo "authorized SSH from ${MYIP}/32" || echo "SSH rule already present"

# --- key pair ---
PEM="$HOME/.ssh/${KEY}.pem"
if ! aws ec2 describe-key-pairs --region "$REGION" --key-names "$KEY" >/dev/null 2>&1; then
    aws ec2 create-key-pair --region "$REGION" --key-name "$KEY" \
        --query KeyMaterial --output text > "$PEM"
    chmod 600 "$PEM"
    echo "created key pair -> $PEM"
else
    echo "key pair $KEY exists (pem expected at $PEM)"
fi

# --- harness credentials -> SSM SecureStrings ---
# One per harness; the VM pulls only the one its --harness needs.
put_secret() {  # $1 = env var name, $2 = SSM parameter name, $3 = how to get it
    local value="${!1:-$(grep -E "^$1=" .env 2>/dev/null | cut -d= -f2- || true)}"
    # .env values may be quoted (compose strips quotes at interpolation; a raw
    # grep doesn't) — strip one layer of surrounding quotes so SSM stores the
    # bare credential.
    value="${value#[\"\']}"; value="${value%[\"\']}"
    if [[ -n "$value" ]]; then
        aws ssm put-parameter --region "$REGION" --name "$2" \
            --type SecureString --value "$value" --overwrite >/dev/null
        echo "stored $1 in SSM ($2)"
    else
        echo "WARN: $1 not found in env or .env — $3"
    fi
}

put_secret CLAUDE_CODE_OAUTH_TOKEN /mineclaude-bench/claude-code-oauth-token \
    "run 'claude setup-token' and re-run (needed for --harness claude-code)"
put_secret OPENCODE_API_KEY /mineclaude-bench/opencode-api-key \
    "get a key from opencode Zen (Go plan) and re-run (needed for --harness opencode)"
put_secret CURSOR_API_KEY /mineclaude-bench/cursor-api-key \
    "create one at Cursor dashboard -> API Keys and re-run (needed for --harness cursor)"

echo "setup complete"
