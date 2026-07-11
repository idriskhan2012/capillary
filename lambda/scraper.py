"""
The Capillary — scheduled scraper.

Runs on a daily EventBridge trigger. Job:
  1. Diff Substack's archive against what we saw last time -> find new posts.
  2. Pull each new post's full text and strip it to plain text.
  3. Call Gemini (free tier) to classify each new post: ticker, sentiment,
     mental model, thesis, outlook -> write drafts to pending_review.json
     (NOT straight into the live tracker — see the "Safety net" in ARCHITECTURE.md).
  4. Refresh current prices for all tracked tickers (Twelve Data, Yahoo fallback)
     and update the `current` field of each stock in data.json.
  5. Write the merged result back to data.json in this Lambda's S3 bucket.
  6. Log every error/warning (rate limits, failed lookups, etc.) to logs.json in the
     same bucket, so they're visible from the site's hidden diagnostics panel instead
     of only living in CloudWatch. See "Logging" below and ARCHITECTURE.md.

Secrets (Twelve Data + Gemini API keys) are read at runtime from SSM Parameter Store
(SecureString) — the parameter NAMES come in as env vars, never the keys themselves.

See ../CLAUDE.md and ../docs/ARCHITECTURE.md for full context and decisions before
changing this. See ../docs/TICKER_MAP.md before adding any new ticker.

No third-party pip dependencies: everything here uses the Python stdlib plus boto3,
which is preinstalled in the Lambda runtime. That keeps the deployment a plain .zip.
"""
import json
import os
import re
import time
import traceback
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from html.parser import HTMLParser

SUBSTACK_ARCHIVE_URL = "https://thecapillary.substack.com/api/v1/archive?sort=new&limit=50&offset=0"
SUBSTACK_POST_URL_TMPL = "https://thecapillary.substack.com/api/v1/posts/{slug}"

S3_BUCKET = os.environ.get("DATA_BUCKET", "REPLACE_ME")
S3_DATA_KEY = "data.json"
SEEN_SLUGS_KEY = "seen_slugs.json"
PENDING_REVIEW_KEY = "pending_review.json"
LOGS_KEY = "logs.json"
MAX_LOG_ENTRIES = 300  # oldest entries drop off once this cap is hit, so the file can't grow unbounded

USER_AGENT = "Mozilla/5.0 (compatible; CapillaryTracker/1.0)"

# Names of the SSM SecureString parameters that hold the API keys (set by deploy.sh).
GEMINI_PARAM = os.environ.get("GEMINI_PARAM", "/capillary/gemini-api-key")
TWELVEDATA_PARAM = os.environ.get("TWELVEDATA_PARAM", "/capillary/twelvedata-api-key")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

# Gemini free tier caps requests-per-minute (10-15 RPM), not just requests-per-day.
# Volume is never the risk here (even reclassifying every tracked stock daily is
# ~2% of the 1,500/day cap) — bursting past the per-minute ceiling is. Classification
# calls MUST run sequentially with this delay between them, never in parallel
# (no asyncio.gather / thread pool / batch-all-at-once). A full run finishing in
# 60-90 seconds instead of 5 costs nothing on a daily cron job.
GEMINI_CALL_DELAY_SECONDS = int(os.environ.get("GEMINI_CALL_DELAY_SECONDS", "6"))
# Only classify a few posts per run so a burst of new posts can never blow the free-tier
# per-minute / per-day request cap. Anything beyond this waits for the next daily run
# (steady state is ~1-4 new posts/week, far under the cap).
MAX_CLASSIFY_PER_RUN = int(os.environ.get("MAX_CLASSIFY_PER_RUN", "5"))
# Truncate post text so each call stays well under the free-tier input-tokens-per-minute
# cap. The thesis is almost always within the first few thousand characters.
POST_TEXT_LIMIT = int(os.environ.get("POST_TEXT_LIMIT", "4000"))

