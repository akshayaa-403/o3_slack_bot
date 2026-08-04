#!/usr/bin/env bash
#
# Stands up the admin console infrastructure. NOTHING here has been run.
#
# Read it before executing. Every step prints what it is about to do, and the
# whole script is a no-op unless you pass --apply, so you can dry-run it first:
#
#     ./infra/provision-console.sh              # prints the plan, changes nothing
#     ./infra/provision-console.sh --apply      # actually creates resources
#
# Resources created, and what each one costs:
#
#   S3 bucket                 pennies/month at this size
#   CloudFront distribution   free tier covers 1 TB out + 10M requests/month
#   Cognito user pool         free tier covers 10k monthly active users
#   DynamoDB tables (2)       on-demand; free tier covers 25 GB + low traffic
#   Lambda + HTTP API         free tier covers 1M requests/month
#
# At this console's traffic the expected steady-state bill is ~$0-2/month,
# dominated by the S3 bucket and CloudFront request overage if any. The one
# thing to watch is that a CloudFront distribution takes a while to delete, so
# treat creating it as the least reversible step.

set -euo pipefail

APPLY=false
[[ "${1:-}" == "--apply" ]] && APPLY=true

ACCOUNT_ID="${ACCOUNT_ID:-661779458398}"
REGION="${REGION:-ap-southeast-2}"
BUCKET="${BUCKET:-ivvy-admin-console-${ACCOUNT_ID}}"
TENANT_TABLE="${TENANT_TABLE:-ivvy-bot-tenants}"
INTENT_TABLE="${INTENT_TABLE:-ivvy-bot-intents}"
POOL_NAME="${POOL_NAME:-ivvy-console}"
FN_NAME="${FN_NAME:-o3_admin_api}"
ROLE_NAME="${ROLE_NAME:-IvvY_Admin_API_Role}"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
step() { printf '  %s\n' "$*"; }

run() {
  if $APPLY; then
    step "RUN  $*"
    "$@"
  else
    step "SKIP $*"
  fi
}

$APPLY || say "DRY RUN — nothing will be created. Re-run with --apply to commit."

# --------------------------------------------------------------------------- #
say "1. Private S3 bucket for the static files"
# --------------------------------------------------------------------------- #
step "Bucket stays private. CloudFront reads it through an Origin Access"
step "Control, so nobody can bypass the CDN and hit S3 directly."
run aws s3api create-bucket \
  --bucket "$BUCKET" \
  --region "$REGION" \
  --create-bucket-configuration "LocationConstraint=$REGION"
run aws s3api put-public-access-block \
  --bucket "$BUCKET" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
run aws s3api put-bucket-encryption \
  --bucket "$BUCKET" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# --------------------------------------------------------------------------- #
say "2. DynamoDB tables"
# --------------------------------------------------------------------------- #
step "On-demand billing: no capacity to plan, and it costs nothing when idle."
run aws dynamodb create-table \
  --table-name "$TENANT_TABLE" \
  --attribute-definitions AttributeName=tenant_id,AttributeType=S \
  --key-schema AttributeName=tenant_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION"

step "Drafted intents are keyed by tenant so one query returns one tenant's set."
run aws dynamodb create-table \
  --table-name "$INTENT_TABLE" \
  --attribute-definitions \
    AttributeName=tenant_id,AttributeType=S \
    AttributeName=intent_key,AttributeType=S \
  --key-schema \
    AttributeName=tenant_id,KeyType=HASH \
    AttributeName=intent_key,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION"

# --------------------------------------------------------------------------- #
say "3. Execution role for the API Lambda"
# --------------------------------------------------------------------------- #
step "Policy is in iam/admin_api_policy.json. Substitute ACCOUNT_ID and REGION"
step "first — see iam/README.md for the sed one-liner and the rationale."
run aws iam create-role \
  --role-name "$ROLE_NAME" \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]
  }'
step "Then: aws iam put-role-policy --role-name $ROLE_NAME \\"
step "        --policy-name AdminApi --policy-document file://iam/admin_api_policy.json"

# --------------------------------------------------------------------------- #
say "4. Cognito user pool + PKCE app client"
# --------------------------------------------------------------------------- #
step "custom:role and custom:tenants drive what each person can see."
run aws cognito-idp create-user-pool \
  --pool-name "$POOL_NAME" \
  --schema \
    'Name=role,AttributeDataType=String,Mutable=true' \
    'Name=tenants,AttributeDataType=String,Mutable=true' \
  --region "$REGION"
step "Then create an app client with NO secret (public client) and"
step "allowed flow 'code'. A secret cannot be kept in a browser, which is why"
step "js/auth.js uses PKCE instead."

# --------------------------------------------------------------------------- #
say "5. Lambda function"
# --------------------------------------------------------------------------- #
run bash -c "cd \"$(dirname "$0")/..\" && zip -q -j /tmp/admin_api.zip lambda_o3_admin_api.py"
run aws lambda create-function \
  --function-name "$FN_NAME" \
  --runtime python3.12 \
  --handler lambda_o3_admin_api.lambda_handler \
  --role "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}" \
  --zip-file fileb:///tmp/admin_api.zip \
  --timeout 15 \
  --memory-size 512 \
  --environment "Variables={TENANT_TABLE=$TENANT_TABLE,INTENT_TABLE=$INTENT_TABLE,SECRET_PREFIX=ivvy-bot,METRIC_NAMESPACE=IvvY/Bot}" \
  --region "$REGION"

# --------------------------------------------------------------------------- #
say "6. HTTP API with a Cognito JWT authorizer"
# --------------------------------------------------------------------------- #
step "The authorizer validates the token, so the Lambda never parses one."
step "Create the API, add a JWT authorizer (issuer = the user pool, audience ="
step "the app client id), then one \$default route to the Lambda."
step "Left as explicit console/CLI steps because the ids only exist after 4 and 5."

# --------------------------------------------------------------------------- #
say "7. CloudFront distribution"
# --------------------------------------------------------------------------- #
step "Two origins:"
step "  default   -> the S3 bucket via OAC          (cache aggressively)"
step "  /api/*    -> the HTTP API endpoint          (caching disabled)"
step "One hostname means no CORS and the Authorization header just works."
step "Also attach a response-headers policy for CSP and HSTS — this console"
step "handles Jira tokens, so those headers are not optional."
step "Least reversible step: a distribution takes ~15 min to disable + delete."

# --------------------------------------------------------------------------- #
say "8. Upload the console"
# --------------------------------------------------------------------------- #
step "tests/ is excluded — it must not be publicly reachable."
run bash -c "cd \"$(dirname "$0")/../admin-console\" && aws s3 sync . \"s3://$BUCKET\" \
  --delete --exclude 'tests/*' --exclude 'README.md'"

say "Done"
if ! $APPLY; then
  step "That was a dry run. Nothing exists yet."
else
  step "Set IVVY_USE_MOCK = false and the Cognito values in"
  step "admin-console/js/config.js, then re-run step 8."
fi
