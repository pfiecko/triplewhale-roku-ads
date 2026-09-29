# VENDORED with lib/tw_data_in.py: identical in every triplewhale-* repo.
from unittest import mock

import tw_data_in as tw


def _rec(day, ad="a1", spend=10.0):
    return tw.ad_record(shop="s", channel="c", channel_account_id="acc", event_date=day,
                        currency="USD", campaign={"id": "c1", "name": "Camp"},
                        adset={"id": "g1"}, ad={"id": ad, "name": "Ad"},
                        metrics={"spend": spend, "clicks": 3})


def test_ad_record_fills_every_metric_and_types_them():
    r = _rec("2026-01-01", spend=10.456)
    assert r["metrics"] == {"spend": 10.46, "impressions": 0, "clicks": 3, "visits": 0,
                            "conversions": 0, "conversion_value": 0.0}
    assert r["adset"] == {"id": "g1", "name": "g1", "status": "ACTIVE"}


def test_ad_record_keeps_image_url_only_when_set():
    r = tw.ad_record(shop="s", channel="c", channel_account_id=1, event_date="2026-01-01",
                     currency="USD", campaign={"id": 1}, adset={"id": 2},
                     ad={"id": 3, "ad_image_url": "https://img"}, metrics={})
    assert r["ad"]["ad_image_url"] == "https://img"
    assert "ad_image_url" not in r["campaign"]
    assert r["channel_account_id"] == "1" and r["campaign"]["id"] == "1"


def test_stable_id_ignores_case_and_whitespace():
    assert tw.stable_id("p", " Camp ", "Seg") == tw.stable_id("p", "camp", "seg")
    assert tw.stable_id("p", "camp", "a") != tw.stable_id("p", "camp", "b")


def test_zero_fill_backfills_gaps_up_to_last_reported_day():
    recs = [_rec("2026-01-01"), _rec("2026-01-03"), _rec("2026-01-02", ad="a2")]
    out = tw.zero_fill(recs, "2026-01-01", "2026-01-05")
    keys = {(r["event_date"], r["ad"]["id"]) for r in out}
    # both ads on every day 1..3, nothing on 4..5 (no data reported yet)
    assert keys == {(d, a) for d in ("2026-01-01", "2026-01-02", "2026-01-03") for a in ("a1", "a2")}
    zero = next(r for r in out if r["event_date"] == "2026-01-02" and r["ad"]["id"] == "a1")
    assert zero["metrics"]["spend"] == 0 and zero["ad"]["name"] == "Ad"
    assert len({tw.upsert_key(r) for r in out}) == len(out)


def test_zero_fill_empty():
    assert tw.zero_fill([], "2026-01-01", "2026-01-02") == []


def test_send_records_counts_failures():
    ok, bad = mock.Mock(ok=True), mock.Mock(ok=False, status_code=400, text="nope")
    with mock.patch.object(tw.requests.Session, "post", side_effect=[ok, bad]):
        out = tw.send_records([_rec("2026-01-01"), _rec("2026-01-02")], "key", workers=1)
    assert out["sent"] == 1 and out["failed"] == 1
    assert out["failures"][0]["status"] == 400