# Twelve Data free tier caps 8 requests/minute (and 800/day). Same reasoning as above:
# pace the per-ticker price calls sequentially. 60/8 = 7.5s, rounded up to 8s.
PRICE_CALL_DELAY_SECONDS = int(os.environ.get("PRICE_CALL_DELAY_SECONDS", "8"))

# Yahoo fallback suffix by exchange, for tickers Twelve Data's free tier can't resolve.
# Kept in sync with docs/TICKER_MAP.md. Special-cased symbols that needed manual
# resolution live in TICKER_OVERRIDES.
YAHOO_SUFFIX = {"NSE": ".NS", "BSE": ".BO", "NASDAQ": "", "NYSE": ""}
TICKER_OVERRIDES = {
    # ticker -> full Yahoo symbol (when the plain ticker+suffix rule doesn't resolve)
    "AIRFLOA": "AIRFLOA.BO",  # NSE symbol didn't resolve; BSE did
}


# ---------------------------------------------------------------------------
# Logging — every error/warning anywhere in this file should go through log_event()
# so it ends up visible in the site's hidden "System Diagnostics" panel
# (public/index.html), not just in CloudWatch where you'd have to go looking for it.
# ---------------------------------------------------------------------------

def log_event(s3_client, level, source, message, context=None):
    """
    level:   "error" | "warning" | "info"
    source:  "substack_archive" | "substack_post" | "gemini_classify" |
             "price_refresh" | "s3_write" | "run_summary"
    context: small dict of extra detail (slug, ticker, http_status, exception text, ...)
             Keep this small — it gets rendered directly in the frontend panel.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "source": source,
        "message": message,
        "context": context or {},
    }

    try:
        try:
            obj = s3_client.get_object(Bucket=S3_BUCKET, Key=LOGS_KEY)
            logs = json.loads(obj["Body"].read())
        except Exception:
            logs = []

        logs.append(entry)
        logs = logs[-MAX_LOG_ENTRIES:]  # keep only the most recent N

        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=LOGS_KEY,
            Body=json.dumps(logs).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception:
        # Logging must never crash the actual run. If S3 itself is down, the only
        # fallback is CloudWatch's own automatic capture of stdout.
        print("FAILED TO WRITE LOG ENTRY:", json.dumps(entry))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_secret_cache = {}


def get_secret(ssm_client, param_name):
    """Read a SecureString from SSM Parameter Store, cached per cold start."""
    if param_name in _secret_cache:
        return _secret_cache[param_name]
    resp = ssm_client.get_parameter(Name=param_name, WithDecryption=True)
    value = resp["Parameter"]["Value"]
    _secret_cache[param_name] = value
    return value


def fetch_json(url, data=None, headers=None, timeout=20):
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class _TextExtractor(HTMLParser):
    """Collapses HTML to readable plain text, skipping script/style content."""

    def __init__(self):
        super().__init__()
        self._chunks = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4"):
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._chunks.append(data)

    def text(self):
        raw = "".join(self._chunks)
        # collapse runs of blank lines / spaces
        lines = [ln.strip() for ln in raw.splitlines()]
        lines = [ln for ln in lines if ln]
        return "\n".join(lines)


def strip_html(html):
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
    except Exception:
        return html or ""
    return parser.text()


# ---------------------------------------------------------------------------
# Substack
# ---------------------------------------------------------------------------

def get_archive():
    return fetch_json(SUBSTACK_ARCHIVE_URL)


def get_post_body(slug):
    data = fetch_json(SUBSTACK_POST_URL_TMPL.format(slug=slug))
    return data.get("body_html", "")


def get_seen_slugs(s3_client):
    """Returns (seen_slugs_set, file_existed_bool)."""
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET, Key=SEEN_SLUGS_KEY)
        return set(json.loads(obj["Body"].read())), True
    except Exception:
        return set(), False


def save_seen_slugs(s3_client, slugs):
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=SEEN_SLUGS_KEY,
        Body=json.dumps(sorted(slugs)).encode("utf-8"),
        ContentType="application/json",
    )


def load_data(s3_client):
    """The curated tracker content (stocks/models/etc). Source of truth for prices."""
    obj = s3_client.get_object(Bucket=S3_BUCKET, Key=S3_DATA_KEY)
    return json.loads(obj["Body"].read())


def save_data(s3_client, data):
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=S3_DATA_KEY,
        Body=json.dumps(data, indent=2).encode("utf-8"),
        ContentType="application/json",
        CacheControl="no-cache",
    )


# ---------------------------------------------------------------------------
# Classification (Gemini)
# ---------------------------------------------------------------------------

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "ticker": {"type": "string"},
        "exch": {"type": "string"},
        "name": {"type": "string"},
        "sector": {"type": "string"},
        "sentiment": {"type": "string", "enum": ["bull", "bear", "neutral"]},
        "modelShort": {"type": "string"},
        "postDate": {"type": "string"},
        "postPrice": {"type": "number", "nullable": True},
        "entryNote": {"type": "string"},
        "thesis": {"type": "string"},
        "outlook": {"type": "string"},
    },
    "required": ["ticker", "name", "sentiment", "modelShort", "thesis", "outlook"],
}


def _classify_prompt(post_title, post_text, model_names):
    models_block = "\n".join(f"- {n}" for n in model_names)
    text = (post_text or "")[:POST_TEXT_LIMIT]
    return (
        "You are classifying a stock-analysis blog post from an Indian/US markets "
        "newsletter into a structured tracker entry. Read the post and extract:\n"
        "- ticker: the primary stock's exchange ticker symbol (uppercase, no suffix). "
        "Indian stocks use their NSE symbol where possible.\n"
        "- exch: one of NSE, BSE, NASDAQ, NYSE.\n"
        "- name: the full company name.\n"
        "- sector: a short 'Region · Industry' label, e.g. 'India · Pharma'.\n"
        "- sentiment: 'bull', 'bear', or 'neutral' — the author's stance on the stock.\n"
        "- modelShort: the single mental-model framework that best matches the post's "
        "reasoning. Choose from EXACTLY one of these framework names:\n"
        f"{models_block}\n"
        "- postDate: the post's date if stated, else ''.\n"
        "- postPrice: the share price at the time of the post if stated, else null.\n"
        "- entryNote: a one-line note on the author's position/context.\n"
        "- thesis: a 2-4 sentence plain-language summary of the argument.\n"
        "- outlook: a one-line current stance.\n\n"
        "If the post is not about a single specific stock (e.g. it's a general market "
        "essay), still fill your best guess and set sentiment to 'neutral'.\n\n"
        f"POST TITLE: {post_title}\n\nPOST TEXT:\n{text}"
    )


def _retry_delay_seconds(detail):
    """Pull the suggested retryDelay (e.g. \"25s\") out of a Gemini 429 error body."""
    m = re.search(r'"retryDelay":\s*"(\d+)s"', detail or "")
    return int(m.group(1)) if m else None


def classify_post_with_gemini(ssm_client, post_title, post_text, model_names):
    """
    Calls the Gemini API (free tier) with the mental-model list embedded and returns
    a dict matching the stock object shape used in public/index.html / data.json.

    Raises on failure. If it raises due to a 429 (rate limit), the caller
    (classify_new_posts) detects it, logs level="error" source="gemini_classify"
    with rate_limited=true in context, and moves on — it does not abort the run.
    """
    api_key = get_secret(ssm_client, GEMINI_PARAM)
    url = GEMINI_URL_TMPL.format(model=GEMINI_MODEL, key=api_key)
    body = {
        "contents": [{"parts": [{"text": _classify_prompt(post_title, post_text, model_names)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": CLASSIFY_SCHEMA,
            "temperature": 0.2,
        },
    }
    data = json.dumps(body).encode("utf-8")

    def _attempt():
        try:
            return fetch_json(url, data=data, headers={"Content-Type": "application/json"}, timeout=40)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                pass
            if e.code == 429:
                raise RateLimited(f"429 {detail[:300]}", _retry_delay_seconds(detail))
            raise RuntimeError(f"{e.code} {e.reason} {detail[:300]}")

    try:
        resp = _attempt()
    except RateLimited as e:
        # Honor the API's suggested retry delay once (bounded), then let it propagate so
        # the caller logs it and this post is retried on the next run.
        time.sleep(min(e.retry_after or 20, 30))
        resp = _attempt()

    candidates = resp.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"no candidates in Gemini response: {json.dumps(resp)[:300]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    raw = "".join(p.get("text", "") for p in parts)
    return json.loads(raw)


def classify_new_posts(s3_client, ssm_client, new_posts, get_post_body_fn, model_names):
    """
    Classifies each new post one at a time, with a fixed delay between calls, to stay
    under Gemini's free-tier requests-per-minute cap. Do NOT parallelize this loop
    (no asyncio.gather, no thread pool) — see the module-level comment on
    GEMINI_CALL_DELAY_SECONDS and ARCHITECTURE.md's "Rate limits" section for why.

    A failure on one post is logged and skipped — it does not stop the rest of the
    batch or the price-refresh step later in lambda_handler.

    Returns a list of draft stock entries for the pending-review queue.
    """
    batch = new_posts[:MAX_CLASSIFY_PER_RUN]  # cap per run; the rest wait for next run
    drafts = []
    for i, post in enumerate(batch):
        try:
            body_html = get_post_body_fn(post["slug"])
        except Exception as e:
            log_event(
                s3_client, "error", "substack_post",
                f"Failed to fetch post body for '{post['slug']}'",
                {"slug": post["slug"], "exception": str(e)},
            )
            continue

        plain_text = strip_html(body_html)

        try:
            draft = classify_post_with_gemini(ssm_client, post["title"], plain_text, model_names)
            draft["_source_slug"] = post["slug"]
            draft["_classified_at"] = datetime.now(timezone.utc).isoformat()
            draft["custom"] = False
            drafts.append(draft)
        except Exception as e:
            msg = str(e)
            is_rate_limit = "429" in msg or "RESOURCE_EXHAUSTED" in msg
            log_event(
                s3_client,
                "error",
                "gemini_classify",
                f"Classification failed for '{post['slug']}'"
                + (" (rate limited — check GEMINI_CALL_DELAY_SECONDS pacing)" if is_rate_limit else ""),
                {"slug": post["slug"], "exception": msg, "rate_limited": is_rate_limit,
                 "traceback": traceback.format_exc()[-800:]},
            )
            continue

        is_last = (i == len(batch) - 1)
        if not is_last:
            time.sleep(GEMINI_CALL_DELAY_SECONDS)

    return drafts


# ---------------------------------------------------------------------------
# Price refresh (Twelve Data primary, Yahoo Finance fallback)
# ---------------------------------------------------------------------------

class RateLimited(Exception):
    """Raised on a 429 free-tier limit (Twelve Data credits, or Gemini RPM/TPM/RPD).
    Carries the API's suggested retry delay in seconds when one was provided."""
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def twelvedata_price(symbol, exch, api_key):
    """Returns a float price, or None if Twelve Data has no data for this symbol.
    Raises RateLimited when the free-tier cap is hit so the caller can stop using it."""
    params = urllib.parse.urlencode({"symbol": symbol, "exchange": exch, "apikey": api_key})
    resp = fetch_json(f"https://api.twelvedata.com/price?{params}", timeout=20)
    # Success: {"price": "2583.60000"}. Error: {"code":..., "status":"error", "message":...}
    if isinstance(resp, dict) and resp.get("status") == "error":
        code = resp.get("code")
        msg = resp.get("message", "twelvedata error")
        if code == 429 or "run out" in msg.lower() or "credit" in msg.lower() or "limit" in msg.lower():
            raise RateLimited(f"{code} {msg}")
        raise RuntimeError(f"{code} {msg}")
    price = resp.get("price") if isinstance(resp, dict) else None
    if price in (None, ""):
        return None
    return float(price)


def yahoo_price(ticker, exch):
    """Undocumented Yahoo chart endpoint — the free fallback for tickers Twelve Data
    can't resolve on its free tier (notably some NSE/BSE names)."""
    symbol = TICKER_OVERRIDES.get(ticker) or (ticker + YAHOO_SUFFIX.get(exch, ""))
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1d&range=5d"
    resp = fetch_json(url, timeout=20)
    result = (resp.get("chart", {}).get("result") or [None])[0]
    if not result:
        return None
    price = result.get("meta", {}).get("regularMarketPrice")
    return float(price) if price is not None else None


def refresh_prices(s3_client, ssm_client, stocks):
    """
    Fetches a fresh current price for each stock, one at a time (rate-limit safe).
    Tries Twelve Data first; on failure/empty, falls back to Yahoo. A per-ticker
    failure is logged and the previous known price is kept (never overwritten with
    null), so one bad ticker can't blank the whole refresh.

    Returns {ticker: new_price} for the ones that actually updated.
    """
    try:
        td_key = get_secret(ssm_client, TWELVEDATA_PARAM)
    except Exception as e:
        log_event(s3_client, "warning", "price_refresh",
                  "No Twelve Data key available; using Yahoo fallback only",
                  {"exception": str(e)})
        td_key = None

    # Normal load (18 tickers, once/day) is nowhere near Twelve Data's 800/day cap; the
    # only real risk is the 8-requests/minute burst limit, which the pacing below respects.
    # If we DO get told we've hit the cap, we stop calling Twelve Data for the rest of this
    # run and use Yahoo only — so we never keep hammering a limit we've already hit.
    td_disabled = False
    updated = {}
    for i, s in enumerate(stocks):
        ticker, exch = s.get("ticker"), s.get("exch")
        if not ticker:
            continue
        price = None
        used_td = False
        try:
            if td_key and not td_disabled:
                used_td = True
                price = twelvedata_price(ticker, exch, td_key)
            if price is None:
                price = yahoo_price(ticker, exch)
        except RateLimited as e:
            td_disabled = True
            log_event(s3_client, "warning", "price_refresh",
                      "Twelve Data free-tier limit hit — switching to Yahoo for the rest of this run",
                      {"ticker": ticker, "exception": str(e)})
            try:
                price = yahoo_price(ticker, exch)
            except Exception as e2:
                log_event(s3_client, "warning", "price_refresh",
                          f"Price lookup failed for {ticker}, kept previous price",
                          {"ticker": ticker, "exch": exch, "exception": str(e2)})
                price = None
        except Exception as e:
            # Twelve Data failed for this symbol (not a rate limit) — try Yahoo before giving up.
            try:
                price = yahoo_price(ticker, exch)
            except Exception as e2:
                log_event(s3_client, "warning", "price_refresh",
                          f"Price lookup failed for {ticker}, kept previous price",
                          {"ticker": ticker, "exch": exch, "exception": f"{e} | {e2}"})
                price = None

        if price is None:
            log_event(s3_client, "warning", "price_refresh",
                      f"No price data for {ticker}, kept previous price",
                      {"ticker": ticker, "exch": exch})
        else:
            s["current"] = price
            updated[ticker] = price

        if i != len(stocks) - 1:
            # Full pace only when a Twelve Data call was actually made (its 8/min cap);
            # once we're Yahoo-only, a light 1s spacing is plenty.
            time.sleep(PRICE_CALL_DELAY_SECONDS if (used_td and not td_disabled) else 1)

    return updated


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    import boto3

    s3 = boto3.client("s3")
    ssm = boto3.client("ssm")

    # data.json is the source of truth for both the curated content (models list we
    # classify against) and the prices we refresh. Load it once up front.
    try:
        data = load_data(s3)
    except Exception as e:
        log_event(s3, "error", "s3_write",
                  "Could not load data.json — aborting this run",
                  {"exception": str(e)})
        return {"error": "data.json load failed", "detail": str(e)}

    model_names = [m.get("name") for m in data.get("models", []) if m.get("name")]

    # --- 1-3: detect + classify new posts -------------------------------------
    try:
        seen, seen_existed = get_seen_slugs(s3)
        archive = get_archive()
    except Exception as e:
        log_event(
            s3, "error", "substack_archive",
            "Failed to fetch Substack archive — skipping post detection this run",
            {"exception": str(e), "traceback": traceback.format_exc()[-800:]},
        )
        archive, seen, seen_existed = [], set(), True

    new_posts = [p for p in archive if p["slug"] not in seen]

    # First run ever: everything currently in the archive is already represented in the
    # curated data.json, so seed seen_slugs and DON'T re-classify the backlog. Gemini
    # classification only fires for posts published after this point.
    if not seen_existed and archive:
        seen.update(p["slug"] for p in archive)
        try:
            save_seen_slugs(s3, seen)
        except Exception as e:
            log_event(s3, "error", "s3_write", "Failed to seed seen_slugs.json", {"exception": str(e)})
        log_event(s3, "info", "run_summary",
                  f"First run: seeded {len(archive)} existing post(s), skipped backlog classification",
                  {"seeded": len(archive)})
        new_posts = []

    pending_review = []
    if new_posts:
        pending_review = classify_new_posts(s3, ssm, new_posts, get_post_body, model_names)

    if pending_review:
        # Merge with any existing pending queue so drafts aren't lost between runs.
        try:
            existing = json.loads(s3.get_object(Bucket=S3_BUCKET, Key=PENDING_REVIEW_KEY)["Body"].read())
        except Exception:
            existing = []
        existing_slugs = {d.get("_source_slug") for d in existing}
        existing.extend(d for d in pending_review if d.get("_source_slug") not in existing_slugs)
        try:
            s3.put_object(
                Bucket=S3_BUCKET, Key=PENDING_REVIEW_KEY,
                Body=json.dumps(existing, indent=2).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as e:
            log_event(s3, "error", "s3_write", "Failed to write pending_review.json",
                      {"exception": str(e)})

    # Mark only successfully-classified posts as seen, so posts that failed (rate limit,
    # transient error) or weren't reached this run (per-run cap) are retried next run
    # instead of being silently dropped.
    classified_slugs = {d.get("_source_slug") for d in pending_review}
    if classified_slugs:
        seen.update(classified_slugs)
        try:
            save_seen_slugs(s3, seen)
        except Exception as e:
            log_event(s3, "error", "s3_write", "Failed to write seen_slugs.json",
                      {"exception": str(e)})

    # --- 4-5: refresh prices + write data.json back ---------------------------
    # Price refresh is intentionally separate from classification above — it hits a
    # price API (Twelve Data / Yahoo), never Gemini, so it can't affect or be affected
    # by Gemini's rate limits regardless of how many tickers are tracked.
    updated = {}
    try:
        updated = refresh_prices(s3, ssm, data.get("stocks", []))
        data["pricesAsOf"] = datetime.now(timezone.utc).strftime("%b %d, %Y")
        save_data(s3, data)
    except Exception as e:
        log_event(s3, "error", "price_refresh",
                  "Price refresh / data.json write failed",
                  {"exception": str(e), "traceback": traceback.format_exc()[-800:]})

    log_event(
        s3, "info", "run_summary",
        f"Run complete: {len(new_posts)} new post(s), {len(pending_review)} classified, "
        f"{len(updated)} price(s) updated",
        {"new_posts": len(new_posts), "classified": len(pending_review),
         "remaining_unclassified": max(0, len(new_posts) - len(pending_review)),
         "prices_updated": len(updated)},
    )

    return {
        "new_posts_found": len(new_posts),
        "pending_review_count": len(pending_review),
        "prices_updated": len(updated),
    }
