"""
The Capillary — on-demand Deep Dive generator (HTTP API + Lambda).

Triggered from the live site's "Add a Deep Dive" button (POST /deepdive on the
API Gateway HTTP API). Given a ticker it:

  1. Checks a shared passphrase (SSM SecureString) so the public endpoint can't be
     abused by randoms to burn the Groq quota or inject junk.
  2. Looks up the live price + company name via Yahoo's chart endpoint (same source
     the scraper falls back to) — NO manual entry.
  3. Calls Groq (free tier, OpenAI-compatible) to generate a full 11-stage deep-dive
     analysis matching the data.json["deepDives"] schema the UI renders.
  4. Appends the draft to pending_deepdives.json in S3 (the review queue — it is NOT
     published live). A human approves it with `infra/promote.sh deepdive-approve`.
  5. Returns the generated draft to the browser so it can be previewed immediately.

Reuses helpers from scraper.py (get_secret, fetch_json, twelvedata_price, yahoo_price,
log_event, GROQ_URL/GROQ_MODEL, S3 constants) — same deployment package, so there's no
duplicated price/Groq code. See ../CLAUDE.md and ../docs/ARCHITECTURE.md.
"""
import json
import os
import urllib.error
import urllib.parse
from datetime import datetime, timezone

import scraper  # same package; reuse its helpers + constants

# Name of the SSM SecureString holding the shared passphrase (set by deploy.sh).
DEEPDIVE_PASSPHRASE_PARAM = os.environ.get("DEEPDIVE_PASSPHRASE_PARAM", "/capillary/deepdive-passphrase")
PENDING_DEEPDIVES_KEY = "pending_deepdives.json"

# The 11 framework stages, in order — must match the Decision Framework / the two
# hand-authored deep dives so a generated one renders identically.
STAGE_TITLES = [
    "Business State vs. Ticker State",
    "Ownership Screen",
    "Decompose The Return",
    "Stress-Test The Moat",
    "Check For Fresh Cracks",
    "Read Management & Capital Allocation",
    "Paradigm-Shift Overlay",
    "Size The Conviction",
    "Plan The Entry",
    "Add Over Time, Not On Price",
    "Loop — Re-Run Stage 1 Continuously",
]
# Which stages carry a checkpoint (a decision gate with outcome badges), matching the
# style of the existing dives. The model fills the question + outcomes.
CHECKPOINT_STAGES = {2, 4, 5, 9}

VALID_EXCH = {"NASDAQ", "NYSE", "NSE", "BSE"}


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "content-type",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
        },
        "body": json.dumps(body),
    }


def _lookup_meta(ticker, exch):
    """One Yahoo chart call → {price, currency, name}. Authoritative header data so the
    model never invents the price/name. Returns None if the symbol can't be resolved."""
    symbol = scraper.TICKER_OVERRIDES.get(ticker) or (ticker + scraper.YAHOO_SUFFIX.get(exch, ""))
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?interval=1d&range=5d")
    resp = scraper.fetch_json(url, timeout=20)
    result = (resp.get("chart", {}).get("result") or [None])[0]
    if not result:
        return None
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice")
    if price is None:
        return None
    return {
        "price": float(price),
        "currency": meta.get("currency") or "",
        "name": meta.get("longName") or meta.get("shortName") or ticker,
    }


def _fmt_price(price, currency):
    sym = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}.get(currency, "")
    if sym:
        return f"{sym}{price:,.2f}"
    return f"{price:,.2f} {currency}".strip()


def _gen_prompt(ticker, name, exch, price_str):
    titles = "\n".join(f"  {i + 1}. {t}" for i, t in enumerate(STAGE_TITLES))
    cp_list = ", ".join(str(n) for n in sorted(CHECKPOINT_STAGES))
    return (
        f"You are an equity analyst writing a rigorous deep-dive on {name} ({ticker}, {exch}), "
        f"current price {price_str}. Produce a JSON object ONLY (no prose) with these keys:\n"
        "  sector      : short 'Region · Industry' label, e.g. 'US · Semiconductors'.\n"
        "  sentiment   : one of 'bull', 'bear', 'neutral' (your overall stance).\n"
        "  marketCap   : approximate market cap as a short string, e.g. '~$4.6T' or '~₹20,000 Cr'.\n"
        "  valuation   : one sentence on the valuation (multiples, PEG, what it implies).\n"
        "  exitTrigger : the ONE specific, observable thing that would prove the call wrong.\n"
        "  stages      : an array of EXACTLY 11 objects, in this order and with these exact titles:\n"
        f"{titles}\n"
        "Each stage object has:\n"
        "  n  : the stage number 1-11 (integer).\n"
        "  t  : the exact stage title from the list above.\n"
        "  m  : an empty array [].\n"
        "  b  : a substantive 3-6 sentence analysis paragraph for THIS company at THIS stage.\n"
        f"  cp : ONLY on stages {cp_list}, an object {{q, outs}} where q is the checkpoint "
        "question and outs is an array of 1-2 arrays [badgeClass, label, description]; "
        "badgeClass is 'bull', 'bear', or 'neutral'; label is a short verdict like 'PASS', "
        "'HOLDS', 'CRACK', 'WATCH'. Other stages have NO cp key.\n"
        "Be specific and quantitative where you can; ground it in the real business. "
        "Return ONLY the JSON object."
    )


