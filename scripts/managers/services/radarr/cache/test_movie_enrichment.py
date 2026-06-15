"""Tests for RadarrCacheMovieFilesManager.refresh_enrichment — broadcast the enrich
daemon's cached cast/crew + Trakt rating onto movie_files rows (cache-only)."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.radarr.cache.movie_files import RadarrCacheMovieFilesManager


class _Log:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _StubCache:
    """Returns daemon-style {cast,crew}/{rating,votes} for tmdb 603 only."""
    def get_people(self, t):
        if t == 603:
            return {"cast": [{"name": "Sylvester Stallone", "order": 0},
                             {"name": "Michael B. Jordan", "order": 1}],
                    "crew": [{"name": "Ryan Coogler", "job": "Director", "department": "Directing"}]}
        return {}
    def get_ratings(self, t):
        return {"rating": 8.1, "votes": 1234} if t == 603 else {}


def _mgr(df, cache):
    # object.__new__ (not M.__new__) bypasses BaseManager's shared singleton so these
    # tests can't pollute each other's stubbed load()/save() (see test_movie_files_cache).
    m = object.__new__(RadarrCacheMovieFilesManager)
    m.logger = _Log()
    m._movie_cache = cache
    m._resolve_instance = lambda i: i
    m.load = lambda i: df
    m._saved = {}
    m.save = lambda i, d: (m._saved.__setitem__("df", d), True)[1]
    return m


def test_broadcasts_daemon_cast_crew_and_rating():
    df = pd.DataFrame([{"tmdb_id": 603, "title": "Creed"},
                       {"tmdb_id": 999, "title": "Unenriched"}])
    m = _mgr(df, _StubCache())
    n = m.refresh_enrichment("standard")
    assert n == 1                                            # only tmdb 603 had daemon data
    out = m._saved["df"]
    r = out[out.tmdb_id == 603].iloc[0]
    assert r["cast_names"] == "Sylvester Stallone|Michael B. Jordan"
    assert r["director_names"] == "Ryan Coogler"
    assert r["trakt_rating"] == 8.1 and r["trakt_vote_count"] == 1234
    assert pd.isna(out[out.tmdb_id == 999].iloc[0]["cast_names"])   # unenriched → missing


def test_no_daemon_data_is_noop():
    class _Empty:
        def get_people(self, t): return {}
        def get_ratings(self, t): return {}
    m = _mgr(pd.DataFrame([{"tmdb_id": 1, "title": "X"}]), _Empty())
    assert m.refresh_enrichment("standard") == 0 and "df" not in m._saved   # nothing saved


def test_missing_tmdb_column_safe():
    m = _mgr(pd.DataFrame([{"title": "X"}]), _StubCache())
    assert m.refresh_enrichment("standard") == 0
