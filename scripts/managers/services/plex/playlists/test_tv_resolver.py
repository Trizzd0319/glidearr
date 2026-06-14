"""Tests for the TV playlist resolver — watched filter + join/expand/order glue."""
from __future__ import annotations

from scripts.managers.services.plex.playlists.tv_resolver import (
    build_tv_plan,
    watched_episode_keys,
)


# ── watched_episode_keys (per-user finished episodes) ─────────────────────────
def test_watched_filter_finished_episodes_only():
    history = [
        {"media_type": "episode", "rating_key": "1", "percent_complete": 95},
        {"media_type": "episode", "rating_key": "2", "percent_complete": 40},   # in-progress
        {"media_type": "movie", "rating_key": "3", "percent_complete": 99},     # not an episode
        {"media_type": "episode", "rating_key": None, "percent_complete": 99},  # no key
        {"media_type": "episode", "rating_key": "4", "percent_complete": "x"},  # bad pct → 0
    ]
    assert watched_episode_keys(history) == {"1"}


def test_watched_filter_threshold_tunable():
    h = [{"media_type": "episode", "rating_key": "9", "percent_complete": 50}]
    assert watched_episode_keys(h, min_pct=40) == {"9"}
    assert watched_episode_keys(h, min_pct=90) == set()


def test_watched_keys_include_stable_identities():
    """A finished episode contributes its ratingKey AND numeric (series, season, episode)
    AND (series, title) identities, so the join survives Plex ratingKey churn (the
    real-world failure: a heavy watcher's historical ratingKeys point at re-scanned
    items — 11/117 matched by ratingKey, 117/117 by (series, season, episode))."""
    h = [{"media_type": "episode", "rating_key": "40373",
          "grandparent_title": "Bluey (2018)", "title": "Chest",
          "parent_media_index": 3, "media_index": 11, "percent_complete": 100}]
    assert watched_episode_keys(h) == {
        "40373", ("bluey (2018)", 3, 11), ("bluey (2018)", "chest")}


def test_stale_ratingkey_still_excluded_via_title_and_index():
    """The actual bug: owned episode resolves to the CURRENT ratingKey but Tautulli only
    knows the STALE one. Without identity matching it leaks back into the plan; with it,
    both the (series, season, episode) and (series, title) joins exclude it."""
    owned = [_owned(1, 3, 11, "353546:3:11", title="Chest")]
    inv = {"353546:3:11": {"rating_key": "40467",          # current key (post re-scan)
                           "series_title": "Bluey (2018)", "title": "Chest"}}
    # Tautulli watched set carries only the STALE ratingKey + stable identities:
    watched = watched_episode_keys([{
        "media_type": "episode", "rating_key": "40373",     # stale, != 40467
        "grandparent_title": "Bluey (2018)", "title": "Chest",
        "parent_media_index": 3, "media_index": 11, "percent_complete": 100}])
    plan, _ = build_tv_plan(owned, inv, watched, {1: 80})
    assert plan.items == ()                                # excluded despite ratingKey mismatch


# ── build_tv_plan ─────────────────────────────────────────────────────────────
def _owned(sid, s, e, jk, title="ep"):
    return {"series_id": sid, "season_number": s, "episode_number": e,
            "tvdb_join_key": jk, "title": title, "air_date_utc": f"20{10+e:02d}-01-01",
            "is_special": s == 0}


def test_resolves_owned_episode_to_ratingkey():
    owned = [_owned(1, 1, 1, "100:1:1")]
    inv = {"100:1:1": {"rating_key": "5001", "title": "Pilot"}}
    plan, stats = build_tv_plan(owned, inv, set(), {1: 80})
    assert [i.rating_key for i in plan.items] == ["5001"]
    assert stats == {"owned": 1, "unresolved": 0, "resolved": 1, "series": 1, "in_plan": 1}


def test_unresolved_episode_dropped_and_counted():
    owned = [_owned(1, 1, 1, "100:1:1"), _owned(1, 1, 2, None),       # no join key
             _owned(1, 1, 3, "999:9:9")]                              # key not in inventory
    inv = {"100:1:1": {"rating_key": "5001"}}
    plan, stats = build_tv_plan(owned, inv, set(), {1: 80})
    assert [i.rating_key for i in plan.items] == ["5001"]
    assert stats["resolved"] == 1 and stats["unresolved"] == 2


def test_watched_episode_excluded_from_plan():
    owned = [_owned(1, 1, 1, "k1"), _owned(1, 1, 2, "k2")]
    inv = {"k1": {"rating_key": "a"}, "k2": {"rating_key": "b"}}
    plan, _ = build_tv_plan(owned, inv, {"a"}, {1: 80})       # 'a' already watched
    assert [i.rating_key for i in plan.items] == ["b"]


def test_episode_cap_limits_per_series():
    owned = [_owned(1, 1, e, f"k{e}") for e in range(1, 6)]   # 5 episodes
    inv = {f"k{e}": {"rating_key": f"r{e}"} for e in range(1, 6)}
    plan, _ = build_tv_plan(owned, inv, set(), {1: 80}, episode_cap=2)
    assert [i.rating_key for i in plan.items] == ["r1", "r2"]   # earliest 2 unwatched


def test_series_ordered_by_watchability():
    owned = [_owned(1, 1, 1, "a1"), _owned(2, 1, 1, "b1")]
    inv = {"a1": {"rating_key": "a"}, "b1": {"rating_key": "b"}}
    # series 2 scores higher → its block leads
    plan, _ = build_tv_plan(owned, inv, set(), {1: 30, 2: 90})
    assert [i.rating_key for i in plan.items] == ["b", "a"]
    assert plan.items[0].group_kind == "series"   # TV degrades to per-series grouping


def test_empty_inputs_safe():
    plan, stats = build_tv_plan([], {}, set(), {})
    assert plan.items == () and stats["resolved"] == 0 and stats["in_plan"] == 0
