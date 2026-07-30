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
import re
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
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


# What each framework stage actually MEANS in this system — so the model applies the
# framework instead of writing generic commentary (the AAPL draft's biggest weakness).
STAGE_DEFINITIONS = {
    1:  "Compare where the BUSINESS is (fundamentals, growth trajectory, margins) against what the TICKER PRICE implies. Is the stock lagging an improving business, or leading a deteriorating one? Name the mismatch.",
    2:  "Who controls the company? Founder-led / dual-class super-voting / PE-flip / government-owned / owner-as-customer? Flag governance red flags. NOT generic 'institutions own X%'.",
    3:  "Split the expected return into EARNINGS GROWTH vs MULTIPLE RE-RATING. Is the return coming from a growing business, or just paying up for a higher multiple? The healthy case is earnings-led.",
    4:  "Identify the durable moat, then stress it against the MOST CREDIBLE attack (not the obvious one) — often procurement economics / bundling / a cheaper substitute, not a better product.",
    5:  "New or emerging risks that could break the thesis (regulation, customer concentration, demand air-pocket). Distinguish thesis-BREAKERS from mere dents.",
    6:  "How management communicates and allocates capital (buybacks / dividends / R&D / M&A). Look for discipline and honesty tells vs narrative-protection.",
    7:  "Is there a secular paradigm shift this name is on the right or wrong side of? Does it change the base rates for the whole thesis?",
    8:  "How big should the position be given the asymmetry — is downside bounded (a valuation/cash floor) or is it a growth name that de-rates hard? Quality-vs-entry.",
    9:  "Where is the margin of safety / the right entry? What specifically to avoid (e.g. chasing strength into a cyclical peak).",
    10: "Add on CONFIRMED evidence / thesis progress, not on price moves. Discipline of adding into confirmed demand, not into a dip that's really deterioration.",
    11: "The ONE continuously-updating variable to monitor. When the business state turns before the ticker state does, the thesis changes — tie it back to the exit trigger.",
}


def _yahoo_fundamentals(symbol):
    """Real fundamentals via Yahoo's quoteSummary (cookie + crumb dance). Returns a dict of
    ground-truth figures the model must not contradict, or {} if the dance fails (we then
    generate without grounding and flag it). Low-volume on-demand call, so the extra
    round-trips are fine."""
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", ua)]
    try:
        opener.open("https://fc.yahoo.com", timeout=10).read()
    except Exception:
        pass  # we only need the Set-Cookie; the body/status doesn't matter
    try:
        crumb = opener.open("https://query1.finance.yahoo.com/v1/test/getcrumb",
                            timeout=10).read().decode("utf-8").strip()
    except Exception:
        return {}
    if not crumb or len(crumb) > 40:
        return {}
    mods = "price,summaryDetail,defaultKeyStatistics,financialData,assetProfile"
    url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(symbol)}"
           f"?modules={mods}&crumb={urllib.parse.quote(crumb)}")
    try:
        resp = json.loads(opener.open(url, timeout=15).read().decode("utf-8"))
        r = (resp.get("quoteSummary", {}).get("result") or [None])[0]
    except Exception:
        return {}
    if not r:
        return {}
    sd, ks, fd, ap = (r.get("summaryDetail", {}), r.get("defaultKeyStatistics", {}),
                      r.get("financialData", {}), r.get("assetProfile", {}))

    def fmt(node, key):
        v = (node.get(key) or {})
        return v.get("fmt") if isinstance(v, dict) else None

    def raw(node, key):
        v = (node.get(key) or {})
        return v.get("raw") if isinstance(v, dict) else None

    return {
        "marketCap_fmt": fmt(sd, "marketCap"), "marketCap_raw": raw(sd, "marketCap"),
        "sharesOutstanding_raw": raw(ks, "sharesOutstanding"),
        "trailingPE_fmt": fmt(sd, "trailingPE"), "forwardPE_fmt": fmt(sd, "forwardPE"),
        "grossMargins_fmt": fmt(fd, "grossMargins"), "operatingMargins_fmt": fmt(fd, "operatingMargins"),
        "profitMargins_fmt": fmt(ks, "profitMargins"),
        "revenueGrowth_fmt": fmt(fd, "revenueGrowth"), "totalRevenue_fmt": fmt(fd, "totalRevenue"),
        "sector": ap.get("sector"), "industry": ap.get("industry"),
    }


