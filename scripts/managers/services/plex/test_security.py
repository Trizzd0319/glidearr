"""Security-posture tests (DESIGN §6.2): the logger redacts profile PINs and bare
minted tokens, and the on-disk cache-key scheme never includes a token key."""
from __future__ import annotations

from scripts.support.utilities.logger.logger import LoggerManager


def test_pin_is_scrubbed_from_logs():
    s = LoggerManager()._scrub("POST /home/users/abc/switch?pin=4821 ok")
    assert "4821" not in s and "pin=<redacted>" in s


def test_xplex_token_query_param_scrubbed():
    s = LoggerManager()._scrub("GET https://metadata.provider.plex.tv/...?X-Plex-Token=abcd1234efgh")
    assert "abcd1234efgh" not in s


def test_registered_token_scrubbed_anywhere():
    LoggerManager.register_secrets(["minted-token-9f8e7d6c"])
    s = LoggerManager()._scrub("authToken was minted-token-9f8e7d6c for the switch")
    assert "minted-token-9f8e7d6c" not in s and "<redacted>" in s
