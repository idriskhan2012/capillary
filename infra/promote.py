#!/usr/bin/env python3
"""
Promote a Gemini-classified draft from pending_review.json into the live data.json,
fully wiring the cross-links the UI depends on:

  1. adds the stock to data.json["stocks"]
  2. tags it onto its mental model  -> appends the ticker to that model's tickers[]
     (this is the reverse link that powers a model's ticker chips AND a stock modal's
     "Mental Models Behind This Call")
  3. adds the "read the original post" link -> data.json["stockPosts"][ticker]
  4. removes the draft from pending_review.json

Operates on LOCAL json files; infra/promote.sh handles the S3 download/upload + cache
invalidation around it. Pure stdlib (no boto3) so it runs with the system Python.

Usage (normally via promote.sh, but usable directly):
  python3 promote.py list    --pending pending_review.json
  python3 promote.py approve --data data.json --pending pending_review.json --slug <slug> [--model "Model Name"]
  python3 promote.py reject  --pending pending_review.json --slug <slug>
"""
import argparse
import json
import re
import sys

STOCK_FIELDS = ["ticker", "exch", "name", "sector", "sentiment", "modelShort",
                "postDate", "postPrice", "entryNote", "thesis", "outlook"]


def load(path):
    with open(path) as f:
        return json.load(f)


def save(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def norm(s):
    """Normalize a model name for fuzzy matching: lowercase, drop 'the', strip non-alnum."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\bthe\b", " ", s)
    return re.sub(r"\s+", "", s)


def find_model(model_name, models):
    """Match a (possibly loose) model label to exactly one model. Returns model dict or None."""
    dn = norm(model_name)
    if not dn:
        return None
    for m in models:                                   # exact normalized match
        if norm(m.get("name")) == dn:
            return m
    cands = [m for m in models                          # substring either direction
             if norm(m.get("name")) and (norm(m["name"]) in dn or dn in norm(m["name"]))]
    return cands[0] if len(cands) == 1 else None


def find_draft(pending, slug):
    return next((d for d in pending if d.get("_source_slug") == slug), None)


def cmd_list(args):
    pending = load(args.pending)
    if not pending:
        print("pending_review.json is empty — nothing to promote.")
        return 0
    print(f"{len(pending)} draft(s) awaiting review:\n")
    for i, d in enumerate(pending):
        print(f"[{i}] {d.get('ticker','?'):12} {d.get('sentiment','?'):8} "
              f"model={d.get('modelShort','?')!r}")
        print(f"     name : {d.get('name','?')}")
        print(f"     slug : {d.get('_source_slug','?')}")
        print(f"     thesis: {(d.get('thesis','') or '')[:120]}")
        print()
    return 0


def cmd_reject(args):
    pending = load(args.pending)
    if not find_draft(pending, args.slug):
        print(f"No draft with slug {args.slug!r}.", file=sys.stderr)
        return 1
    save(args.pending, [d for d in pending if d.get("_source_slug") != args.slug])
    print(f"Rejected and removed draft {args.slug!r} from the pending queue.")
    return 0


def cmd_approve(args):
    data = load(args.data)
    pending = load(args.pending)
    draft = find_draft(pending, args.slug)
    if not draft:
        print(f"No draft with slug {args.slug!r}. Run 'list' to see options.", file=sys.stderr)
        return 1

    ticker = (args.ticker or draft.get("ticker") or "").strip().upper()
    if not ticker:
        print("Draft has no ticker — pass --ticker <SYMBOL>.", file=sys.stderr)
        return 1

    # 1. build + upsert the stock
    stock = {k: draft.get(k) for k in STOCK_FIELDS}
    stock["ticker"] = ticker
    stock["custom"] = False
    data["stocks"] = [s for s in data.get("stocks", []) if s.get("ticker") != ticker]
    data["stocks"].append(stock)

    # 2. tag the ticker onto its mental model (the reverse cross-link)
    model_label = args.model or draft.get("modelShort")
    model = find_model(model_label, data.get("models", []))
    if not model:
        print(f"Could not match model {model_label!r} to a single mental model.\n"
              f"Re-run with --model set to one of:", file=sys.stderr)
        for m in data.get("models", []):
            print(f'  --model "{m["name"]}"', file=sys.stderr)
        return 2
    model.setdefault("tickers", [])
    if ticker not in model["tickers"]:
        model["tickers"].append(ticker)

    # 3. add the "read the original post" link
    slug = draft.get("_source_slug")
    if slug:
        title = draft.get("_source_title") or f'{draft.get("name", ticker)}'
        data.setdefault("stockPosts", {})[ticker] = [{"title": title, "slug": slug}]

    # 4. drain the draft
    save(args.data, data)
    save(args.pending, [d for d in pending if d.get("_source_slug") != args.slug])

    print(f"Promoted {ticker} ({stock.get('name')}):")
    print(f"  + added to stocks[] (sentiment={stock.get('sentiment')})")
    print(f"  + tagged onto model: {model['name']}  (tickers now: {model['tickers']})")
    if slug:
        print(f"  + post link: {title}")
    print("  - removed from pending_review")
    print("\nUpload the updated data.json + pending_review.json (promote.sh does this for you).")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list"); p.add_argument("--pending", required=True)
    p = sub.add_parser("reject"); p.add_argument("--pending", required=True); p.add_argument("--slug", required=True)
    p = sub.add_parser("approve")
    p.add_argument("--data", required=True)
    p.add_argument("--pending", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--model", help="override the mental model to tag (exact name)")
    p.add_argument("--ticker", help="override the ticker symbol (e.g. if the classifier got it wrong)")

    args = ap.parse_args()
    return {"list": cmd_list, "reject": cmd_reject, "approve": cmd_approve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
