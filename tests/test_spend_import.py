from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import runtime
from conftest import load_function

main = load_function("spend-import")
import roku  # noqa: E402  (on sys.path once the function is loaded)
ROWS = roku.parse_rows((Path(__file__).parent / "fixtures" / "roku_report.csv").read_text())


def test_report_maps_to_records_with_goal_conversions():
    recs = main.to_records(ROWS, "2026-09-01", "2026-09-03", runtime.config(), "USD", "acct_1")
    # 2 creatives × 3 days (crv_3002 on 09-02 is zero-filled); the bad-date row is dropped
    assert len(recs) == 6
    first = next(r for r in recs if r["event_date"] == "2026-09-01" and r["ad"]["id"] == "crv_3001")
    assert first["metrics"] == {"spend": 1204.5, "impressions": 98012, "clicks": 0, "visits": 0,
                                "conversions": 3, "conversion_value": 267.0}
    assert first["adset"]["name"] == "Ad group grp_2001" and first["channel_account_id"] == "acct_1"
    filled = next(r for r in recs if r["event_date"] == "2026-09-02" and r["ad"]["id"] == "crv_3002")
    assert filled["metrics"]["spend"] == 0


def test_conversion_event_can_be_disabled():
    cfg = {**runtime.config(), "triplewhale": {**runtime.config()["triplewhale"], "conversion_event": ""}}
    recs = main.to_records(ROWS, "2026-09-01", "2026-09-03", cfg, "USD", "a")
    assert sum(r["metrics"]["conversions"] for r in recs) == 0


@pytest.mark.parametrize("raw,iso", [("United States Dollar", "USD"), ("gbp", "GBP"), ("Martian Credit", "USD"), (None, "USD")])
def test_iso_currency(raw, iso):
    assert main.iso_currency(raw, "USD") == iso


def test_run_requires_secret(monkeypatch):
    monkeypatch.setenv("ROKU_IMPORT_RUN_SECRET", "right")
    req = SimpleNamespace(path="/run", headers={"X-Run-Secret": "wrong"}, args={})
    assert main.spend_import(req)[1] == 403


def test_oauth_start_uses_derived_state_not_the_secret(monkeypatch):
    monkeypatch.setenv("ROKU_IMPORT_RUN_SECRET", "right")
    _, status, headers = main.spend_import(SimpleNamespace(path="/oauth/start", args={}, headers={}))
    assert status == 302 and "&state=" in headers["Location"] and "right" not in headers["Location"]


def test_oauth_callback_rejects_wrong_state(monkeypatch):
    monkeypatch.setenv("ROKU_IMPORT_RUN_SECRET", "right")
    req = SimpleNamespace(path="/oauth/callback", args={"code": "c", "state": "forged"}, headers={})
    assert main.spend_import(req)[1] == 403


def test_run_dry_run_end_to_end(monkeypatch):
    for k, v in {"ROKU_IMPORT_RUN_SECRET": "s", "ROKU_OAUTH_CLIENT_ID": "id",
                 "ROKU_OAUTH_CLIENT_SECRET": "cs", "ROKU_OAUTH_REFRESH_TOKEN": "rt"}.items():
        monkeypatch.setenv(k, v)
    text = (Path(__file__).parent / "fixtures" / "roku_report.csv").read_text()
    with mock.patch.object(roku, "refresh_access_token", return_value={"access_token": "at"}), \
         mock.patch.object(roku, "list_accounts", return_value=[{"uid": "acct_1", "attributes": {"currency": "United States Dollar"}}]), \
         mock.patch.object(roku, "create_report", return_value="rep_1"), \
         mock.patch.object(roku, "poll_report", return_value="https://csv"), \
         mock.patch.object(roku, "download_report_text", return_value=text):
        req = SimpleNamespace(path="/run", headers={"X-Run-Secret": "s"},
                              args={"since": "2026-09-01", "until": "2026-09-03", "dry_run": "1"})
        body, status, _ = main.spend_import(req)
    assert status == 200 and '"records": 6' in body and '"currency": "USD"' in body
