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

The tracker is read-only — content comes from `data.json` (populated by the scraper +
`infra/promote.sh`). No user-write feature.

## Deploy to AWS

One dedicated AWS account + two free API keys, then one command. See **`docs/DEPLOY.md`**.

```bash
cp infra/secrets.env.example infra/secrets.env   # fill in the two API keys
AWS_PROFILE=capillary ./infra/deploy.sh
```

## Project layout

```
public/index.html    → the app: 3 tabs (tracker / mental models / decision framework)
public/data.json     → curated content the app fetches (stocks, models, cross-links)
public/logs.json     → sample; the live one is written to S3 by the scraper
lambda/scraper.py     → daily scraper: new posts (Groq) + prices (Twelve Data/Yahoo) → data.json
infra/template.yaml   → CloudFormation: S3 + CloudFront + scraper Lambda + EventBridge
infra/deploy.sh       → one-command deploy;  infra/teardown.sh → remove everything
infra/promote.sh      → review + promote scraper-classified drafts into the live tracker
docs/DEPLOY.md        → full deployment runbook (IAM user, permissions, keys, costs)
docs/ARCHITECTURE.md  → design + logging/diagnostics + rate-limit reasoning
docs/TICKER_MAP.md    → company name → exchange ticker lookups (the fiddly part)
CLAUDE.md             → full project context/decisions — READ THIS FIRST in a fresh session
```

## Status

Deployed and live: S3 + CloudFront host the 3-tab site; a daily EventBridge cron runs the
scraper (prices + new-post classification). Read-only tracker. See `CLAUDE.md`.
