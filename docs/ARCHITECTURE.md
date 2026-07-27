# Architecture — Live-Updating System (Planned)

## Why not fully client-side

Confirmed neither `thecapillary.substack.com`'s API nor `query1.finance.yahoo.com` sends
`Access-Control-Allow-Origin` headers. A browser cannot cross-origin-fetch either directly —
this has to run server-side, where CORS doesn't apply.

## Chosen design (AWS free tier)

```
EventBridge (daily cron)
        |
        v
Lambda: scraper  --curl-equivalent-->  Substack archive API + a price API
        |
        v
   S3: data.json   (same bucket/distribution as the static site = same-origin, no CORS)
        |
        v
CloudFront + S3 (static site: index.html)
        |
        v
   Your browser (fetch('/data.json') on load)
```

- **EventBridge**: free, no meaningful limit for a daily trigger.
- **Lambda**: 1M requests/month + 400K GB-seconds free, forever. A ~10s daily run uses
  essentially none of it.
- **S3**: 5GB storage, 20K GET/month free (12 months). A few KB of JSON daily is nothing.
- **CloudFront** (optional, recommended for HTTPS + a real domain): 1TB/month out free
  (12 months).
- **IAM role**: scope Lambda's permissions to just this one bucket, nothing else.

## Data sources

**Posts** (server-side, no CORS issue):
```
GET https://thecapillary.substack.com/api/v1/archive?sort=new&limit=50&offset=0
GET https://thecapillary.substack.com/api/v1/posts/{slug}   → body_html
```

**Prices**: Yahoo Finance's endpoint (`query1.finance.yahoo.com/v8/finance/chart/{TICKER}`)
is unofficial/undocumented — fine for one-off manual runs, risky for an unattended cron job
since it can rate-limit or change shape without warning. For anything scheduled, use a real
free-tier API with a key instead: Alpha Vantage, Twelve Data, or Finnhub all have workable
free daily quotas.

## Classifying new posts (sentiment, mental model, thesis)

This step is language understanding, not scraping — needs an LLM call. **Provider: Groq's
free tier** (OpenAI-compatible Chat Completions), `openai/gpt-oss-20b` primary →
`gpt-oss-120b` fallback on 429. At ~1-4 posts/week this is a rounding error against Groq's
free limits.

**Why not Gemini (the original plan):** Gemini was implemented first, but its free-tier
quota returned `limit: 0` for this Google account across *both* projects with billing OFF
— i.e. the free tier isn't provisioned for this account/region (verified live; not a
billing/project misconfiguration). Every `generateContent` call 429'd. Groq's free tier
works, so classification moved there. (Google/Groq may train on free-tier prompts — fine
for public Substack content; don't reuse for anything sensitive.)

## Rate limits & robustness

- **Price refresh** (all tracked tickers, daily) goes through Twelve Data / Yahoo, never the
  LLM — so prices and classification never share a quota, regardless of ticker count.
- **Classification** only runs for *new* posts (~1-4/week). Guards in
  `lambda/scraper.py`'s `classify_new_posts()` (do not remove when extending):
  - calls run **sequentially** with `CLASSIFY_CALL_DELAY_SECONDS` between them — never in
    parallel (no `asyncio.gather` / thread pool) — to stay under the per-minute cap;
  - `MAX_CLASSIFY_PER_RUN` caps how many posts are classified per daily run;
  - a **give-up counter** (`classify_attempts.json`, `MAX_CLASSIFY_ATTEMPTS`) marks a
    post seen after N failed attempts so a permanently-failing post stops being retried
    daily and spamming the diagnostics log — it's flagged once for manual entry instead.

## Safety net (recommended, not yet built)

Don't let the scraper auto-publish new stock entries straight into the live tracker.
Write newly classified posts to a "pending review" list instead (a separate JSON key,
or a Slack/email notification with the draft), so a wrong ticker guess or a mis-tagged
mental model gets a human glance before it's presented as fact in something feeding
real investing decisions.

## Logging — so failures are visible without digging through CloudWatch

Every external call in `lambda/scraper.py` (Substack archive/post fetch, Groq
classification, price refresh, S3 writes) is wrapped in a try/except that calls
`log_event()` on failure. Entries are appended to **`logs.json`** in the same S3
bucket as `data.json` — same-origin, so the frontend can fetch it directly with no
CORS concern, same as the main data file. Capped at the most recent 300 entries so
the file can't grow unbounded.

**Entry shape:**
```json
{
  "timestamp": "2026-07-11T15:30:00+00:00",
  "level": "error" | "warning" | "info",
  "source": "substack_archive" | "substack_post" | "groq_classify" | "price_refresh" | "s3_write" | "run_summary",
  "message": "human-readable description",
  "context": { "slug": "...", "ticker": "...", "exception": "...", "rate_limited": true/false }
}
```

A `groq_classify` failure containing `"rate_limited": true` in its context means a
`429`/`RESOURCE_EXHAUSTED` was hit — that specifically points at the pacing in
`classify_new_posts()` (see `CLASSIFY_CALL_DELAY_SECONDS`) needing to be increased,
not at the daily quota being exhausted (see the Rate limits section above for why
volume itself is very unlikely to be the cause).

**Where this shows up:** `public/index.html` has a hidden "System Diagnostics" link
in the footer (easy to miss unless you're looking, deliberately — this is a personal
debug tool, not a feature to advertise) that fetches `logs.json` and renders it as a
filterable list (All / Errors / Warnings / Info), newest first. Every failed run
should be visible there the next time you open the site, without needing to log into
AWS and hunt through CloudWatch.

Every successful run also logs one `info`-level `run_summary` entry (new posts found,
how many classified successfully, how many failed) — so an empty Errors filter with a
recent `run_summary` entry confirms the scraper is actually running, not just quiet
because it's broken.

## Ticker resolution

Company names in posts ("Kalyan Jewellers") don't map cleanly to exchange tickers
(`KALYANKJIL.NS`) — this needed manual trial-and-error against Yahoo Finance the first
time around. See `docs/TICKER_MAP.md`. Keep that file as the source of truth and only
extend it by hand the first time a genuinely new company appears; an LLM guess at a
ticker suffix (`.NS` vs `.BO` vs no suffix at all) will occasionally be wrong.
