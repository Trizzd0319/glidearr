"""Tests for the SERIES active-watcher upgrade decision (space.upgrade_planner), extracted
from sonarr/series/quality.run_active_watcher_upgrades (ML Step 7c). The pure phases the
service threads its per-series fetches through: aggregate -> df-guards -> per-record decide.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from scripts.managers.machine_learning.space.upgrade_planner import (
    active_series_candidates,
    aggregate_series_signals,
    decide_series_upgrade,
    series_fully_downloaded,
)


def test_series_fully_downloaded():
    assert series_fully_downloaded({"statistics": {"episodeCount": 10, "episodeFileCount": 10}}) is True
    assert series_fully_downloaded({"statistics": {"episodeCount": 10, "episodeFileCount": 9}}) is False
    assert series_fully_downloaded({"statistics": {"episodeCount": 0, "episodeFileCount": 0}}) is False  # 0 eps -> not "fully"
    assert series_fully_downloaded({}) is False

_KIDS = {"g", "pg", "tv-g", "tv-y", "tv-y7"}
_FREEZE = {"keep_quality", "keep-quality", "keepquality"}


def test_aggregate_series_signals():
    df = pd.DataFrame([
        {"series_id": 1, "series_title": "A", "last_watched_at": "2026-06-01T00:00:00Z",
         "certification": "TV-MA", "keep_policy": None, "is_watched": True, "all_household_watched": True},
        {"series_id": 1, "series_title": "A", "last_watched_at": "2026-06-05T00:00:00Z",
         "certification": "tv-ma", "keep_policy": None, "is_watched": True, "all_household_watched": False},
        {"series_id": 2, "series_title": "B", "last_watched_at": None,
         "certification": "G", "keep_policy": "keep_series", "is_watched": False, "all_household_watched": False},
    ])
    sd = aggregate_series_signals(df)
    assert set(sd) == {1, 2}
    assert sd[1]["title"] == "A"
    assert sd[1]["watched_eps"] == 2
    assert sd[1]["household_eps"] == 1
    assert sd[1]["certs"] == {"tv-ma"}                       # lowercased + deduped
    assert str(sd[1]["latest_watch"])[:10] == "2026-06-05"  # max across rows
    assert sd[2]["latest_watch"] is None
    assert sd[2]["keep_policy"] == "keep_series"


def test_active_series_candidates_guards():
    cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
    recent = datetime(2026, 6, 1, tzinfo=timezone.utc)
    stale = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sd = {
        1: {"title": "keep",   "latest_watch": recent, "certs": set(),       "keep_policy": "keep_series", "watched_eps": 1, "household_eps": 0},
        2: {"title": "stale",  "latest_watch": stale,  "certs": set(),       "keep_policy": None,          "watched_eps": 1, "household_eps": 0},
        3: {"title": "kids",   "latest_watch": recent, "certs": {"g"},       "keep_policy": None,          "watched_eps": 1, "household_eps": 0},
        4: {"title": "active", "latest_watch": recent, "certs": {"tv-ma"},   "keep_policy": None,          "watched_eps": 1, "household_eps": 0},
    }
    active, stats = active_series_candidates(sd, cutoff=cutoff, kids_certs=_KIDS)
    assert [sid for sid, _ in active] == [4]
    assert stats == {"checked": 4, "skipped_keep": 1, "skipped_not_active": 1, "skipped_kids": 1}


def test_active_series_candidates_none_latest_is_not_active():
    cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
    sd = {1: {"title": "x", "latest_watch": None, "certs": set(), "keep_policy": None, "watched_eps": 0, "household_eps": 0}}
    active, stats = active_series_candidates(sd, cutoff=cutoff, kids_certs=_KIDS)
    assert active == [] and stats["skipped_not_active"] == 1


def test_decide_series_upgrade_guards_in_order():
    fully = {"statistics": {"episodeCount": 10, "episodeFileCount": 10}, "qualityProfileId": 1, "runtime": 30}
    assert decide_series_upgrade(fully, set(), best_id=9, freeze_tags=_FREEZE, mbpm=50)["skip"] == "fully_downloaded"

    frozen = {"statistics": {"episodeCount": 10, "episodeFileCount": 3}, "qualityProfileId": 1, "runtime": 30}
    assert decide_series_upgrade(frozen, {"keep_quality"}, best_id=9, freeze_tags=_FREEZE, mbpm=50)["skip"] == "quality_frozen"

    at_best = {"statistics": {"episodeCount": 10, "episodeFileCount": 3}, "qualityProfileId": 9, "runtime": 30}
    assert decide_series_upgrade(at_best, set(), best_id=9, freeze_tags=_FREEZE, mbpm=50)["skip"] == "already_best"


def test_decide_series_upgrade_numbers():
    rec = {"statistics": {"episodeCount": 10, "episodeFileCount": 3}, "qualityProfileId": 1, "runtime": 30}
    v = decide_series_upgrade(rec, set(), best_id=9, freeze_tags=_FREEZE, mbpm=50)
    assert v["skip"] is None
    assert v["remaining"] == 7
    assert v["runtime_min"] == 30.0
    assert abs(v["est_gb"] - (50 * 30 * 7 / 1024.0)) < 1e-9


def test_decide_series_upgrade_runtime_fallback():
    rec = {"statistics": {"episodeCount": 5, "episodeFileCount": 1}, "qualityProfileId": 1, "runtime": 0}
    v = decide_series_upgrade(rec, set(), best_id=9, freeze_tags=_FREEZE, mbpm=50)
    assert v["runtime_min"] == 45.0   # 0 -> default
