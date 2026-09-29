#!/usr/bin/env python3
"""
Roku Ads conversion proxy (Cloud Function, gen2, public HTTPS).

The Shopify Custom Pixel (../shopify-pixel/pixel.js) posts every purchase here.
Per request the function:
  1. checks the X-Pixel-Secret header,
  2. re-applies the optional currency/country filter,
  3. hashes the email, takes the buyer's IP and User-Agent from the request, and
  4. forwards a PURCHASE event to the Roku Conversions API. The bearer JWT stays
     in Secret Manager and never reaches the browser.

event_id is the Shopify order id, shared with the browser-side rkp PURCHASE event,
so Roku deduplicates the pair (10-minute window).

Roku CAPI reference: https://help.ads.roku.com/en/articles/8880744-conversions-api
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

try:
    import runtime
except ImportError:  # running from a checkout: shared modules live in ../lib
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import runtime
import pixel_proxy as pp

try:
    import functions_framework
except ImportError:
    functions_framework = None

log = runtime.setup_logging("roku-ads-conversion-proxy")


def _respond(body: dict, status: int = 200):
    return runtime.json_response(body, status, pp.CORS_HEADERS)


def build_event(payload: dict, ip: str | None, user_agent: str | None) -> dict | None:
    """Roku CAPI PURCHASE event, or None when there is no usable identifier."""
    user_data: dict = {}
    email = pp.normalize_email(payload.get("email"), drop_plus_alias=True)
    if email:
        user_data["em"] = pp.sha256_hex(email)
        user_data["is_hashed"] = True
    if ip:
        user_data["client_ip_address"] = ip
    if user_agent:
        user_data["client_user_agent"] = user_agent
    if payload.get("country"):
        user_data["country"] = str(payload["country"]).upper()
    if "em" not in user_data and "client_ip_address" not in user_data:
        return None  # Roku rejects events without an identifier

    custom_data: dict = {
        "value": float(payload.get("value") or 0),  # decimal amount, not cents
        "currency": str(payload.get("currency") or "USD").upper(),
        "order_id": str(payload["order_id"]),
    }
    if payload.get("contents"):
        custom_data["contents"] = payload["contents"]
        custom_data["content_type"] = "product"
    if payload.get("content_ids"):
        custom_data["content_ids"] = payload["content_ids"]
    if payload.get("num_items"):
        custom_data["num_items"] = int(payload["num_items"])

    # No partner_name: that field is for third parties sending on someone else's behalf.
    # Setting it on a first-party integration files events under a "partner" bucket
    # instead of connecting the direct CAPI source.
    return {
        "event_id": str(payload["event_id"]),
        "event_name": "PURCHASE",
        "event_type": "conversion",
        "event_time": int(time.time()),
        "event_source": "website",
        "event_source_url": payload.get("source_url") or "",
        "user_data": user_data,
        "custom_data": custom_data,
    }


def track(request):
    """POST from the Shopify Custom Pixel."""
    if request.method == "OPTIONS":
        return ("", 204, pp.CORS_HEADERS)
    if request.method != "POST":
        return _respond({"error": "method not allowed"}, 405)

    cfg = runtime.config()
    secrets, conv = cfg["gcp"]["secrets"], cfg["conversions"]
    if not pp.secret_matches(request.headers.get("X-Pixel-Secret"), runtime.secret(secrets["pixel_secret"])):
        log.warning("invalid X-Pixel-Secret from %s", request.remote_addr)
        return _respond({"error": "unauthorized"}, 401)

    payload = request.get_json(silent=True) or {}
    if not payload.get("event_id") or not payload.get("order_id"):
        return _respond({"error": "missing required fields: event_id, order_id"}, 400)
    if not pp.in_scope(payload, conv.get("allowed_currencies"), conv.get("allowed_countries")):
        log.info("out of scope: order_id=%s currency=%s country=%s",
                 payload["order_id"], payload.get("currency"), payload.get("country"))
        return _respond({"ok": True, "skipped": "out_of_scope"})

    event = build_event(payload, pp.client_ip(request),
                        request.headers.get("User-Agent") or payload.get("user_agent"))
    if event is None:
        log.warning("skipped order_id=%s: no identifier", payload["order_id"])
        return _respond({"ok": True, "skipped": "no_identifier"})

    test = bool(conv.get("use_test_endpoint"))
    url = conv["test_events_url"] if test else conv["events_url"]
    try:
        resp = requests.post(url, timeout=15, json={"event_group_id": conv["event_group_id"], "events": [event]},
                             headers={"Authorization": f"Bearer {runtime.secret(secrets['capi_token'])}",
                                      "Accept": "application/json", "Content-Type": "application/json"})
    except requests.RequestException as exc:
        log.error("Roku CAPI request failed: %s", exc)
        return _respond({"ok": False, "error": str(exc)[:300]}, 502)

    # 200 = all events passed, 210 = partial success. Anything else is surfaced as 502 so
    # rejections show up in request logs instead of hiding behind a 200.
    ok = 200 <= resp.status_code < 300
    (log.info if ok else log.error)("Roku CAPI %s %d event_id=%s value=%.2f %s: %s",
                                    "TEST" if test else "PROD", resp.status_code, event["event_id"],
                                    event["custom_data"]["value"], event["custom_data"]["currency"],
                                    resp.text[:300])
    return _respond({"ok": ok, "roku": {"status": resp.status_code, "body": resp.text[:300]}},
                    200 if ok else 502)


if functions_framework:
    track = functions_framework.http(track)
