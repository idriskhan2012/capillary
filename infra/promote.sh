#!/usr/bin/env bash
#
# Review + promote scraper-classified drafts into the live tracker.
#
#   ./infra/promote.sh list                         # show pending drafts
#   ./infra/promote.sh approve <slug> [--model "…"] # wire a draft in + go live
#   ./infra/promote.sh reject  <slug>               # drop a draft
#
# "approve" adds the stock, tags it onto its mental model (models[].tickers), adds the
# post link (stockPosts), removes it from the queue, re-uploads data.json, and
# invalidates CloudFront. Downloads live files first so it never works on stale data.
#
# Requires: aws CLI (target profile), and infra/promote.py (pure stdlib).
set -euo pipefail

PROJECT="${PROJECT:-capillary}"
REGION="${CAP_REGION:-${AWS_REGION:-ap-south-1}}"
STACK_NAME="${STACK_NAME:-${PROJECT}-stack}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${SITE_BUCKET:-${PROJECT}-site-${ACCOUNT_ID}}"
DIST_ID="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" --output text)"

TMP="$(mktemp -d)"
dl () { aws s3 cp "s3://${BUCKET}/$1" "${TMP}/$1" --region "$REGION" >/dev/null; }
ul () { aws s3 cp "${TMP}/$1" "s3://${BUCKET}/$1" --content-type application/json \
          --cache-control no-cache --region "$REGION" >/dev/null; }

CMD="${1:-}"; shift || true
case "$CMD" in
  list)
    if ! dl pending_review.json 2>/dev/null; then echo "No pending_review.json yet (no new posts classified)."; exit 0; fi
    python3 infra/promote.py list --pending "${TMP}/pending_review.json"
    ;;
  approve)
    SLUG="${1:?usage: promote.sh approve <slug> [--model \"Name\"]}"; shift
    dl data.json; dl pending_review.json
    python3 infra/promote.py approve --data "${TMP}/data.json" --pending "${TMP}/pending_review.json" --slug "$SLUG" "$@"
    echo "Uploading updated data.json + pending_review.json..."
    ul data.json; ul pending_review.json
    aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/data.json" --region "$REGION" >/dev/null || true
    echo "Live. Refresh the site to see it."
    ;;
  reject)
    SLUG="${1:?usage: promote.sh reject <slug>}"
    dl pending_review.json
    python3 infra/promote.py reject --pending "${TMP}/pending_review.json" --slug "$SLUG"
    ul pending_review.json
    ;;
  deepdive-list)
    if ! dl pending_deepdives.json 2>/dev/null; then echo "No pending_deepdives.json yet (none generated)."; exit 0; fi
    python3 infra/promote.py deepdive-list --pending "${TMP}/pending_deepdives.json"
    ;;
  deepdive-approve)
    TICKER="${1:?usage: promote.sh deepdive-approve <ticker>}"
    dl data.json; dl pending_deepdives.json
    python3 infra/promote.py deepdive-approve --data "${TMP}/data.json" --pending "${TMP}/pending_deepdives.json" --ticker "$TICKER"
    echo "Uploading updated data.json + pending_deepdives.json..."
    ul data.json; ul pending_deepdives.json
    aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/data.json" --region "$REGION" >/dev/null || true
    echo "Live. Refresh the site to see it in Deep Dives."
    ;;
  deepdive-reject)
    TICKER="${1:?usage: promote.sh deepdive-reject <ticker>}"
    dl pending_deepdives.json
    python3 infra/promote.py deepdive-reject --pending "${TMP}/pending_deepdives.json" --ticker "$TICKER"
    ul pending_deepdives.json
    ;;
  *)
    echo "usage: promote.sh {list | approve <slug> [--model \"Name\"] | reject <slug>"
    echo "               | deepdive-list | deepdive-approve <ticker> | deepdive-reject <ticker>}"; exit 1 ;;
esac

rm -rf "$TMP"
