#!/usr/bin/env python3
"""
Roku Ads → Triple Whale spend import (Cloud Function, gen2).

Pulls spend, impressions and platform-reported conversions from the Roku Ads API
(async reporting) and upserts them to Triple Whale Data-In, so Roku CTV spend shows
up as a channel with a campaign → ad group → creative hierarchy.

Routes (matched on the path suffix, so they work behind the cloudfunctions.net proxy):
  GET       /                 help
  GET       /oauth/start      302 to the Roku authorization URL (one-time bootstrap)
  GET       /oauth/callback   exchange ?code= and store the refresh token in Secret Manager
  GET|POST  /run              import; header X-Run-Secret required
                              ?days=N | ?since=YYYY-MM-DD&until=YYYY-MM-DD, ?dry_run=1,
                              ?accounts=1 (list accounts), ?debug=1 (include the CSV head)

The function is deployed publicly because Roku must reach /oauth/callback, so /run
is gated by a shared secret instead of IAM.

CLI (offline): python spend-import/main.py --fixture tests/fixtures/roku_report.csv
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import sys
from pathlib import Path

try:
    import runtime
except ImportError:  # running from a checkout: shared modules live in ../lib
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import runtime
import roku
import tw_data_in as tw

try:
    import functions_framework
except ImportError:
    functions_framework = None

log = runtime.setup_logging("roku-ads-tw-import")

# Roku returns account.currency as a display name ("United States Dollar"); Triple Whale wants ISO.
_CURRENCY_ISO = {
    "united states dollar": "USD", "us dollar": "USD",
    "pound sterling": "GBP", "british pound": "GBP", "british pound sterling": "GBP",
    "euro": "EUR", "canadian dollar": "CAD", "australian dollar": "AUD",
}


def _secret(key: str) -> str:
    return runtime.secret(runtime.config()["gcp"]["secrets"][key])


def _html(body: str, status: int = 200):
    return (body, status, {"Content-Type": "text/html; charset=utf-8"})


# ---------------------------------------------------------------------------
# OAuth bootstrap
# ---------------------------------------------------------------------------

def _oauth_state() -> str:
    """CSRF state derived from the run secret, so the secret itself never enters a URL."""
    return hmac.new(_secret("run_secret").encode(), b"roku-oauth-state", hashlib.sha256).hexdigest()


def _oauth_start():
    url = (runtime.config()["roku"].get("authorize_url") or "").strip()
    if not url:
        return _html("<h3>Roku OAuth</h3><p>Set <code>roku.authorize_url</code> in config.yaml "
                     "(Ads Manager → Organization → Ads API → your app → Authorization URL) "
                     "and redeploy, or open that URL directly.</p>", 400)
    sep = "&" if "?" in url else "?"
    return ("", 302, {"Location": f"{url}{sep}state={_oauth_state()}"})


def _oauth_callback(request):
    if request.args.get("error"):
        return _html(f"<h3>Roku authorization failed</h3><pre>{request.args['error']}</pre>", 400)
    code = request.args.get("code")
    if not code:
        return _html("<h3>Missing ?code</h3>", 400)
    # Opening the Authorization URL straight from Ads Manager carries no state, so a
    # missing state is accepted; a wrong one is not.
    state = request.args.get("state")
    if state and not hmac.compare_digest(state, _oauth_state()):
        return _html("<h3>Invalid state</h3>", 403)

    cfg = runtime.config()
    tokens = roku.exchange_code(cfg["roku"]["token_url"], _secret("oauth_client_id"),
                                _secret("oauth_client_secret"), code, cfg["roku"]["redirect_uri"])
    if not tokens.get("refresh_token"):
        return _html("<h3>No refresh_token returned</h3><p>Revoke the app's access in Ads "
                     "Manager and run <code>/oauth/start</code> again.</p>", 400)
    runtime.write_secret(cfg["gcp"]["secrets"]["oauth_refresh_token"], tokens["refresh_token"])
    log.info("stored a new Roku refresh token")
    return _html("<h3>✅ Roku connected</h3><p>Refresh token stored. Trigger an import with "
                 "<code>/run</code>.</p>")


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _event_date(raw: str) -> str | None:
    """Roku's date dimension is MM/DD/YYYY; normalise to YYYY-MM-DD."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime((raw or "").strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _pick(row: dict, name: str) -> str:
    """Case-insensitive column lookup, tolerant of header drift."""
    return next((v for k, v in row.items() if k.lower() == name.lower()), "")


