# VENDORED with lib/pixel_proxy.py: identical in triplewhale-roku-ads and triplewhale-openai-ads.
from types import SimpleNamespace

import pixel_proxy as pp


def test_secret_matches():
    assert pp.secret_matches("s3cret", "s3cret")
    assert not pp.secret_matches("", "s3cret") and not pp.secret_matches(None, "s3cret")
    assert not pp.secret_matches("other", "s3cret")


def test_client_ip_prefers_first_forwarded_hop():
    req = SimpleNamespace(headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.1"}, remote_addr="9.9.9.9")
    assert pp.client_ip(req) == "1.2.3.4"
    assert pp.client_ip(SimpleNamespace(headers={}, remote_addr="9.9.9.9")) == "9.9.9.9"


def test_email_normalization_and_hashing():
    assert pp.normalize_email(" Jane+ads@Example.com ") == "jane+ads@example.com"
    assert pp.normalize_email("Jane+ads@Example.com", drop_plus_alias=True) == "jane@example.com"
    assert pp.normalize_email("not-an-email") is None
    h = pp.hash_identifier("jane@example.com")
    assert len(h) == 64 and pp.hash_identifier(h.upper()) == h


def test_in_scope():
    assert pp.in_scope({"currency": "GBP"}, [], None)
    assert pp.in_scope({"currency": "usd", "country": "us"}, ["USD"], ["US"])
    assert pp.in_scope({"currency": "USD"}, ["USD"], ["US"])          # no country sent → allowed
    assert not pp.in_scope({"currency": "GBP", "country": "US"}, ["USD"], ["US"])
    assert not pp.in_scope({"currency": "USD", "country": "CA"}, ["USD"], ["US"])