def _facts_block(f):
    """Human-readable authoritative-figures block for the prompt (only the ones we got)."""
    rows = [
        ("Market cap", f.get("marketCap_fmt")),
        ("Trailing P/E", f.get("trailingPE_fmt")),
        ("Forward P/E", f.get("forwardPE_fmt")),
        ("Gross margin", f.get("grossMargins_fmt")),
        ("Operating margin", f.get("operatingMargins_fmt")),
        ("Net profit margin", f.get("profitMargins_fmt")),
        ("Revenue (TTM)", f.get("totalRevenue_fmt")),
        ("Revenue growth (YoY)", f.get("revenueGrowth_fmt")),
        ("Sector", f.get("sector")),
        ("Industry", f.get("industry")),
    ]
    return "\n".join(f"  - {k}: {v}" for k, v in rows if v)


def _stage_spec():
    lines = []
    for i, t in enumerate(STAGE_TITLES):
        n = i + 1
        cp = "  [HAS a checkpoint]" if n in CHECKPOINT_STAGES else ""
        lines.append(f"  {n}. {t}{cp}\n     — {STAGE_DEFINITIONS[n]}")
    return "\n".join(lines)


# A compact (single-stage) example of the expected depth + checkpoint shape. Kept tiny on
# purpose — Groq's free tier caps a request at 8000 tokens/min, so a full worked example
# won't fit alongside the framework spec. The stage DEFINITIONS carry the framework teaching.
_MINI_EXAMPLE = (
    '{"n":4,"t":"Stress-Test The Moat","m":[],'
    '"b":"The moat is the software ecosystem lock-in, not the hardware lead. The credible '
    'attack isn\'t a better product — it\'s cheaper total-cost-of-ownership from in-house '
    'silicon at the top customers, going around the moat rather than through it. Switching '
    'costs still bind the installed base, so the attack dents share of new workloads more '
    'than it breaks the franchise.",'
    '"cp":{"q":"Does the moat survive the real attack (procurement economics, not features)?",'
    '"outs":[["bull","HOLDS","Ecosystem lock-in intact for the core workload"],'
    '["bear","CRACK","Losing share of the fastest-growing segment"]]}}'
)


def _gen_prompt(ticker, name, exch, price_str, facts):
    cp_list = ", ".join(str(n) for n in sorted(CHECKPOINT_STAGES))
    facts_block = _facts_block(facts)
    grounding = (
        "\n\nAUTHORITATIVE FIGURES (REAL, fetched live — use these EXACT numbers; do NOT invent "
        "different ones for market cap, margins, P/E, or revenue):\n"
        f"{facts_block}\n"
        "If a figure isn't listed above, describe it qualitatively rather than inventing a number.\n"
    ) if facts_block else "\n\n(No live fundamentals available — avoid citing specific financial figures you cannot verify.)\n"

    return (
        f"You are a rigorous equity analyst applying a specific 11-stage framework to "
        f"{name} ({ticker}, {exch}), current price {price_str}."
        f"{grounding}"
        "\nProduce a JSON object ONLY (no prose) with keys: sector, sentiment, valuation, "
        "exitTrigger, stages.\n"
        "  sector      : short 'Region · Industry' label (use the sector/industry above if given).\n"
        "  sentiment   : 'bull', 'bear', or 'neutral'. MUST be internally consistent: if bearish, any "
        "price target you mention is BELOW the current price; if bullish, ABOVE.\n"
        "  valuation   : one sentence grounded in the authoritative multiples above.\n"
        "  exitTrigger : the ONE specific, observable BUSINESS event that would prove the call wrong "
        "(e.g. 'gross margin falls below X for two quarters', 'the top customer leaves'). "
        "It must NOT be a share-price level — 'the price rises above $X' is NOT acceptable.\n"
        "  stages      : EXACTLY 11 objects, in this order, each applying its DEFINITION:\n"
        f"{_stage_spec()}\n"
        "Each stage: n (1-11 int), t (exact title), m ([]), b (3-5 sentence analysis that applies "
        f"THIS stage's definition to THIS company using the real figures). cp ONLY on stages {cp_list}: "
        "{q, outs} where outs is 1-2 arrays [badgeClass('bull'|'bear'|'neutral'), "
        "label('PASS'|'HOLDS'|'CRACK'|'WATCH'), description]. Other stages have NO cp key.\n"
        f"Example of ONE stage at the right depth (different company — match the rigor, don't copy): {_MINI_EXAMPLE}\n"
        "Return ONLY the JSON object."
    )


class _JsonGenFailed(Exception):
    """Groq returned 400 'Failed to generate JSON' — retry on the bigger model."""