def iso_currency(raw: str | None, fallback: str) -> str:
    s = (raw or "").strip()
    if len(s) == 3 and s.isalpha():
        return s.upper()
    return _CURRENCY_ISO.get(s.lower(), fallback)


def to_records(rows: list[dict], since: str, until: str, cfg: dict,
               currency: str, channel_account_id: str) -> list[dict]:
    """Roku report rows → Data-In records, one per day × campaign × ad group × creative.

    Roku is CTV: there are no clicks or visits. Conversions come from the configured
    goal event's action_<event> / order_value_<event> columns.
    """
    t = cfg["triplewhale"]
    event = t.get("conversion_event") or ""
    agg: dict[tuple, dict] = {}
    skipped = 0
    for row in rows:
        day = _event_date(_pick(row, "date"))
        if not day:
            skipped += 1
            continue
        camp = _pick(row, "campaign_id") or "unknown"
        group = _pick(row, "ad_group_id") or "unknown"
        creative = _pick(row, "creative_id") or "unknown"
        a = agg.setdefault((day, camp, group, creative), {
            "campaign_name": _pick(row, "campaign_name") or camp,
            "creative_name": _pick(row, "creative_name") or creative,
            "spend": 0.0, "impressions": 0.0, "conversions": 0.0, "conversion_value": 0.0,
        })
        a["spend"] += _num(_pick(row, "spend"))
        a["impressions"] += _num(_pick(row, "impressions"))
        if event:
            a["conversions"] += _num(_pick(row, f"action_{event}"))
            a["conversion_value"] += _num(_pick(row, f"order_value_{event}"))
    if skipped:
        log.warning("skipped %d rows with an unparseable date", skipped)

    records = [
        tw.ad_record(
            shop=t["shop_id"], channel=t["channel"], channel_account_id=channel_account_id,
            event_date=day, currency=currency,
            campaign={"id": camp, "name": a["campaign_name"]},
            adset={"id": group, "name": f"Ad group {group}"},  # Roku exposes no ad group name
            ad={"id": creative, "name": a["creative_name"]},
            metrics={k: a[k] for k in ("spend", "impressions", "conversions", "conversion_value")},
        )
        for (day, camp, group, creative), a in agg.items()
    ]
    return tw.zero_fill(records, since, until)


# ---------------------------------------------------------------------------
# Import run
# ---------------------------------------------------------------------------

def _access_token() -> str:
    cfg = runtime.config()
    refresh_id = cfg["gcp"]["secrets"]["oauth_refresh_token"]
    try:
        refresh_token = runtime.secret(refresh_id, cache=False)
    except Exception as exc:
        raise PermissionError("no refresh token stored: open /oauth/start first") from exc
    tokens = roku.refresh_access_token(cfg["roku"]["token_url"], _secret("oauth_client_id"),
                                       _secret("oauth_client_secret"), refresh_token)
    # Roku may rotate the refresh token; persist the new one or the next run fails.
    if tokens.get("refresh_token") and tokens["refresh_token"] != refresh_token:
        runtime.write_secret(refresh_id, tokens["refresh_token"])
        log.info("rotated the Roku refresh token")
    return tokens["access_token"]


def _resolve_account(api_base: str, token: str) -> tuple[str | None, str, str]:
    """(account_uid, currency, channel_account_id)."""
    cfg = runtime.config()
    want = (cfg["roku"].get("account_uid") or "").strip()
    t = cfg["triplewhale"]
    try:
        accounts = roku.list_accounts(api_base, token)
    except roku.RokuError as exc:
        log.warning("list_accounts failed (%s); using config defaults", exc)
        return want or None, t["currency"], t["channel_account_id"]
    acct = next((a for a in accounts if a.get("uid") == want), None) if want else (accounts or [None])[0]
    if not acct:
        raise roku.RokuError(f"account {want!r} not found; available: {[a.get('uid') for a in accounts]}")
    attrs = acct.get("attributes") or {}
    return acct["uid"], iso_currency(attrs.get("currency"), t["currency"]), acct["uid"]


