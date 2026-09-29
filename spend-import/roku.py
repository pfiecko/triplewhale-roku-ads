#!/usr/bin/env python3
"""
Thin Roku Ads API client: OAuth and async reporting.

Auth
----
OAuth2. First-time bootstrap uses grant_type=authorization_code (once, via the
browser → /oauth/callback). Every run thereafter uses grant_type=refresh_token
server-to-server. Access tokens live ~24h; the refresh token is persisted in
Secret Manager. Roku MAY rotate the refresh token on refresh — the caller writes
the returned one back when present.

Reporting
---------
Async: POST /reports (JSON:API) → poll GET /reports/{uid} until status=="done"
→ download the pre-signed CSV URL. Roku is CTV/impression-based: metrics have
spend + impressions but NO clicks.

Docs: https://developer.ads.roku.com/ads/reference/introduction
"""

from __future__ import annotations

import csv
import io
import logging
import time

import requests

log = logging.getLogger(__name__)

TIMEOUT = 30

# Report shape: campaign → ad group → creative, per day.
#   actions     = goal conversions (the campaign's goal action, e.g. Purchases)
#   order_value = purchase/transaction value passed by the pixel (drives ROAS in TW)
REPORT_METRICS = ["spend", "impressions", "actions", "order_value"]
REPORT_DIMENSIONS = [
    "date", "campaign_id", "campaign_name",
    "ad_group_id", "creative_id", "creative_name",
    "activity_name",
]


class RokuError(RuntimeError):
    pass


# ---- OAuth ----------------------------------------------------------------

def _token_request(token_url: str, payload: dict) -> dict:
    r = requests.post(token_url, json=payload,
                      headers={"Content-Type": "application/json",
                               "Accept": "application/json"},
                      timeout=TIMEOUT)
    if not r.ok:
        raise RokuError(f"token {payload.get('grant_type')} failed "
                        f"{r.status_code}: {r.text[:300]}")
    return r.json()


def exchange_code(token_url: str, client_id: str, client_secret: str,
                  code: str, redirect_uri: str) -> dict:
    """One-time bootstrap: authorization_code → tokens (incl. refresh_token)."""
    return _token_request(token_url, {
        "grant_type": "authorization_code",
        "client_id": client_id, "client_secret": client_secret,
        "code": code, "redirect_uri": redirect_uri,
    })


def refresh_access_token(token_url: str, client_id: str, client_secret: str,
                         refresh_token: str) -> dict:
    """Per-run: refresh_token → {access_token, [refresh_token], expires_in}."""
    return _token_request(token_url, {
        "grant_type": "refresh_token",
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token,
    })


# ---- API ------------------------------------------------------------------

def _auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}",
            "Accept": "application/json"}


def list_accounts(api_base: str, access_token: str) -> list[dict]:
    r = requests.get(f"{api_base}/accounts",
                     headers=_auth_headers(access_token), timeout=TIMEOUT)
    if not r.ok:
        raise RokuError(f"list accounts {r.status_code}: {r.text[:300]}")
    return r.json().get("data", [])


def create_report(api_base: str, access_token: str, account_uid: str,
                  start_at: str, end_at: str, name: str) -> str:
    """POST an async report; return its uid."""
    body = {"data": [{
        "type": "report",
        "attributes": {
            "name": name,
            "metrics": REPORT_METRICS,
            "dimensions": REPORT_DIMENSIONS,
            "filters": [{"metric": "account_uid", "value": account_uid}],
            "start_at": start_at,
            "end_at": end_at,
        },
    }]}
    r = requests.post(f"{api_base}/reports", json=body,
                      headers={**_auth_headers(access_token),
                               "Content-Type": "application/json"},
                      timeout=TIMEOUT)
    if not r.ok:
        raise RokuError(f"create report {r.status_code}: {r.text[:400]}")
    data = r.json().get("data", [])
    if not data:
        raise RokuError(f"create report returned no data: {r.text[:300]}")
    uid = data[0].get("uid")
    if not uid:
        raise RokuError(f"create report returned no uid: {r.text[:300]}")
    return uid


def get_report(api_base: str, access_token: str, uid: str) -> dict:
    r = requests.get(f"{api_base}/reports/{uid}",
                     headers=_auth_headers(access_token), timeout=TIMEOUT)
    if not r.ok:
        raise RokuError(f"get report {r.status_code}: {r.text[:300]}")
    data = r.json().get("data")
    if isinstance(data, list):
        data = data[0] if data else {}
    return data or {}


def _download_url(report: dict) -> str | None:
    """Pre-signed CSV URL lives in the report attributes (name varies slightly)."""
    attrs = report.get("attributes", report) or {}
    for k in ("download_url", "url", "result_url", "download", "file_url"):
        if attrs.get(k):
            return attrs[k]
    return None


def poll_report(api_base: str, access_token: str, uid: str,
                timeout_s: int, interval_s: int) -> str:
    """Poll until status==done; return the pre-signed CSV download URL."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        rep = get_report(api_base, access_token, uid)
        status = ((rep.get("attributes") or {}).get("status")
                  or rep.get("status") or "").lower()
        last = status
        if status == "done":
            url = _download_url(rep)
            if not url:
                raise RokuError(f"report {uid} done but no download URL: "
                                f"{str(rep)[:300]}")
            return url
        if status == "error":
            raise RokuError(f"report {uid} failed: {str(rep)[:300]}")
        time.sleep(interval_s)
    raise RokuError(f"report {uid} not ready within {timeout_s}s (last={last})")


def download_report_text(csv_url: str) -> str:
    """Fetch the pre-signed CSV once and return its raw text."""
    r = requests.get(csv_url, timeout=60)
    if not r.ok:
        raise RokuError(f"download CSV {r.status_code}: {r.text[:200]}")
    return r.content.decode("utf-8-sig", errors="replace")


def parse_rows(text: str) -> list[dict]:
    """Parse CSV text into a list of header→value dicts (trimmed)."""
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()}
            for row in reader]