def _groq_json(api_key, prompt, temperature=0.4, max_tokens=6000):
    """One Groq JSON-mode call; falls back to the bigger model on a 429 or a JSON-gen
    failure. Returns parsed JSON."""
    def _call(model):
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_tokens,
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
            # A 400 "Failed to generate JSON" is the small model producing malformed output —
            # signal the caller to retry on the bigger model, which handles JSON mode better.
            if e.code == 400 and "generate json" in detail.lower():
                raise _JsonGenFailed(f"400 {detail[:200]}")
            raise RuntimeError(f"{e.code} {e.reason} {detail[:300]}")
        content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise RuntimeError("empty Groq response")
        return json.loads(content)

    try:
        return _call(scraper.GROQ_MODEL)
    except (scraper.RateLimited, _JsonGenFailed):
        return _call(scraper.GROQ_FALLBACK_MODEL)


def _generate_with_groq(ssm, ticker, name, exch, price_str, facts):
    api_key = scraper.get_secret(ssm, scraper.GROQ_PARAM)
    # Prompt is ~1.2k tokens, so 5000 max_tokens (≈6.2k total) stays under the 8000/min cap
    # while giving the 11-stage output enough room not to get truncated into invalid JSON.
    return _groq_json(api_key, _gen_prompt(ticker, name, exch, price_str, facts), max_tokens=5000)


def _verify_prompt(draft, facts, price_str, issues):
    """Compact, PATCH-style verify — sends only the fields that can be wrong plus the flagged
    stage bodies, and asks for a small correction object (not the whole draft). Keeps the call
    tiny enough to fit the free-tier per-minute token budget."""
    facts_block = _facts_block(facts) or "(none available)"
    # bodies of the stages that the guardrails flagged (plus 8 & 9 where targets live)
    want = set([8, 9])
    for w in issues:
        m = re.search(r"Stage (\d+)", w)
        if m:
            want.add(int(m.group(1)))
    stage_bodies = {s["n"]: s["b"] for s in draft.get("stages", []) if s.get("n") in want}
    return (
        "You are fact-checking an equity deep-dive. Current share price: " + price_str + ".\n"
        "Authoritative live figures (the analysis must not contradict these):\n" + facts_block + "\n\n"
        "Issues detected:\n" + ("\n".join("- " + i for i in issues) or "- (general consistency check)") + "\n\n"
        "Current values:\n"
        f"  sentiment: {draft.get('sentiment')}\n"
        f"  valuation: {draft.get('valuation')}\n"
        f"  exitTrigger: {draft.get('exitTrigger')}\n"
        f"  flagged stage bodies: {json.dumps(stage_bodies, ensure_ascii=False)}\n\n"
        "Return a JSON object with ONLY the fields that need correcting (omit anything already fine):\n"
        "  sentiment   : corrected stance if inconsistent.\n"
        "  valuation   : corrected sentence if it contradicts the figures.\n"
        "  exitTrigger : rewritten as an observable BUSINESS event if it was a price level.\n"
        "  stageFixes  : array of {n, b} — corrected body ONLY for stages that were wrong "
        "(fix upside/downside wording so it matches the target vs the current price).\n"
        "Return ONLY the JSON object."
    )


def _verify_with_groq(ssm, draft, facts, price_str, issues):
    """Best-effort compact patch. Returns {sentiment?, valuation?, exitTrigger?, stageFixes?}."""
    api_key = scraper.get_secret(ssm, scraper.GROQ_PARAM)
    return _groq_json(api_key, _verify_prompt(draft, facts, price_str, issues),
                      temperature=0.1, max_tokens=1500)


def _apply_patch(draft, patch):
    """Merge a verify patch into the draft in place."""
    if not isinstance(patch, dict):
        return draft
    if patch.get("sentiment") in ("bull", "bear", "neutral"):
        draft["sentiment"] = patch["sentiment"]
    if isinstance(patch.get("valuation"), str) and patch["valuation"].strip():
        draft["valuation"] = patch["valuation"].strip()
    if isinstance(patch.get("exitTrigger"), str) and patch["exitTrigger"].strip():
        draft["exitTrigger"] = patch["exitTrigger"].strip()
    for fix in (patch.get("stageFixes") or []):
        try:
            n = int(fix.get("n"))
        except (TypeError, ValueError):
            continue
        b = (fix.get("b") or "").strip()
        if not b:
            continue
        for s in draft.get("stages", []):
            if s.get("n") == n:
                s["b"] = b
    return draft