def _generate_with_groq(ssm, ticker, name, exch, price_str):
    api_key = scraper.get_secret(ssm, scraper.GROQ_PARAM)
    prompt = _gen_prompt(ticker, name, exch, price_str)

    def _call(model):
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.4,
            "max_tokens": 6000,
        }).encode("utf-8")
        try:
            resp = scraper.fetch_json(
                scraper.GROQ_URL, data=body,
                headers={"Authorization": "Bearer " + api_key,
                         "Content-Type": "application/json"}, timeout=90)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                pass
            if e.code == 429:
                raise scraper.RateLimited(f"429 {detail[:300]}")
            raise RuntimeError(f"{e.code} {e.reason} {detail[:300]}")
        content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise RuntimeError("empty Groq response")
        return json.loads(content)

    try:
        return _call(scraper.GROQ_MODEL)
    except scraper.RateLimited:
        return _call(scraper.GROQ_FALLBACK_MODEL)


def _normalize(gen, ticker, name, exch, price_str, market_cap_hint=None):
    """Coerce the model output into the exact deepDives schema, forcing the header fields
    we looked up (never trust the model for price/name/ticker) and validating the stages."""
    stages_in = gen.get("stages") or []
    by_n = {}
    for s in stages_in:
        try:
            by_n[int(s.get("n"))] = s
        except (TypeError, ValueError):
            continue

    stages = []
    for i, title in enumerate(STAGE_TITLES):
        n = i + 1
        src = by_n.get(n, {})
        body = (src.get("b") or "").strip()
        if not body:
            raise ValueError(f"stage {n} ({title}) has no body")
        stage = {"n": n, "t": title, "m": [], "b": body}
        if n in CHECKPOINT_STAGES and isinstance(src.get("cp"), dict):
            cp = src["cp"]
            outs = []
            for o in (cp.get("outs") or []):
                if isinstance(o, (list, tuple)) and len(o) >= 3:
                    badge = o[0] if o[0] in ("bull", "bear", "neutral") else "neutral"
                    outs.append([badge, str(o[1]), str(o[2])])
            if cp.get("q") and outs:
                stage["cp"] = {"q": str(cp["q"]), "outs": outs}
        stages.append(stage)

    sentiment = gen.get("sentiment")
    if sentiment not in ("bull", "bear", "neutral"):
        sentiment = "neutral"

    return {
        "ticker": ticker,
        "name": name,
        "exch": exch,
        "sector": (gen.get("sector") or "").strip() or "—",
        "sentiment": sentiment,
        "price": price_str,
        "marketCap": (gen.get("marketCap") or market_cap_hint or "—").strip(),
        "valuation": (gen.get("valuation") or "—").strip(),
        "exitTrigger": (gen.get("exitTrigger") or "—").strip(),
        "stages": stages,
    }


def _read_pending(s3):
    try:
        obj = s3.get_object(Bucket=scraper.S3_BUCKET, Key=PENDING_DEEPDIVES_KEY)
        pending = json.loads(obj["Body"].read())
        return pending if isinstance(pending, list) else []
    except Exception:
        return []


def _write_pending(s3, pending):
    s3.put_object(
        Bucket=scraper.S3_BUCKET, Key=PENDING_DEEPDIVES_KEY,
        Body=json.dumps(pending, indent=2).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )


def _append_pending(s3, draft):
    pending = _read_pending(s3)
    # replace any existing draft for the same ticker so re-runs don't pile up
    pending = [d for d in pending if d.get("ticker") != draft["ticker"]]
    draft = dict(draft)
    draft["_generated_at"] = datetime.now(timezone.utc).isoformat()
    pending.append(draft)
    _write_pending(s3, pending)
    return draft


# --- action handlers -------------------------------------------------------

