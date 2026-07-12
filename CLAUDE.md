# The Capillary Research Terminal — Project Context

This file exists so Claude Code (or future-you) has full context without re-reading the
original chat conversation. Read this fully before making changes.

## What this project is

A personal research tool tracking every stock call made by "The Capillary"
(https://thecapillary.substack.com/), a stock-focused Substack. It has three parts:

1. **A tracker** — every stock the author has discussed, his sentiment (bullish/bearish/
   neutral), the price when he posted vs. the live current price, his thesis, and a link
   back to the original post.
2. **A mental models field guide** — the ~20 recurring frameworks he uses to make these
   calls (Two-Engine Framework, Bottleneck Strategy, Caged Bird Model, etc.), each
   explained in plain language with an analogy, cross-linked to which stocks it was used on.
3. **A Decision Framework** — a synthesized 11-stage process (not something the author
   wrote directly) chaining all 20 mental models into one ordered sequence (Screen →
   Analyze → Size & Time → Maintain), shown as a vertical timeline with checkpoints and
   worked examples, cross-linked to the other two tabs.

All live in one file: `public/index.html` — a single-page HTML/CSS/JS app (no build step,
no framework). The tracker/models data is loaded from `data.json`; the Decision Framework
is authored static HTML (synthesized content the scraper never touches).

## Current state

- `public/index.html` is fully built and working: three tabs (Stock Tracker / Mental Models
  Guide / Decision Framework), filters, cross-linked modals, links to original Substack
  posts, a light/dark theme toggle, and a hidden "System Diagnostics" panel (footer link)
  that reads `logs.json`.
- **Data is no longer hardcoded.** The curated content (`stocks`, `models`, `categories`,
  `stockPosts`, `modelPost`) lives in `public/data.json`; `index.html` `fetch()`es it on
  load. Header/filter counts are computed from the data. Regenerate `data.json` from a
  legacy inline copy with the extractor pattern in git history if ever needed.
- The "+ Add Stock" feature is **API-first with a localStorage fallback**: it calls
  `/api/stocks` (a DynamoDB-backed Lambda Function URL, cross-device) when the backend is
  deployed, and falls back to this browser's `localStorage` when there's no API (local dev,
  or pre-deploy). No more `window.storage` in the data path (theme still uses it with a
  localStorage fallback — harmless).
- **Backend is written and ready to deploy** (not yet deployed — waiting on a dedicated AWS
  account). `lambda/scraper.py` implements the archive-diff, HTML-strip, Gemini
  classification, and Twelve-Data-with-Yahoo-fallback price refresh, writing `data.json`
  back to S3. `lambda/addstock.py` is the DynamoDB CRUD handler. `infra/template.yaml` is
  the full CloudFormation stack; `infra/deploy.sh` is one-command deploy. See `docs/DEPLOY.md`.

## Chosen implementation decisions (July 2026)

- **Prices:** Twelve Data (free tier) primary, Yahoo Finance fallback for tickers Twelve
  Data's free tier can't resolve (notably some NSE/BSE names).
- **Add-Stock backend:** DynamoDB + a **Lambda Function URL** (NOT API Gateway) so it stays
  in the always-free tier forever — API Gateway's free tier expires after 12 months.
- **Classification:** implemented now (not deferred), Gemini via REST, sequential pacing kept.
- **Secrets:** SSM Parameter Store SecureString, set by `deploy.sh` from `infra/secrets.env`.
  Never in the template, git, or Lambda env vars.

## How the original data was gathered (for the scraper to replicate)

Substack's public JSON API works great server-side (no CORS issue there — CORS only
blocks *browser* cross-origin calls, not server-to-server):

```
GET https://thecapillary.substack.com/api/v1/archive?sort=new&limit=50&offset=0
```
Returns all posts with `slug`, `title`, `post_date`. Then for each post:
```
GET https://thecapillary.substack.com/api/v1/posts/{slug}
```
Returns `body_html` — parse with BeautifulSoup, `get_text()`, strip to plain text.

Stock prices were fetched via Yahoo Finance's undocumented endpoint:
```
GET https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}?interval=1d&range=5d
```
`data.chart.result[0].meta.regularMarketPrice`. **This is unofficial and can break/rate-
limit without warning** — for anything unattended (a cron job), swap to a real free-tier
API with a key (Alpha Vantage, Twelve Data, or Finnhub all have workable free daily quotas).
Indian tickers need the `.NS` (NSE) or `.BO` (BSE) suffix, e.g. `CAPLIPOINT.NS`.

Ticker resolution was the fiddly part — company names in the posts ("Kalyan Jewellers")
don't map cleanly to tickers (`KALYANKJIL.NS`) without some trial and error. Keep the
manual mapping table (`docs/TICKER_MAP.md` — TODO: create this) and only extend it by
hand when a genuinely new company shows up.

## Decisions made on the live-updating system (not yet built)

**Why not fully client-side:** Confirmed neither Substack's API nor Yahoo Finance sends
`Access-Control-Allow-Origin` headers, so the browser cannot fetch either directly —
this must run server-side.

