"""Tests for affinity.platform_usage — household + per-user device tallies (pure)."""
from __future__ import annotations

from scripts.managers.machine_learning.affinity.platform_usage import (
    per_user_platform_usage,
    platform_usage,
)


def test_platform_usage_tally_desc():
    hist = [{"platform": "PS5"}, {"platform": "PS5"}, {"platform": "iOS"}, {}]
    assert platform_usage(hist) == {"PS5": 2, "iOS": 1, "Unknown": 1}


def test_per_user_platform_usage_groups_by_user_id():
    hist = [
        {"user_id": "1", "user": "Aiden", "platform": "PlayStation"},
        {"user_id": "1", "user": "Aiden", "platform": "PlayStation"},
        {"user_id": "1", "user": "Aiden", "platform": "iOS"},
        {"user_id": "2", "user": "Bea", "platform": "WebOS"},
    ]
    users = [{"user_id": "1", "username": "Aiden"}, {"user_id": "2", "username": "Bea"}]
    out = per_user_platform_usage(hist, users)
    assert out == {"Aiden": {"PlayStation": 2, "iOS": 1}, "Bea": {"WebOS": 1}}


def test_per_user_platform_usage_friendly_name_fallback_and_omits_empty():
    # User whose friendly_name differs from username joins via the `user` field; a user with no
    # history is omitted (not an empty dict).
    hist = [{"user": "Aiden / Raina", "platform": "Roku"}]
    users = [{"username": "Aiden", "friendly_name": "Aiden / Raina"},
             {"username": "Ghost"}]
    out = per_user_platform_usage(hist, users)
    assert out == {"Aiden": {"Roku": 1}}
