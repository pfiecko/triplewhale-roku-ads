import json
from types import SimpleNamespace
from unittest import mock

import pytest

from conftest import load_function

proxy = load_function("conversion-proxy")
PAYLOAD = {"event_id": "1001", "order_id": "1001", "value": 89.5, "currency": "USD", "country": "US",
           "email": " Jane+tv@Example.com ", "contents": [{"id": "SKU1", "quantity": 1, "price": 89.5}],
           "content_ids": ["SKU1"], "num_items": 1, "source_url": "https://shop.test/thank-you"}


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setenv("ROKU_PIXEL_SECRET", "px")
    monkeypatch.setenv("ROKU_CAPI_TOKEN", "jwt")


def _req(payload, secret="px", method="POST"):
    return SimpleNamespace(method=method, get_json=lambda silent=True: payload, remote_addr="9.9.9.9",
                           headers={"X-Pixel-Secret": secret, "X-Forwarded-For": "1.2.3.4",
                                    "User-Agent": "UA"})


def test_build_event_hashes_email_without_alias():
    ev = proxy.build_event(PAYLOAD, "1.2.3.4", "UA")
    import hashlib
    assert ev["user_data"]["em"] == hashlib.sha256(b"jane@example.com").hexdigest()
    assert ev["user_data"]["is_hashed"] is True
    assert ev["custom_data"]["value"] == 89.5 and ev["event_id"] == "1001"
    assert "partner_name" not in ev


def test_build_event_needs_an_identifier():
    assert proxy.build_event({**PAYLOAD, "email": None}, None, "UA") is None


def test_rejects_bad_secret():
    assert proxy.track(_req(PAYLOAD, secret="nope"))[1] == 401


def test_out_of_scope_is_skipped_without_calling_roku():
    with mock.patch.object(proxy.requests, "post") as post:
        body, status, _ = proxy.track(_req({**PAYLOAD, "currency": "GBP"}))
    assert status == 200 and json.loads(body)["skipped"] == "out_of_scope"
    post.assert_not_called()


@pytest.mark.parametrize("roku_status,expected", [(200, 200), (210, 200), (400, 502)])
def test_reflects_roku_outcome(roku_status, expected):
    with mock.patch.object(proxy.requests, "post", return_value=mock.Mock(status_code=roku_status, text="{}")) as post:
        status = proxy.track(_req(PAYLOAD))[1]
    assert status == expected
    sent = post.call_args.kwargs["json"]
    assert sent["event_group_id"] == "EG_TEST" and post.call_args.args[0].endswith("/v1/events")
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer jwt"
