# VENDORED with lib/runtime.py: identical in every triplewhale-* repo.
import datetime as dt

import pytest

import runtime


def test_secret_env_override(monkeypatch):
    monkeypatch.setenv("TRIPLEWHALE_API_KEY", " abc ")
    assert runtime.secret("triplewhale-api-key") == "abc"


def test_date_window_explicit():
    assert runtime.date_window(None, "2026-01-01", "2026-01-03", 14) == ("2026-01-01", "2026-01-03")


def test_date_window_trailing(monkeypatch):
    monkeypatch.setattr(runtime, "today_utc", lambda: dt.date(2026, 1, 14))
    assert runtime.date_window(None, None, None, 14) == ("2026-01-01", "2026-01-14")
    assert runtime.date_window(3, None, None, 14) == ("2026-01-12", "2026-01-14")


@pytest.mark.parametrize("args", [(None, "2026-01-01", None), (None, "2026-01-05", "2026-01-01"), (0, None, None)])
def test_date_window_rejects_bad_input(args):
    with pytest.raises(ValueError):
        runtime.date_window(*args, 14)


def test_truthy():
    assert runtime.truthy("1") and runtime.truthy("Yes") and not runtime.truthy(None)