def _normalize(gen, ticker, name, exch, price_str, market_cap_hint=None):
    """Coerce the model output into the exact deepDives schema, forcing the header fields
    we looked up (never trust the model for price/name/ticker) and validating the stages."""
    # Groq occasionally returns a bare JSON array instead of the object we asked for —
    # recover the most likely intent rather than crashing on gen.get(...).
    if isinstance(gen, list):
        if gen and isinstance(gen[0], dict) and "stages" in gen[0]:
            gen = gen[0]                          # [ {sector, stages, ...} ]
        elif gen and isinstance(gen[0], dict) and "n" in gen[0]:
            gen = {"stages": gen}                 # the stages array itself
        else:
            gen = {}
    if not isinstance(gen, dict):
        gen = {}
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

    # Prefer the authoritative market cap (real figure / computed) over anything the model says.
    market_cap = market_cap_hint or (gen.get("marketCap") or "—")
    return {
        "ticker": ticker,
        "name": name,
        "exch": exch,
        "sector": (gen.get("sector") or "").strip() or "—",
        "sentiment": sentiment,
        "price": price_str,
        "marketCap": str(market_cap).strip(),
        "valuation": (gen.get("valuation") or "—").strip(),
        "exitTrigger": (gen.get("exitTrigger") or "—").strip(),
        "stages": stages,
    }


def _authoritative_marketcap(facts, price):
    """The market cap we trust: Yahoo's figure if present, else shares × price, else None."""
    if facts.get("marketCap_fmt"):
        return facts["marketCap_fmt"]
    shares = facts.get("sharesOutstanding_raw")
    if shares and price:
        val = shares * price
        for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
            if val >= div:
                return f"~${val / div:.2f}{suf}"
    return None


def _guardrails(draft, facts, price):
    """Deterministic sanity checks surfaced to the reviewer. Returns a list of warning strings.
    These flag likely errors; they don't block — the human decides at approve time."""
    warnings = []

    # 1. exit trigger should be a business event, not a price level
    et = (draft.get("exitTrigger") or "").lower()
    price_words = re.search(r"(share price|stock price|price (rises|falls|drops|climbs|reaches|hits|above|below)|rises? above|falls? below|\$\s?\d)", et)
    business_words = re.search(r"(margin|revenue|growth|customer|client|guidance|market share|churn|cash flow|earnings|debt|capex|contract|order)", et)
    if price_words and not business_words:
        warnings.append("Exit trigger looks price-based, not a business event — the framework wants an observable business change.")

    # 2. "upside"/"downside" wording that contradicts the target number vs the current price
    #    (catches e.g. "target $280 reflecting a 15% upside" when the price is $331).
    if price:
        for n in (8, 9):
            s = next((x for x in draft.get("stages", []) if x.get("n") == n), None)
            if not s:
                continue
            body = s.get("b", "")
            m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)", body)
            if not m:
                continue
            try:
                tgt = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            low = body.lower()
            if "upside" in low and tgt < price:
                warnings.append(f"Stage {n}: calls a ${tgt:,.0f} target 'upside' but it's below the current price (${price:,.2f}) — that's downside. Check the direction.")
            if "downside" in low and tgt > price:
                warnings.append(f"Stage {n}: calls a ${tgt:,.0f} target 'downside' but it's above the current price (${price:,.2f}) — that's upside. Check the direction.")

    # 3. did we ground it at all?
    if not _facts_block(facts):
        warnings.append("No live fundamentals were available for this ticker — financial figures are the model's own and unverified.")

    return warnings


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

    # DATA GROUNDING: fetch real fundamentals so the model can't invent margins/market cap.
    symbol = scraper.TICKER_OVERRIDES.get(ticker) or (ticker + scraper.YAHOO_SUFFIX.get(exch, ""))
    try:
        facts = _yahoo_fundamentals(symbol)
    except Exception:
        facts = {}
    market_cap = _authoritative_marketcap(facts, meta["price"])

    # generate (framework grounding via stage definitions + a compact inline example — a full
    # few-shot exemplar won't fit the free-tier 8000 tokens/min cap alongside the spec)
    try:
        gen = _generate_with_groq(ssm, ticker, meta["name"], exch, price_str, facts)
        draft = _normalize(gen, ticker, meta["name"], exch, price_str, market_cap_hint=market_cap)
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

    # GUARDRAILS first (deterministic, free).
    issues = _guardrails(draft, facts, meta["price"])

    # VERIFICATION PASS — only if guardrails flagged something, to stay inside the free-tier
    # per-minute token budget (two big calls in one minute would exceed it). Compact patch,
    # best-effort: on any failure keep the first draft.
    if issues:
        try:
            patch = _verify_with_groq(ssm, draft, facts, price_str, issues)
            draft = _apply_patch(draft, patch)
            issues = _guardrails(draft, facts, meta["price"])   # re-check after the fix
        except Exception:
            pass

    draft["_warnings"] = issues

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
