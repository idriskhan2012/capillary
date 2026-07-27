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

- `public/index.html` is a **hash-routed single-page app with a home landing page, a
  persistent top navigation bar (Home / Stock Tracker / Mental Models Guide / Decision
  Framework + theme toggle), and breadcrumbs**. Every view has its own real, bookmarkable,
  back/forward-safe URL via `location.hash` (`#stocks`, `#stock-CAPLIPOINT`, `#model-<name>`,
  `#framework`), driven by a `route()` function. Stock/model "detail pages" render into
  in-panel views, not modals. This design came from a separate Claude.ai session and is
  now `public/index.html` itself — wired to `data.json`, with the removed Add-Stock stripped
  (the original standalone `capillary_research_terminal.html` was deleted once it went live).
  Diagnostics is still a small modal opened from the footer link (reads `logs.json`).
- **Data is no longer hardcoded.** The curated content (`stocks`, `models`, `categories`,
  `stockPosts`, `modelPost`) lives in `public/data.json`; `index.html` `fetch()`es it on
  load. Header/filter counts are computed from the data. Regenerate `data.json` from a
  legacy inline copy with the extractor pattern in git history if ever needed.
- **The tracker is read-only** — there is no user-write feature. Content is populated by
  the scraper + the promote step (`infra/promote.sh`). A manual "+ Add Stock" button used
  to exist (localStorage / a DynamoDB+Lambda-URL backend) but was removed: since the
  scraper already covers every stock from the posts, it was redundant, and dropping it
  removed a whole class of cross-device/auth complexity. If you ever want a personal
  watchlist of non-Capillary stocks back, that's the feature to re-add.
- **Deployed and live.** `lambda/scraper.py` implements the archive-diff, HTML-strip,
  Groq classification, and Twelve-Data-with-Yahoo-fallback price refresh, writing
  `data.json` back to S3. `infra/template.yaml` is the CloudFormation stack (S3 + CloudFront
  + scraper Lambda + daily EventBridge); `infra/deploy.sh` is one-command deploy. See
  `docs/DEPLOY.md`.

## Chosen implementation decisions (July 2026)

- **Prices:** Twelve Data (free tier) primary, Yahoo Finance fallback for tickers Twelve
  Data's free tier can't resolve (notably some NSE/BSE names).
- **No user-write backend:** the manual Add-Stock feature (and its DynamoDB + Lambda
  Function URL + CloudFront `/api` OAC path) was built, then removed as redundant — the
  CloudFront→Function-URL OAC path never authorized in this account despite correct config
  (only direct IAM SigV4 worked; likely an AWS-side edge case), and the scraper already
  covers every posted stock. If cross-device custom stocks are ever wanted, API Gateway
  HTTP API in front of a Lambda is the reliable path (free 12 mo, then pennies).
- **Classification:** **Groq** (OpenAI-compatible Chat Completions, `openai/gpt-oss-20b`
  primary → `gpt-oss-120b` fallback on 429). Gemini was dropped: its free-tier returned
  `limit: 0` for this Google account (region/eligibility — not a billing/project issue),
  so every call 429'd. Groq's free tier works. Sequential pacing + `MAX_CLASSIFY_PER_RUN`
  cap + a give-up-after-`MAX_CLASSIFY_ATTEMPTS` counter (`classify_attempts.json`) so a
  permanently-failing post stops being retried daily. gpt-oss-20b returns the ticker symbol
  reliably; drafts still land in `pending_review.json` for human review before promote.
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
this needs an LLM call. **Now uses Groq's free tier** (OpenAI-compatible Chat Completions,
`openai/gpt-oss-20b` primary → `gpt-oss-120b` fallback on 429).

  1. **Gemini was tried first and abandoned.** Its free-tier quota came back `limit: 0`
     for this Google account across *both* projects with billing OFF — i.e. the free tier
     simply isn't provisioned for this account/region (not a billing/project mistake). Every
     `generateContent` call 429'd. Groq's free tier works, so classification moved there.
  2. Free tiers may train on prompts — fine here (public Substack content); don't reuse for
     anything sensitive.
  3. **Price refresh and classification never share a quota** — prices go through Twelve
     Data/Yahoo, never the LLM. Classification only fires for genuinely new posts (~1-4/wk).
     Guards in `lambda/scraper.py` (don't remove when extending): sequential calls with
     `CLASSIFY_CALL_DELAY_SECONDS` (never parallel), `MAX_CLASSIFY_PER_RUN` cap per run, and
     a give-up-after-`MAX_CLASSIFY_ATTEMPTS` counter in `classify_attempts.json` so a
     permanently-failing post stops being retried daily and spamming the diagnostics log.

**Recommended safety net:** don't let the scraper auto-publish new stock entries straight
to the live tracker. Have it write to a "pending review" section (or just notify you) so
you can glance at the auto-classified ticker/sentiment/model before it goes live — a wrong
ticker guess or mis-tagged model sitting in a tracker used for real decisions is worse than
a 30-second manual check.

## Status & how to operate it

**Deployed and live** in the dedicated AWS account (region `ap-south-1`) — S3 + CloudFront
host the site; a daily EventBridge cron runs the scraper. Redeploy any change with
`AWS_PROFILE=capillary ./infra/deploy.sh`.

**Promoting classified drafts** — the scraper writes auto-classified drafts to
`pending_review.json` (the safety net), never straight to the live tracker. Promote with:
`./infra/promote.sh list`, then `./infra/promote.sh approve <slug> [--model "Name"]`.
Approve adds the stock, **tags the ticker onto its mental model** (`models[].tickers` — the
reverse cross-link the UI needs), adds the `stockPosts` link, drains the queue, and
re-uploads `data.json`. Pure-stdlib `infra/promote.py` does the wiring; CLI only, by design.

**Possible future work:** a personal watchlist of non-Capillary stocks (the removed
Add-Stock feature) — would need a write path; API Gateway HTTP API in front of a Lambda is
the reliable option if revisited.

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
