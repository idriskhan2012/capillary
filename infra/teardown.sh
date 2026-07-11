#!/usr/bin/env bash
#
# Tear everything down. Empties the S3 buckets (CloudFormation can't delete a
# non-empty bucket), deletes the stack (S3 site bucket, CloudFront, Lambdas,
# DynamoDB, EventBridge, IAM roles), removes the artifacts bucket, and deletes
# the SSM API-key parameters.
set -euo pipefail

PROJECT="${PROJECT:-capillary}"
REGION="${CAP_REGION:-${AWS_REGION:-ap-south-1}}"
STACK_NAME="${STACK_NAME:-${PROJECT}-stack}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
SITE_BUCKET="${SITE_BUCKET:-${PROJECT}-site-${ACCOUNT_ID}}"
ARTIFACTS_BUCKET="${PROJECT}-deploy-artifacts-${ACCOUNT_ID}-${REGION}"

echo "This will DELETE the stack ${STACK_NAME} and all its data in ${REGION}."
read -r -p "Type 'delete' to confirm: " CONFIRM
[[ "$CONFIRM" == "delete" ]] || { echo "Aborted."; exit 1; }

echo "Emptying site bucket..."
aws s3 rm "s3://${SITE_BUCKET}" --recursive --region "$REGION" 2>/dev/null || true

echo "Deleting stack (CloudFront distribution deletion can take several minutes)..."
aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION" || true

echo "Emptying + removing artifacts bucket..."
aws s3 rb "s3://${ARTIFACTS_BUCKET}" --force --region "$REGION" 2>/dev/null || true

echo "Deleting SSM parameters..."
aws ssm delete-parameter --name /capillary/gemini-api-key --region "$REGION" 2>/dev/null || true
aws ssm delete-parameter --name /capillary/twelvedata-api-key --region "$REGION" 2>/dev/null || true

echo "Done."
