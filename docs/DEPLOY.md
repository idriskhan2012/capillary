# Deployment Runbook

Everything needed to host The Capillary Research Terminal in a **dedicated AWS account**,
staying inside the free tiers. The whole thing is one CloudFormation stack + a deploy script.

## What gets created

| Resource | Purpose | Cost |
|---|---|---|
| S3 bucket (`capillary-site-<acct>`) | hosts `index.html`, `data.json`, `logs.json` | free 12 mo, then pennies |
| CloudFront distribution | HTTPS + CDN in front of S3 | free 12 mo (1 TB/mo out) |
| Lambda `capillary-scraper` | daily: refresh prices + classify new posts → `data.json` | always-free (1M req/mo) |
| EventBridge rule | daily cron trigger for the scraper | free |
| SSM Parameter Store (SecureString ×2) | holds the Groq + Twelve Data keys | free (Standard tier) |

Hosting (S3 + CloudFront) is free for 12 months, then ~1–5¢/month for a site this small.
The scraper Lambda + EventBridge are always-free. The tracker is read-only (no write API,
no DynamoDB) — content comes from the scraper + the promote step.

---

## Step 1 — Create the deployer IAM user (in the NEW account)

In the new account's IAM console:

1. **Users → Create user** → name: **`capillary-deployer`**
2. Permissions — pick one:
   - **Simplest (recommended for a dedicated, isolated project account):** attach the
     AWS-managed **`AdministratorAccess`** policy.
   - **Scoped alternative:** create a customer-managed policy from
     [`infra/deployer-iam-policy.json`](../infra/deployer-iam-policy.json) and attach it.
3. After creating the user: **Security credentials → Create access key → "Command Line Interface (CLI)"**.
   Save the **Access key ID** and **Secret access key**.

> You do **not** need to paste those keys into chat. Configure them locally (Step 2) and I
> deploy using that profile.

## Step 2 — Configure the AWS CLI with those creds

```bash
aws configure --profile capillary
#   AWS Access Key ID     : <the deployer's access key id>
#   AWS Secret Access Key : <the deployer's secret>
#   Default region name   : ap-south-1      # Mumbai (change if you prefer)
#   Default output format : json
```

Verify it points at the **new** account (not the current one):

```bash
aws sts get-caller-identity --profile capillary
```

## Step 3 — Get the two API keys

- **Twelve Data** — https://twelvedata.com/ → sign up → copy the API key (free tier: 800 calls/day).
- **Groq** — https://console.groq.com/ → API Keys → create (no card; free tier). Used to
  classify new posts (`openai/gpt-oss-20b` primary, `gpt-oss-120b` fallback). *(Gemini was
  the original choice but its free tier returned `limit: 0` for this account — see
  [ARCHITECTURE.md](ARCHITECTURE.md).)*

Put them in `infra/secrets.env` (gitignored):

```bash
cp infra/secrets.env.example infra/secrets.env
# edit infra/secrets.env and paste both keys
```

## Step 4 — Deploy

```bash
AWS_PROFILE=capillary CAP_REGION=ap-south-1 ./infra/deploy.sh
```

First run takes 5–15 min (CloudFront). It prints the **live HTTPS URL** at the end.

Then run the scraper once to pull fresh prices and pick up any new posts:

```bash
AWS_PROFILE=capillary aws lambda invoke --function-name capillary-scraper /tmp/out.json && cat /tmp/out.json
```

## Re-deploying

`deploy.sh` is re-runnable. It pushes `index.html` and assets every time but, by default,
**does not** overwrite the live `data.json` (so it won't clobber scraper-updated prices).
When you intentionally edit curated content (a thesis, a new model), force it:

```bash
FORCE_DATA=1 AWS_PROFILE=capillary ./infra/deploy.sh
```

## Promoting a newly-classified post into the tracker

The daily scraper classifies new posts into `pending_review.json` (it never auto-publishes).
To review and promote:

```bash
AWS_PROFILE=capillary ./infra/promote.sh list                      # see pending drafts
AWS_PROFILE=capillary ./infra/promote.sh approve <slug>            # wire it in + go live
AWS_PROFILE=capillary ./infra/promote.sh approve <slug> --model "The Two-Engine Framework"  # if the model tag needs fixing
AWS_PROFILE=capillary ./infra/promote.sh reject  <slug>            # discard a draft
```

`approve` adds the stock to `data.json`, **tags the ticker onto its mental model**
(`models[].tickers` — the reverse link that drives the model's ticker chips and the stock
modal's "Mental Models Behind This Call"), adds the `stockPosts` "read the original post"
link, removes the draft from the queue, re-uploads `data.json`, and invalidates CloudFront.
If the classifier's model guess doesn't match one of the 20 frameworks, `approve` refuses
and lists the valid `--model` names.

## Teardown

```bash
AWS_PROFILE=capillary ./infra/teardown.sh    # asks you to type 'delete' to confirm
```

---

## Notes / caveats

- **Twelve Data + Indian stocks:** NSE/BSE symbols may not all resolve on Twelve Data's free
  tier. The scraper automatically falls back to Yahoo Finance for any ticker Twelve Data
  can't price, and keeps the previous price (never blanks it) if both fail — failures show up
  in the site's **System Diagnostics** panel.
- **Read-only tracker:** there is no user-write API. Content is populated by the scraper +
  `infra/promote.sh`. (A manual Add-Stock feature backed by DynamoDB + a Lambda Function URL
  was built then removed — the CloudFront→Function-URL OAC path never authorized in this
  account despite correct config, and it was redundant with the scraper. If cross-device
  custom stocks are ever wanted, put an API Gateway HTTP API in front of a Lambda.)
- **Region:** defaults to `ap-south-1` (Mumbai). Override with `CAP_REGION=...`. CloudFront is
  global regardless.
- **Secrets** live only in SSM Parameter Store (SecureString) and your local `secrets.env` —
  never in the template, git, or Lambda env vars.
