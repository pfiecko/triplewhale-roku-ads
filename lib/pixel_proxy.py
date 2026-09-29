"""
Helpers for Cloud Functions that receive events from a Shopify Custom Pixel and
forward them to an ad platform's Conversions API.

VENDORED: this file is identical in triplewhale-roku-ads and triplewhale-openai-ads.
Change it in one place and copy it to the other.
lib-version: 1.0.0

Why a proxy at all: conversions APIs authenticate with a bearer token that must
never ship to the browser. The pixel posts to the function with a shared secret
(X-Pixel-Secret); the function holds the real token in Secret Manager.

The shared secret is visible to anyone who reads the pixel source, so treat it as
a spam filter, not authentication. The function re-validates everything it
forwards, and the scope filter runs server-side as well.
"""

from __future__ import annotations

import hashlib
import hmac
import re

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Pixel-Secret",
    "Access-Control-Max-Age": "3600",
}

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def secret_matches(provided: str | None, expected: str) -> bool:
    """Constant-time comparison of the X-Pixel-Secret header."""
    return bool(provided) and hmac.compare_digest(provided.encode(), expected.encode())


def client_ip(request) -> str | None:
    """Buyer's IP: the first X-Forwarded-For hop (Cloud Run appends its own proxies)."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip() or None
    return request.remote_addr or None


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_email(email: str | None, *, drop_plus_alias: bool = False) -> str | None:
    """Lowercase and trim; optionally drop a +alias from the local part."""
    if not email:
        return None
    email = str(email).strip().lower()
    if "@" not in email:
        return None
    if drop_plus_alias:
        local, domain = email.split("@", 1)
        email = f"{local.split('+', 1)[0]}@{domain}"
    return email


def hash_identifier(value: str | None) -> str | None:
    """SHA-256 of a trimmed, lowercased value; already-hashed values pass through."""
    if not value:
        return None
    value = str(value).strip().lower()
    return value if _SHA256_HEX.match(value) else sha256_hex(value)


def in_scope(payload: dict, allowed_currencies: list[str] | None,
             allowed_countries: list[str] | None) -> bool:
    """Optional order filter. Empty or missing lists allow everything.

    Currency is checked strictly. Country is checked only when the pixel sent one,
    because Shopify does not always expose an address on the checkout event.
    """
    currencies = [c.upper() for c in allowed_currencies or []]
    countries = [c.upper() for c in allowed_countries or []]
    currency = str(payload.get("currency") or "").upper()
    country = str(payload.get("country") or "").upper()
    if currencies and currency not in currencies:
        return False
    if countries and country and country not in countries:
        return False
    return True
