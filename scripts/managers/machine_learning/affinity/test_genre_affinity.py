"""Tests for affinity.genre_affinity — household/per-user taste maps, and the opt-in
temporal recency decay (default-off = byte-identical raw counts)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from scripts.managers.machine_learning.affinity.genre_affinity import (
    aggregate_affinity,
    per_user_affinity,
)

_NOW = datetime(2026, 6, 10, tzinfo=timezone.utc)


def _ts(days_ago: int) -> int:
    return int((_NOW - timedelta(days=days_ago)).timestamp())


_META = {"1": {"genres": ["Recent"]}, "2": {"genres": ["Old"]}}
_HIST = [
    {"rating_key": "1", "date": _ts(0)},    # Recent, today
    {"rating_key": "2", "date": _ts(90)},   # Old, 90d ago
    {"rating_key": "2", "date": _ts(90)},   # Old again (count 2)
]


def test_default_off_is_byte_identical_int_counts():
    out = aggregate_affinity(_HIST, _META)
    assert out["genres"] == {"Old": 2, "Recent": 1}          # raw counts, Old leads
    assert all(isinstance(v, int) for v in out["genres"].values())   # ints, not floats


def test_decay_flips_ranking_toward_recent():
    out = aggregate_affinity(_HIST, _META, half_life_days=30, now=_NOW)
    # Recent: exp(0)=1.0 ; Old: 2*exp(-90/30)=2*exp(-3) ~= 0.0996 -> Recent now leads
    assert list(out["genres"])[0] == "Recent"
    assert out["genres"]["Recent"] == 1.0
    assert abs(out["genres"]["Old"] - 2 * math.exp(-3)) < 1e-9


def test_missing_date_is_neutral_weight():
    out = aggregate_affinity([{"rating_key": "1"}], _META, half_life_days=30, now=_NOW)
    assert out["genres"]["Recent"] == 1.0   # no date -> no decay, not dropped


def test_per_user_threads_half_life():
    hist = [
        {"user": "A", "rating_key": "1", "date": _ts(0)},
        {"user": "A", "rating_key": "2", "date": _ts(90)},
    ]
    users = [{"username": "A"}]
    out = per_user_affinity(hist, _META, users, half_life_days=30, now=_NOW)
    assert list(out["A"]["genres"])[0] == "Recent"   # decay applied per-user


def test_per_user_joins_on_user_id_when_friendly_name_differs_from_username():
    # Regression: a Tautulli account whose history `user` (friendly name) differs from its
    # login `username` must still get its affinity — joined on the stable user_id. The old
    # code bucketed history by the friendly name 'Aiden / Raina' but looked it up by the
    # username 'Aiden', so this user matched nothing and silently lost all affinity.
    hist = [
        {"user": "Aiden / Raina", "user_id": 9, "rating_key": "1", "date": _ts(0)},
        {"user": "Aiden / Raina", "user_id": 9, "rating_key": "2", "date": _ts(1)},
    ]
    users = [{"username": "Aiden", "user_id": 9, "friendly_name": "Aiden / Raina"}]
    out = per_user_affinity(hist, _META, users)
    assert "Aiden" in out                              # keyed by login username (cache path stable)
    assert set(out["Aiden"]["genres"]) == {"Recent", "Old"}   # history reached the user


def test_per_user_friendly_name_fallback_when_history_lacks_user_id():
    # No user_id on the history rows -> fall back to the friendly-name `user` match.
    hist = [{"user": "Mom", "rating_key": "1", "date": _ts(0)}]
    users = [{"username": "Mom", "friendly_name": "Mom"}]
    out = per_user_affinity(hist, _META, users)
    assert out["Mom"]["genres"] == {"Recent": 1}