def _run(request):
    if not hmac.compare_digest(request.headers.get("X-Run-Secret", ""), _secret("run_secret")):
        return runtime.json_response({"error": "forbidden"}, 403)
    cfg = runtime.config()
    args = request.args
    try:
        days = int(args["days"]) if args.get("days") else None
        since, until = runtime.date_window(days, args.get("since"), args.get("until"),
                                           cfg["import"]["lookback_days"])
    except ValueError as exc:
        return runtime.json_response({"error": str(exc)}, 400)
    dry_run, debug = runtime.truthy(args.get("dry_run")), runtime.truthy(args.get("debug"))
    api_base = cfg["roku"]["api_base"]

    try:
        token = _access_token()
    except PermissionError as exc:
        return runtime.json_response({"error": str(exc)}, 428)
    except roku.RokuError as exc:
        return runtime.json_response({"error": f"token refresh failed: {exc}; open /oauth/start"}, 401)

    if runtime.truthy(args.get("accounts")):
        return runtime.json_response({"accounts": [
            {"uid": a.get("uid"), **(a.get("attributes") or {})}
            for a in roku.list_accounts(api_base, token)]})

    try:
        account_uid, currency, caid = _resolve_account(api_base, token)
        uid = roku.create_report(api_base, token, account_uid, f"{since}T00:00:00Z",
                                 f"{until}T23:59:59Z", f"tw-import {since}..{until}")
        url = roku.poll_report(api_base, token, uid, cfg["import"]["poll_timeout_s"],
                               cfg["import"]["poll_interval_s"])
        text = roku.download_report_text(url)
    except roku.RokuError as exc:
        return runtime.json_response({"error": f"report failed: {exc}"}, 502)

    rows = roku.parse_rows(text)
    records = to_records(rows, since, until, cfg, currency, caid)
    out = {"window": [since, until], "account_uid": account_uid, "currency": currency,
           "source_rows": len(rows), **tw.summarize(records), "dry_run": dry_run}
    if debug:
        out["csv_head"] = text[:1500]
    if dry_run or not records:
        out["sample"] = records[:3]
        return runtime.json_response(out)
    out["result"] = tw.send_records(records, _secret("triplewhale_api_key"))
    log.info("done: %s", {k: v for k, v in out.items() if k not in ("sample", "csv_head")})
    return runtime.json_response(out, 207 if out["result"]["failed"] else 200)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def spend_import(request):
    path = (request.path or "/").rstrip("/")
    try:
        if path.endswith("/oauth/callback"):
            return _oauth_callback(request)
        if path.endswith("/oauth/start"):
            return _oauth_start()
        if path.endswith("/run"):
            return _run(request)
    except Exception as exc:  # never leak a stack trace to the caller
        log.exception("unhandled error")
        return runtime.json_response({"error": str(exc)[:400]}, 500)
    return _html("<h3>Roku Ads → Triple Whale spend import</h3><ul>"
                 "<li><code>GET /oauth/start</code>: one-time connect</li>"
                 "<li><code>GET /oauth/callback</code>: OAuth redirect target</li>"
                 "<li><code>POST /run?dry_run=1</code>: import (X-Run-Secret header)</li></ul>")


if functions_framework:
    spend_import = functions_framework.http(spend_import)


def _cli() -> None:
    p = argparse.ArgumentParser(description="Map a saved Roku report CSV to Triple Whale records.")
    p.add_argument("--fixture", required=True, help="Roku report CSV")
    p.add_argument("--since")
    p.add_argument("--until")
    a = p.parse_args()
    cfg = runtime.config()
    rows = roku.parse_rows(Path(a.fixture).read_text())
    days = sorted(d for d in (_event_date(_pick(r, "date")) for r in rows) if d)
    since, until = a.since or days[0], a.until or days[-1]
    records = to_records(rows, since, until, cfg, cfg["triplewhale"]["currency"],
                         cfg["triplewhale"]["channel_account_id"])
    print(json.dumps({"window": [since, until], "source_rows": len(rows),
                      **tw.summarize(records), "tw_records": records}, indent=2))


if __name__ == "__main__":
    _cli()
