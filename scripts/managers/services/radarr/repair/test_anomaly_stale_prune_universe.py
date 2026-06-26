"""RadarrRepairAnomalyManager.demote_stale_monitored — the universe_credit DELETE guard.

The owned stale-prune is the movie delete path that runs when the space coordinator does NOT
own deletion. It must spare an UNTAGGED hot-saga member (universe_credit >= UNIVERSE_PROTECT_MIN)
from its DELETE branch — exactly as the space-pressure delete/downgrade paths do — while still
letting it unmonitor/age so the recency-decayed credit eventually lets it go. Sourced from the
movie_files parquet because this path scores raw Radarr API dicts that lack the column.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.services.radarr.repair.anomaly import RadarrRepairAnomalyManager
from scripts.managers.machine_learning.space.downgrade_planner import UNIVERSE_PROTECT_MIN

_GIB = 1024 ** 3
_NOW = datetime.now(timezone.utc)
_OLD = (_NOW - timedelta(days=200)).isoformat()   # well past every dwell -> delete-eligible


class _Logger:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_error(self, *a, **k): pass
    def log_success(self, *a, **k): pass
    def log_grid(self, *a, **k): pass
    def log_table(self, *a, **k): pass


class _Cache:
    """Minimal global_cache: serves the per-movie delete clock, swallows set()."""
    def __init__(self, clock):
        self._clock = clock

    def get(self, key, default=None):
        return self._clock if key.endswith("delete_clock") or "demote" in key or "delete" in key else default

    def set(self, key, val):
        pass


class _MFM:
    """Fake movie_files manager: load() returns the parquet frame carrying universe_credit."""
    def __init__(self, df):
        self._df = df

    def load(self, instance):
        return self._df


class _Registry:
    def __init__(self, mfm):
        self._mfm = mfm

    def get(self, kind, name):
        if name == "RadarrCacheMovieFilesManager":
            return self._mfm
        return None


class _Radarr:
    def disk_free_gb(self, instance):
        return 50.0          # below U (floor 100) -> pressure active -> delete branch live

    def _make_request(self, *a, **k):
        return None          # never hit in dry_run


def _movie(mid, tmdb):
    return {"id": mid, "tmdbId": tmdb, "title": f"movie {tmdb}", "year": 2015,
            "hasFile": True, "monitored": True,
            "movieFile": {"id": 100 + mid, "size": 5 * _GIB}}


def _mgr(credit_df):
    m = object.__new__(RadarrRepairAnomalyManager)
    m.config = {
        "owned_demote_enabled": True, "owned_delete_enabled": True,
        "deletions_consent": True, "free_space_limit": 100.0,
        "owned_demote_score_threshold": 20, "owned_demote_dwell_days": 30,
        "owned_delete_dwell_days": 90, "owned_delete_min_dwell_days": 7,
    }
    m.logger = _Logger()
    m.dry_run = True
    m.radarr_api = _Radarr()
    m.global_cache = _Cache({"1001": _OLD, "1002": _OLD})
    m.registry = _Registry(_MFM(credit_df))
    m._resolve_instance = lambda i: i
    # Two below-floor, fully-credited (not deferred), never-watched owned movies.
    ctx = {"all_movies": [_movie(1, 1001), _movie(2, 1002)],
           "watched_tmdb_ids": set(), "collection_members": {}, "tag_label_map": {}}
    m._build_scoring_context = lambda inst: ctx
    m._resolve_keep_policy = lambda movie, tlm: None
    m._score_owned = lambda movie, c, score_fn: (0, True)   # score 0 < floor, has credits
    return m


def _credit_df(c1001, c1002):
    return pd.DataFrame([
        {"tmdb_id": 1001, "universe_credit": c1001},
        {"tmdb_id": 1002, "universe_credit": c1002},
    ])


def test_hot_saga_member_spared_from_stale_prune_delete():
    # tmdb 1002 carries credit >= UNIVERSE_PROTECT_MIN -> spared the DELETE branch (unmonitored
    # instead), while the cold sibling 1001 is deleted. Proves the guard fires on THIS path.
    m = _mgr(_credit_df(0.0, 2.0))
    stats = m.demote_stale_monitored("standard")
    assert stats["skipped_universe"] == 1, stats
    assert stats["deleted"] == 1, stats          # only the cold one
    assert stats["unmonitored"] == 1, stats      # the hot one routed to unmonitor/age


def test_without_credit_both_are_deleted():
    # Same two movies, both cold (credit below floor) -> the guard never fires and BOTH delete.
    # The delta vs the test above is exactly the credit guard's protection.
    m = _mgr(_credit_df(0.0, 0.4))
    stats = m.demote_stale_monitored("standard")
    assert stats["skipped_universe"] == 0, stats
    assert stats["deleted"] == 2, stats


def test_at_floor_is_protected_and_missing_column_is_byte_identical():
    # Boundary: credit == UNIVERSE_PROTECT_MIN is protected (>=).
    m = _mgr(_credit_df(0.0, UNIVERSE_PROTECT_MIN))
    assert m.demote_stale_monitored("standard")["skipped_universe"] == 1
    # Cold/absent column: no universe_credit column at all -> guard inert, both delete.
    m2 = _mgr(pd.DataFrame([{"tmdb_id": 1001}, {"tmdb_id": 1002}]))
    s2 = m2.demote_stale_monitored("standard")
    assert s2["skipped_universe"] == 0 and s2["deleted"] == 2, s2
