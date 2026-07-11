"""
The Capillary — custom-stock write API.

A tiny CRUD handler behind a Lambda Function URL (no API Gateway — Function URLs are
free and Lambda's always-free tier covers the volume, so this whole feature is $0).
Backs the "+ Add Stock" feature in public/index.html with a DynamoDB table so custom
additions sync across devices instead of living in one browser's localStorage.

Routes (the site calls these same-origin via CloudFront's /api/* behavior):
    GET    /api/stocks            -> list all custom stocks
    POST   /api/stocks            -> upsert one (JSON body = a stock entry)
    DELETE /api/stocks/{ticker}   -> delete one

Table: PK `ticker` (String). Every item carries custom=true so the frontend renders
the "Yours" badge and delete affordance.

Note on auth: the Function URL is public (AuthType NONE) with CORS locked to the site
origin, which stops casual browser abuse but is not real authentication — anyone who
finds the URL could POST to it with curl. That's an accepted tradeoff for a personal
tracker with a tiny, non-sensitive, capped payload. To harden later, put Cognito or a
shared-secret header in front; see docs/DEPLOY.md.
"""
import json
import os
import decimal

import boto3

TABLE_NAME = os.environ.get("TABLE_NAME", "capillary-custom-stocks")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")

_table = boto3.resource("dynamodb").Table(TABLE_NAME)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json",
}


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            # DynamoDB stores all numbers as Decimal; emit ints/floats cleanly.
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": CORS_HEADERS,
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _method_and_path(event):
    # Lambda Function URL uses the API Gateway v2.0 payload shape.
    ctx = event.get("requestContext", {}).get("http", {})
    method = ctx.get("method") or event.get("httpMethod", "GET")
    path = event.get("rawPath") or ctx.get("path") or "/"
    if path.startswith("/api"):
        path = path[len("/api"):] or "/"
    return method.upper(), path


def lambda_handler(event, context):
    method, path = _method_and_path(event)

    try:
        if method == "OPTIONS":
            return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

        if method == "GET" and path in ("/stocks", "/stocks/"):
            items = _table.scan().get("Items", [])
            return _resp(200, items)

        if method == "POST" and path in ("/stocks", "/stocks/"):
            raw = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                import base64
                raw = base64.b64decode(raw).decode("utf-8")
            entry = json.loads(raw, parse_float=decimal.Decimal)
            ticker = (entry.get("ticker") or "").strip().upper()
            name = (entry.get("name") or "").strip()
            if not ticker or not name:
                return _resp(400, {"error": "ticker and name are required"})
            entry["ticker"] = ticker
            entry["name"] = name
            entry["custom"] = True
            _table.put_item(Item=entry)
            return _resp(200, {"ok": True, "ticker": ticker})

        if method == "DELETE" and path.startswith("/stocks/"):
            ticker = path[len("/stocks/"):].strip().upper()
            if not ticker:
                return _resp(400, {"error": "ticker required in path"})
            _table.delete_item(Key={"ticker": ticker})
            return _resp(200, {"ok": True, "ticker": ticker})

        return _resp(404, {"error": f"no route for {method} {path}"})
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON body"})
    except Exception as e:  # noqa: BLE001 — surface a clean error to the browser
        return _resp(500, {"error": str(e)})
