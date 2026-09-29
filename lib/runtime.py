"""
Runtime helpers shared by every Cloud Function in the triplewhale-* repos.

VENDORED: this file is identical in triplewhale-shop-campaigns, triplewhale-roku-ads
and triplewhale-openai-ads. Change it in one place and copy it to the others.
lib-version: 1.0.0

- config: `config.yaml` is looked up in $CONFIG_PATH, next to this module (deployed
  bundle), then one directory up (repo root, when running locally).
- secrets: Google Secret Manager, cached per instance. For local runs, an environment
  variable named after the secret id wins (`triplewhale-api-key` → TRIPLEWHALE_API_KEY),
  so nothing needs GCP credentials to try the code.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import os
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent


def setup_logging(name: str) -> logging.Logger:
    """INFO-level logger that actually reaches Cloud Logging.

    functions-framework configures the root logger before our code runs, which
    makes logging.basicConfig a no-op and leaves the level at WARNING, silently
    dropping every log.info. Set the level explicitly.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger().setLevel(logging.INFO)
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    return log


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _config_candidates() -> list[Path]:
    env = os.environ.get("CONFIG_PATH")
    paths = [Path(env)] if env else []
    return paths + [_HERE / "config.yaml", _HERE.parent / "config.yaml"]


@functools.lru_cache(maxsize=1)
def config() -> dict:
    for path in _config_candidates():
        if path.is_file():
            return yaml.safe_load(path.read_text()) or {}
    raise FileNotFoundError(
        "config.yaml not found (copy config.example.yaml to config.yaml, "
        "or set CONFIG_PATH)")


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

_secret_cache: dict[str, str] = {}
_sm_client = None


def _env_name(secret_id: str) -> str:
    return secret_id.upper().replace("-", "_").replace(".", "_")


def _sm():
    global _sm_client
    if _sm_client is None:
        from google.cloud import secretmanager  # lazy: local runs need no GCP libs
        _sm_client = secretmanager.SecretManagerServiceClient()
    return _sm_client


def _project_id() -> str:
    project = (config().get("gcp") or {}).get("project_id") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("gcp.project_id is not set in config.yaml")
    return project


def secret(secret_id: str, *, cache: bool = True) -> str:
    """Latest version of a secret; env var override for local runs."""
    env = os.environ.get(_env_name(secret_id))
    if env:
        return env.strip()
    if cache and secret_id in _secret_cache:
        return _secret_cache[secret_id]
    name = f"projects/{_project_id()}/secrets/{secret_id}/versions/latest"
    value = _sm().access_secret_version(request={"name": name}).payload.data.decode().strip()
    _secret_cache[secret_id] = value
    return value


def write_secret(secret_id: str, value: str) -> None:
    """Add a new secret version (the runtime service account needs secretVersionAdder)."""
    parent = f"projects/{_project_id()}/secrets/{secret_id}"
    _sm().add_secret_version(request={"parent": parent, "payload": {"data": value.encode()}})
    _secret_cache[secret_id] = value


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def json_response(body: dict, status: int = 200, headers: dict | None = None):
    return (json.dumps(body, indent=2, default=str), status,
            {"Content-Type": "application/json", **(headers or {})})


def truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def date_window(days: int | None, since: str | None, until: str | None,
                default_days: int) -> tuple[str, str]:
    """Inclusive (since, until) ISO dates: explicit bounds, else a trailing N-day window."""
    if since or until:
        if not (since and until):
            raise ValueError("since and until must be given together")
        d0, d1 = dt.date.fromisoformat(since), dt.date.fromisoformat(until)
        if d0 > d1:
            raise ValueError("since must not be after until")
        return d0.isoformat(), d1.isoformat()
    n = default_days if days is None else days
    if n < 1:
        raise ValueError("days must be >= 1")
    end = today_utc()
    return (end - dt.timedelta(days=n - 1)).isoformat(), end.isoformat()


def days_between(since: str, until: str) -> list[str]:
    d0, d1 = dt.date.fromisoformat(since), dt.date.fromisoformat(until)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
