"""
Triple Whale Data-In Ads API client: build ad records and upsert them.

VENDORED: this file is identical in triplewhale-shop-campaigns, triplewhale-roku-ads
and triplewhale-openai-ads. Change it in one place and copy it to the others.
lib-version: 1.0.0

Endpoint: POST https://api.triplewhale.com/api/v2/data-in/ads  (header x-api-key,
scope "Ads: Write"). Docs: https://triplewhale.readme.io/reference/create-ad-record

Things worth knowing before you send anything:

- The upsert key is shop + channel + channel_account_id + event_date + campaign.id
  + adset.id + ad.id. Re-sending a row overwrites it, so importers re-send a
  trailing window every run to pick up restatements.
- Never change how ids are derived once data is live, and never drop the ad
  level: a different key makes Triple Whale keep a second copy of every row.
- `metrics` accepts only clicks, conversions, conversion_value, impressions,
  spend and visits.
- A 200 response does not mean the row is queryable yet: Data-In rows reach
  `ads_table` asynchronously, minutes to hours later.
- The endpoint takes one record per request and drops connections above roughly
  ten concurrent requests.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

TW_ADS_URL = "https://api.triplewhale.com/api/v2/data-in/ads"
METRIC_KEYS = ("spend", "impressions", "clicks", "visits", "conversions", "conversion_value")
DEFAULT_WORKERS = 8


def stable_id(prefix: str, *parts: str) -> str:
    """Deterministic id for sources that expose names but no ids."""
    key = "|".join(p.strip().lower() for p in parts)
    return f"{prefix}_{hashlib.sha1(key.encode()).hexdigest()[:12]}"


def _entity(entity: dict, default_status: str) -> dict:
    out = {"id": str(entity["id"]), "name": str(entity.get("name") or entity["id"])[:300],
           "status": entity.get("status") or default_status}
    if entity.get("ad_image_url"):
        out["ad_image_url"] = entity["ad_image_url"]
    return out


def ad_record(*, shop: str, channel: str, channel_account_id: str, event_date: str,
              currency: str, campaign: dict, adset: dict, ad: dict, metrics: dict,
              default_status: str = "ACTIVE") -> dict:
    """One Data-In ad record. Missing metrics are sent as 0; campaign/adset/ad take id, name, status."""
    m = {k: metrics.get(k) or 0 for k in METRIC_KEYS}
    for k in ("impressions", "clicks", "visits", "conversions"):
        m[k] = int(round(float(m[k])))
    for k in ("spend", "conversion_value"):
        m[k] = round(float(m[k]), 2)
    return {
        "shop": shop,
        "channel": channel,
        "channel_account_id": str(channel_account_id),
        "event_date": event_date,
        "currency": currency,
        "campaign": _entity(campaign, default_status),
        "adset": _entity(adset, default_status),
        "ad": _entity(ad, default_status),
        "metrics": m,
    }


def upsert_key(rec: dict) -> tuple:
    return (rec["shop"], rec["channel"], rec["channel_account_id"], rec["event_date"],
            rec["campaign"]["id"], rec["adset"]["id"], rec["ad"]["id"])


def zero_fill(records: list[dict], since: str, until: str) -> list[dict]:
    """Add zero-metric rows so restatements down to 0 overwrite earlier values.

    Every campaign/adset/ad seen in the window gets a row for each day from `since`
    up to the latest day that has data (not beyond `until`). Days after the latest
    reported day are left alone, so a source that has not caught up yet does not
    get zeros written over data that is still arriving.
    """
    if not records:
        return []
    by_key = {upsert_key(r): r for r in records}
    templates: dict[tuple, dict] = {}
    for r in records:
        templates.setdefault((r["campaign"]["id"], r["adset"]["id"], r["ad"]["id"]), r)
    last_day = min(until, max(r["event_date"] for r in records))
    d0, d1 = dt.date.fromisoformat(since), dt.date.fromisoformat(last_day)
    for i in range((d1 - d0).days + 1):
        day = (d0 + dt.timedelta(days=i)).isoformat()
        for tpl in templates.values():
            filled = {**tpl, "event_date": day, "metrics": {k: 0 for k in METRIC_KEYS}}
            by_key.setdefault(upsert_key(filled), filled)
    return sorted(by_key.values(), key=lambda r: (r["event_date"], upsert_key(r)))


def summarize(records: list[dict]) -> dict:
    return {
        "records": len(records),
        "spend": round(sum(r["metrics"]["spend"] for r in records), 2),
        "impressions": sum(r["metrics"]["impressions"] for r in records),
        "conversions": sum(r["metrics"]["conversions"] for r in records),
        "conversion_value": round(sum(r["metrics"]["conversion_value"] for r in records), 2),
    }


def _session(workers: int) -> requests.Session:
    retry = Retry(
        total=5, connect=5, read=5, backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["POST"]),  # upserts are idempotent, so retrying is safe
        respect_retry_after_header=True, raise_on_status=False,
    )
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=workers,
                                    pool_maxsize=workers))
    return s


def send_records(records: list[dict], api_key: str, *, workers: int = DEFAULT_WORKERS,
                 url: str = TW_ADS_URL) -> dict:
    """Upsert records concurrently. Returns counts plus the first few failures."""
    headers = {"x-api-key": api_key, "Content-Type": "application/json",
               "Accept": "application/json"}
    session = _session(workers)

    def post(rec: dict):
        where = {"event_date": rec["event_date"], "campaign": rec["campaign"]["id"],
                 "ad": rec["ad"]["id"]}
        try:
            r = session.post(url, headers=headers, json=rec, timeout=30)
        except requests.RequestException as exc:
            return {**where, "error": str(exc)[:200]}
        return None if r.ok else {**where, "status": r.status_code, "body": r.text[:200]}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        failures = [f for f in pool.map(post, records) if f]
    if failures:
        log.error("Triple Whale: %d/%d records failed; first: %s",
                  len(failures), len(records), failures[0])
    return {"sent": len(records) - len(failures), "failed": len(failures),
            "failures": failures[:10]}
