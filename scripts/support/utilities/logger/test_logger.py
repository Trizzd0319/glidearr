"""Unit tests for LoggerManager secret-scrubbing — the short-credential register
path and the broadened ``pin=`` pattern (PR-6 logger pin / short-token hardening).

The scrubber's ``_scrub_values`` set is class-level (the logger is a singleton), so
each test snapshots and restores it to stay isolated from registrations elsewhere."""
from __future__ import annotations

import pytest

from scripts.support.utilities.logger.logger import LoggerManager


@pytest.fixture
def clean_scrub():
    """Isolate ``_scrub_values`` so registrations don't leak across tests."""
    saved = set(LoggerManager._scrub_values)
    LoggerManager._scrub_values.clear()
    try:
        yield LoggerManager()
    finally:
        LoggerManager._scrub_values.clear()
        LoggerManager._scrub_values.update(saved)


# ──────────────── short-credential register path ──────────────── #

def test_short_pin_scrubbed_in_pin_equals_form(clean_scrub):
    LoggerManager.register_short_secrets(["1234"])
    s = clean_scrub._scrub("POST /home/users/abc/switch?pin=1234 ok")
    assert "1234" not in s


def test_short_pin_scrubbed_in_dict_repr_form(clean_scrub):
    LoggerManager.register_short_secrets(["1234"])
    s = clean_scrub._scrub("payload was {'username': 'kid', 'pin': '1234'}")
    assert "1234" not in s and "<redacted>" in s


def test_short_pin_scrubbed_as_bare_value_in_sentence(clean_scrub):
    LoggerManager.register_short_secrets(["1234"])
    s = clean_scrub._scrub("the resolved pin is 1234 for this profile")
    assert "1234" not in s and "<redacted>" in s


def test_register_secret_allow_short_equivalent(clean_scrub):
    """``register_secret(value, allow_short=True)`` is the single-value spelling."""
    LoggerManager.register_secret("1234", allow_short=True)
    assert "1234" in LoggerManager._scrub_values
    s = clean_scrub._scrub("bare 1234 leaks")
    assert "1234" not in s


def test_non_numeric_short_pin_scrubbed_when_registered(clean_scrub):
    LoggerManager.register_short_secrets(["ab9z"])
    s = clean_scrub._scrub("switch?pin=ab9z&continue=1")
    assert "ab9z" not in s


# ──────────────── general register path keeps the >=8 floor ──────────────── #

def test_register_secrets_still_ignores_under_8_char_values(clean_scrub):
    LoggerManager.register_secrets(["1234"])
    assert "1234" not in LoggerManager._scrub_values
    # an unregistered bare short value is left untouched (no over-scrubbing of noise)
    s = clean_scrub._scrub("order count was 1234 items")
    assert "1234" in s


def test_register_secret_default_keeps_floor(clean_scrub):
    LoggerManager.register_secret("1234")          # allow_short defaults False
    assert "1234" not in LoggerManager._scrub_values


def test_register_secrets_still_accepts_long_values(clean_scrub):
    LoggerManager.register_secrets(["minted-token-9f8e7d6c"])
    s = clean_scrub._scrub("authToken was minted-token-9f8e7d6c for the switch")
    assert "minted-token-9f8e7d6c" not in s and "<redacted>" in s


# ──────────────── broadened pin= pattern (no registration needed) ──────────────── #

def test_pin_pattern_numeric_still_redacted(clean_scrub):
    s = clean_scrub._scrub("POST switch?pin=4821 ok")
    assert "4821" not in s and "pin=<redacted>" in s


def test_pin_pattern_non_numeric_redacted(clean_scrub):
    s = clean_scrub._scrub("POST switch?pin=4a8z1 ok")
    assert "4a8z1" not in s and "pin=<redacted>" in s


def test_pin_pattern_ampersand_terminated_redacted(clean_scrub):
    s = clean_scrub._scrub("switch?pin=4821&X-Plex-Product=glidearr")
    assert "4821" not in s and "pin=<redacted>" in s
    # the trailing param survives — the redaction stops at the '&'
    assert "X-Plex-Product=glidearr" in s
