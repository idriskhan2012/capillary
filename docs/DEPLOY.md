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
| Lambda `capillary-addstock` + Function URL | the "Add Stock" write API | always-free |
| DynamoDB `capillary-custom-stocks` | stores custom stocks (5/5 provisioned) | **always-free** (≤25/25) |
| SSM Parameter Store (SecureString ×2) | holds the Gemini + Twelve Data keys | free (Standard tier) |

**Add-Stock is $0 forever** (DynamoDB + Lambda + Function URLs are always-free — no API Gateway).
Hosting (S3 + CloudFront) is free for 12 months, then ~1–5¢/month for a site this small.

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
- **Gemini** — https://aistudio.google.com/apikey → create a key in a **dedicated Google Cloud
  project with billing DISABLED** (enabling billing kills that project's free tier — see
  [ARCHITECTURE.md](ARCHITECTURE.md)).

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
- **Add-Stock auth:** the write API is a public Function URL (CORS-open, called directly by the
  site — `deploy.sh` writes its URL into `config.js`) — fine for a personal tracker, but it is
  *not* authenticated. To harden, put Cognito or a shared-secret header in front of
  `capillary-addstock`. See the note at the top of [`lambda/addstock.py`](../lambda/addstock.py).
- **Region:** defaults to `ap-south-1` (Mumbai). Override with `CAP_REGION=...`. CloudFront is
  global regardless.
- **Secrets** live only in SSM Parameter Store (SecureString) and your local `secrets.env` —
  never in the template, git, or Lambda env vars.