def _do_generate(s3, ssm, req):
    ticker = (req.get("ticker") or "").strip().upper()
    exch = (req.get("exch") or "NASDAQ").strip().upper()
    if not ticker:
        return _resp(400, {"error": "A ticker is required."})
    if exch not in VALID_EXCH:
        return _resp(400, {"error": f"exch must be one of {sorted(VALID_EXCH)}."})

    # API lookup (price + name) — no manual entry
    try:
        meta = _lookup_meta(ticker, exch)
    except Exception as e:
        return _resp(502, {"error": f"Price lookup failed: {e}"})
    if not meta:
        return _resp(404, {"error": f"Could not resolve {ticker} on {exch}. Check the symbol/exchange."})
    price_str = _fmt_price(meta["price"], meta["currency"])

    # generate with Groq
    try:
        gen = _generate_with_groq(ssm, ticker, meta["name"], exch, price_str)
        draft = _normalize(gen, ticker, meta["name"], exch, price_str)
    except scraper.RateLimited:
        return _resp(429, {"error": "Groq is rate-limited right now — try again in a minute."})
    except Exception as e:
        try:
            scraper.log_event(s3, "error", "deepdive_generate",
                              f"Deep-dive generation failed for {ticker}",
                              {"ticker": ticker, "exception": str(e)})
        except Exception:
            pass
        return _resp(502, {"error": f"Generation failed: {e}"})

    # save to review queue (NOT live)
    try:
        saved = _append_pending(s3, draft)
    except Exception as e:
        return _resp(500, {"error": f"Could not save draft: {e}"})

    return _resp(200, {
        "ok": True, "queued": True,
        "message": f"Generated a deep dive for {meta['name']} ({ticker}). "
                   "It's in the review queue — approve it to publish.",
        "draft": saved,
    })


def _do_approve(s3, req):
    """Move a queued draft into data.json["deepDives"] (live). data.json is served
    CachingDisabled at CloudFront, so no invalidation is needed — the next fetch is fresh."""
    ticker = (req.get("ticker") or "").strip().upper()
    if not ticker:
        return _resp(400, {"error": "A ticker is required."})

    pending = _read_pending(s3)
    draft = next((d for d in pending if (d.get("ticker") or "").upper() == ticker), None)
    if not draft:
        return _resp(404, {"error": f"No queued draft for {ticker}."})
    if len(draft.get("stages", [])) != 11:
        return _resp(422, {"error": f"Draft has {len(draft.get('stages', []))} stages, expected 11."})

    dd = {k: draft.get(k) for k in DEEPDIVE_FIELDS}   # strip internal _-keys
    dd["ticker"] = ticker

    try:
        data = scraper.load_data(s3)
    except Exception as e:
        return _resp(500, {"error": f"Could not load data.json: {e}"})
    data.setdefault("deepDives", [])
    data["deepDives"] = [x for x in data["deepDives"] if (x.get("ticker") or "").upper() != ticker]
    data["deepDives"].append(dd)

    try:
        scraper.save_data(s3, data)
        _write_pending(s3, [d for d in pending if (d.get("ticker") or "").upper() != ticker])
    except Exception as e:
        return _resp(500, {"error": f"Could not publish: {e}"})

    return _resp(200, {"ok": True, "published": True, "ticker": ticker,
                       "message": f"{ticker} published to Deep Dives."})


def _do_reject(s3, req):
    ticker = (req.get("ticker") or "").strip().upper()
    if not ticker:
        return _resp(400, {"error": "A ticker is required."})
    pending = _read_pending(s3)
    if not any((d.get("ticker") or "").upper() == ticker for d in pending):
        return _resp(404, {"error": f"No queued draft for {ticker}."})
    _write_pending(s3, [d for d in pending if (d.get("ticker") or "").upper() != ticker])
    return _resp(200, {"ok": True, "rejected": True, "ticker": ticker,
                       "message": f"{ticker} draft discarded."})


# Only these fields get published (the deepDives schema) — internal _-keys are dropped.
DEEPDIVE_FIELDS = ["ticker", "name", "exch", "sector", "sentiment", "price",
                   "marketCap", "valuation", "exitTrigger", "stages"]


def lambda_handler(event, context):
    import boto3

    http = (event.get("requestContext", {}).get("http", {}) or {})
    method = http.get("method", "")
    if method == "OPTIONS":
        return _resp(200, {"ok": True})

    path = (event.get("rawPath") or http.get("path") or "").rstrip("/")

    s3 = boto3.client("s3")
    ssm = boto3.client("ssm")

    # parse body
    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64
            raw = base64.b64decode(raw).decode("utf-8")
        req = json.loads(raw)
    except Exception:
        return _resp(400, {"error": "Invalid request body."})

    # passphrase gate (shared by every action)
    try:
        expected = scraper.get_secret(ssm, DEEPDIVE_PASSPHRASE_PARAM)
    except Exception:
        return _resp(500, {"error": "Endpoint not configured (no passphrase set)."})
    if not req.get("passphrase") or req.get("passphrase") != expected:
        return _resp(401, {"error": "Wrong or missing passphrase."})

    if path.endswith("/approve"):
        return _do_approve(s3, req)
    if path.endswith("/reject"):
        return _do_reject(s3, req)
    return _do_generate(s3, ssm, req)