**Chosen architecture (AWS free tier):**
- **EventBridge** — daily cron trigger (free)
- **Lambda (scraper)** — does the archive-diff + post-fetch + price-fetch above, writes
  `data.json` — well within the always-free 1M requests/month + 400K GB-seconds tier
- **S3** — hosts both the static site AND `data.json` in the same bucket/distribution,
  so the browser fetch is same-origin (no CORS problem at all)
- **CloudFront** (optional) — HTTPS + real URL in front of S3, 12-months-free tier covers
  this trivially
- **IAM role** — scoped to just this one bucket

**For classifying NEW posts automatically** (sentiment, mental-model tag, thesis summary):
this needs an LLM call, since it's language understanding, not scraping. Decided to use
**Google Gemini's free API tier** (Flash/Flash-Lite models) instead of the Anthropic API,
purely for zero cost — confirmed the free tier (~1,500 requests/day) is enormous overkill
for ~1-4 posts/week of usage, so this is sustainable indefinitely, with two caveats to
remember:
  1. Keep the Gemini classifier in its **own dedicated Google Cloud project with billing
     disabled** — enabling billing on a project kills its free tier entirely, even for
     calls that would've fit inside the free quota.
  2. Google trains on free-tier prompts — fine here since Substack content is already public,
     but don't reuse this exact setup for anything sensitive.
  3. **Price refresh and classification never share a quota** — refreshing all tracked
     tickers' prices goes through a separate price API, not Gemini, so it can't touch
     Gemini's limits no matter how many stocks you track. Classification only fires for
     new posts (or worst-case, a full daily reclassification of ~25 stocks), which is
     under 2% of Gemini's 1,500/day free cap either way. The actual risk isn't volume,
     it's **bursting** — Gemini free tier also caps requests-per-minute (10-15 RPM), so
     classification calls must run sequentially with a small delay between them (2-3s),
     never in a parallel batch. This is implemented in `lambda/scraper.py` — don't remove
     the pacing when extending it.

**Recommended safety net:** don't let the scraper auto-publish new stock entries straight
to the live tracker. Have it write to a "pending review" section (or just notify you) so
you can glance at the auto-classified ticker/sentiment/model before it goes live — a wrong
ticker guess or mis-tagged model sitting in a tracker used for real decisions is worse than
a 30-second manual check.

## What's NOT built yet (the actual next steps)

1. **The actual AWS deploy** — everything is coded and validated locally, but nothing has
   been created in AWS yet (waiting on a dedicated account + its `capillary-deployer` creds
   and the two API keys). Once those exist: `AWS_PROFILE=capillary ./infra/deploy.sh`.
2. **Promoting classified drafts** — the scraper writes auto-classified drafts to
   `pending_review.json` (the safety net). Promote them with `infra/promote.sh`:
   `./infra/promote.sh list`, then `./infra/promote.sh approve <slug> [--model "Name"]`.
   Approve adds the stock, **tags the ticker onto its mental model** (`models[].tickers` —
   the reverse cross-link the UI needs), adds the `stockPosts` link, drains the queue, and
   re-uploads `data.json`. A pure-stdlib `infra/promote.py` does the wiring; there's no
   in-browser review UI (CLI only, by design — it's a personal tool).
3. **Add-Stock hardening** — the write API is a public (unauthenticated) Function URL with
   open CORS. Fine for a personal tracker; add Cognito / a shared-secret header before it's
   anything more. See the note atop `lambda/addstock.py`.

Done since the original handoff: scraper implementation (1,2 old), `infra/` CloudFormation +
deploy scripts (3 old), `data.json` fetch refactor (4 old), Add-Stock backend (5 old).

## Style/design notes (so a rebuild doesn't drift)

- **Light theme is the default** (warm off-white `#FAFAF7`, near-black text, deepened orange
  accent `#C9541A` for contrast on white). Dark mode is fully supported via
  `[data-theme="dark"]` on `<html>` (ink `#0E1116`, accent `#FF6719` — the actual Substack
  brand color). Add new colors as CSS custom properties with BOTH a light and dark value in
  `:root` / `[data-theme="dark"]`; never hardcode hex in a component.
- **Fonts:** Source Serif 4 (headings), Inter (all UI text — tabs, buttons, badges, labels,
  body), IBM Plex Mono reserved **strictly for numeric/ticker data** (prices, tickers, stat
  values, stage numbers). Don't put UI labels in mono — an earlier pass did and it read worse.
- **Decision Framework tab** is a single left-aligned vertical timeline (`.tl-*` + reused
  `.stage-card`/`.badge`/`.tick-chip`), NOT an SVG flowchart. An earlier flowchart version
  (diamonds, legend, extra phase colors) was scrapped for looking like an imported diagram —
  don't rebuild it that way. Reuse existing components over inventing new visual patterns.
- The UI (theme, fonts, framework tab) was merged in from a separate Claude.ai design session
  (`public/index.reference.html` / the old `capillary-tracker enhacements/` copy, now deleted).
  Treat the live `public/index.html` as source of truth; it has both the visual work AND the
  data.json/backend wiring layered together.
