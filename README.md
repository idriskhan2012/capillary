# The Capillary Research Terminal

A personal tracker for every stock call made on [The Capillary](https://thecapillary.substack.com/),
plus a plain-language field guide to the ~20 mental models the author uses to make those calls.

## Quick start (local)

The app reads its data from `data.json`, so serve the folder over http (opening the file
directly with `file://` won't let it `fetch`):

```bash
cd public && python3 -m http.server 8000
# then open http://127.0.0.1:8000/index.html
```

Locally there's no backend, so "Add Stock" transparently falls back to this browser's
localStorage.

## Deploy to AWS

One dedicated AWS account + two free API keys, then one command. See **`docs/DEPLOY.md`**.

```bash
cp infra/secrets.env.example infra/secrets.env   # fill in the two API keys
AWS_PROFILE=capillary ./infra/deploy.sh
```

## Project layout

```
public/index.html    → the app (tracker + mental models guide, single file)
public/data.json     → curated content the app fetches (stocks, models, cross-links)
public/logs.json     → sample; the live one is written to S3 by the scraper
lambda/scraper.py     → daily scraper: new posts (Gemini) + prices (Twelve Data/Yahoo) → data.json
lambda/addstock.py    → DynamoDB CRUD behind a free Lambda Function URL (the "Add Stock" API)
infra/template.yaml   → CloudFormation: S3 + CloudFront + Lambdas + DynamoDB + EventBridge
infra/deploy.sh       → one-command deploy;  infra/teardown.sh → remove everything
docs/DEPLOY.md        → full deployment runbook (IAM user, permissions, keys, costs)
docs/ARCHITECTURE.md  → design + logging/diagnostics + rate-limit reasoning
docs/TICKER_MAP.md    → company name → exchange ticker lookups (the fiddly part)
CLAUDE.md             → full project context/decisions — READ THIS FIRST in a fresh session
```

## Status

Frontend: done (fetches `data.json`). Backend (scraper + Add-Stock API) and infra: written,
validated locally, ready to deploy — waiting on a dedicated AWS account. See `CLAUDE.md`.
