#!/usr/bin/env bash
# One-time (idempotent) AWS setup for the bench:
#   - S3 artifacts bucket        mineclaude-bench-<account-id>
#   - IAM role+instance profile  mineclaude-bench-ec2 (S3 write, SSM token read)
#   - Security group             mineclaude-bench (inbound: SSH from your IP)
#   - Key pair                   mineclaude-bench (-> ~/.ssh/mineclaude-bench.pem)
#   - SSM SecureString           /mineclaude-bench/claude-code-oauth-token
#
# Prereqs: `aws configure` done; CLAUDE_CODE_OAUTH_TOKEN in env or repo .env.
# Re-run any time — everything is create-if-missing, and the token parameter is
# overwritten (so re-run after rotating the token).
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

# --- harness token -> SSM SecureString ---
TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' .env 2>/dev/null | cut -d= -f2- || true)}"
if [[ -n "$TOKEN" ]]; then
    aws ssm put-parameter --region "$REGION" \
        --name /mineclaude-bench/claude-code-oauth-token \
        --type SecureString --value "$TOKEN" --overwrite >/dev/null
    echo "stored harness token in SSM"
else
    echo "WARN: CLAUDE_CODE_OAUTH_TOKEN not found in env or .env — run again after 'claude setup-token'"
fi

echo "setup complete"
