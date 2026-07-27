#!/usr/bin/env bash
#
# One-command deploy for The Capillary Research Terminal.
#
#   1. reads API keys from infra/secrets.env
#   2. stores them in SSM Parameter Store (SecureString)
#   3. packages the two Lambdas + deploys the CloudFormation stack
#   4. uploads the site (index.html etc); seeds data.json on first deploy only
#   5. invalidates the CloudFront cache and prints the live URL
#
# Re-runnable. It will NOT overwrite the live data.json (scraper-updated prices)
# on repeat runs — set FORCE_DATA=1 to push your local public/data.json anyway
# (use that when you edit curated content like a thesis).
#
# Prereqs: aws CLI configured with the TARGET account's credentials, and
# infra/secrets.env filled in (copy from secrets.env.example).
set -euo pipefail

PROJECT="${PROJECT:-capillary}"
REGION="${CAP_REGION:-${AWS_REGION:-ap-south-1}}"
STACK_NAME="${STACK_NAME:-${PROJECT}-stack}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# --- load secrets --------------------------------------------------------
if [[ ! -f infra/secrets.env ]]; then
  echo "ERROR: infra/secrets.env not found. Copy infra/secrets.env.example and fill it in." >&2
  exit 1
fi
# shellcheck disable=SC1091
set -a; source infra/secrets.env; set +a
: "${GROQ_API_KEY:?set GROQ_API_KEY in infra/secrets.env}"
: "${TWELVEDATA_API_KEY:?set TWELVEDATA_API_KEY in infra/secrets.env}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Deploying to account ${ACCOUNT_ID}, region ${REGION}"

SITE_BUCKET="${SITE_BUCKET:-${PROJECT}-site-${ACCOUNT_ID}}"
ARTIFACTS_BUCKET="${PROJECT}-deploy-artifacts-${ACCOUNT_ID}-${REGION}"
GROQ_PARAM="/capillary/groq-api-key"
TWELVEDATA_PARAM="/capillary/twelvedata-api-key"

# --- 1. secrets -> SSM ---------------------------------------------------
echo "Storing API keys in SSM Parameter Store (SecureString)..."
aws ssm put-parameter --name "$GROQ_PARAM"       --type SecureString --value "$GROQ_API_KEY"       --overwrite --region "$REGION" >/dev/null
aws ssm put-parameter --name "$TWELVEDATA_PARAM" --type SecureString --value "$TWELVEDATA_API_KEY" --overwrite --region "$REGION" >/dev/null

# --- 2. artifacts bucket (for Lambda zips) -------------------------------
if ! aws s3api head-bucket --bucket "$ARTIFACTS_BUCKET" --region "$REGION" 2>/dev/null; then
  echo "Creating artifacts bucket ${ARTIFACTS_BUCKET}..."
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$ARTIFACTS_BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$ARTIFACTS_BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
fi

# --- 3. package + deploy -------------------------------------------------
rm -rf lambda/__pycache__
echo "Packaging Lambdas..."
aws cloudformation package \
  --template-file infra/template.yaml \
  --s3-bucket "$ARTIFACTS_BUCKET" \
  --output-template-file infra/packaged.yaml \
  --region "$REGION" >/dev/null

get_output () { aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

echo "Deploying stack ${STACK_NAME} (CloudFront can take 5-15 min on first run)..."
aws cloudformation deploy \
  --template-file infra/packaged.yaml \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides SiteBucketName="$SITE_BUCKET" ProjectName="$PROJECT" \
  --region "$REGION"

BUCKET="$(get_output SiteBucketName)"
DIST_ID="$(get_output DistributionId)"
SITE_URL="$(get_output SiteURL)"

# --- 4. upload site ------------------------------------------------------
echo "Uploading site content to s3://${BUCKET}..."
# Push everything except the live data/log files so we never clobber scraper output.
# no-cache so browsers always revalidate index.html (cheap 304s via ETag) —
# otherwise a stale cached index.html hides new UI until the browser cache expires.
aws s3 sync public/ "s3://${BUCKET}/" --exclude "data.json" --exclude "logs.json" \
  --cache-control "no-cache" --region "$REGION"

# Seed data.json only if it isn't already there (first deploy), unless forced.
if [[ "${FORCE_DATA:-0}" == "1" ]] || ! aws s3api head-object --bucket "$BUCKET" --key data.json --region "$REGION" >/dev/null 2>&1; then
  echo "Uploading data.json (seed / forced)..."
  aws s3 cp public/data.json "s3://${BUCKET}/data.json" --content-type application/json --cache-control no-cache --region "$REGION"
else
  echo "Keeping existing live data.json (set FORCE_DATA=1 to overwrite)."
fi

# --- 5. invalidate CloudFront -------------------------------------------
echo "Invalidating CloudFront cache..."
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" --region "$REGION" >/dev/null || true

echo ""
echo "======================================================================"
echo "  Live URL:  ${SITE_URL}"
echo "  (first deploy: allow a few minutes for CloudFront to finish)"
echo "======================================================================"
echo "Run the scraper once now with:"
echo "  aws lambda invoke --function-name ${PROJECT}-scraper --region ${REGION} /tmp/out.json && cat /tmp/out.json"
