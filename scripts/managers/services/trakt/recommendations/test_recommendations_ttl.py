"""trakt/recommendations/test_recommendations_ttl.py — the frozen-cache bug.

``get_or_generate_cache`` has TWO independent knobs and BOTH are required to make a key
actually refresh:

  * ``expiration_time``      — how old is too old;
  * ``regenerate_on_expiry`` — whether an expired key is re-fetched, or (the documented
                               legacy default) logged as expired and then served from disk
                               anyway.

Recommendations passed NEITHER. Once ``recommendations/{shows,movies}.json`` existed it was
returned from disk on every subsequent call and ``_fetch_*`` was never invoked again — on
this install the two files were 26 days stale. The tests below drive the REAL cache
implementation against a temp directory (not a stub of it), because the bug lived entirely
in that method's contract and a stub would have reproduced whatever we assumed.
"""
from __future__ import annotations

import os
import time

from scripts.managers.factories.cache import GlobalCacheManager
from scripts.managers.services.trakt.recommendations import (
    TraktRecommendationsManager,
    _RECOMMENDATIONS_TTL_S,
)


class _Logger:
    def log_info(self, m):
        pass

    log_warning = log_debug = log_error = log_info

    def log_table(self, *a, **k):
        pass


class _Api:
    """Counts fetches and returns a distinguishable payload each time."""

    def __init__(self):
        self.calls: list = []

    def _make_request(self, endpoint, params=None):
        self.calls.append(endpoint)
        return [{"title": f"{endpoint} #{len(self.calls)}", "year": 2020}]


def _cache(tmp_path):
    gc = object.__new__(GlobalCacheManager)
    gc.logger = _Logger()
    gc.config = {}
    GlobalCacheManager.__init__(gc, logger=_Logger(), config={})
    # Re-point every path helper at the temp dir — the live cache is never touched.
    from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
    from scripts.managers.factories.cache.json_handler import CacheJsonManager
    gc.key_builder = CacheKeyBuilder(tmp_path)
    gc.cache_root = gc.key_builder.base_dir
    gc.json_handler = CacheJsonManager(logger=_Logger(), base_dir=gc.key_builder.base_dir)
    return gc


def _mgr(cache, api):
    m = object.__new__(TraktRecommendationsManager)
    m.global_cache = cache
    m.logger = _Logger()
    m.config = {"trakt": {"username": "u"}}
    m.registry = None
    m.dry_run = True
    m.trakt_api = api
    m.user = "u"
    return m


def _age(cache, key, seconds):
    """Backdate the cached file so the TTL is genuinely exceeded."""
    path = cache.key_builder.build_cache_path(*key.split("/"), suffix=".json")
    past = time.time() - seconds
    os.utime(path, (past, past))


def test_a_fresh_cache_is_served_without_refetching(tmp_path):
    api = _Api()
    mgr = _mgr(_cache(tmp_path), api)
    mgr.get_recommendations_shows()
    mgr.get_recommendations_shows()
    assert api.calls == ["recommendations/shows"], api.calls


def test_an_expired_cache_is_ACTUALLY_REFETCHED(tmp_path):
    """The bug. Before the fix this asserted one call forever, no matter how old the file
    got — the shipped code had no TTL at all and the method serves stale by default."""
    cache = _cache(tmp_path)
    api = _Api()
    mgr = _mgr(cache, api)
    first = mgr.get_recommendations_shows()
    _age(cache, "trakt/u/recommendations/shows", _RECOMMENDATIONS_TTL_S + 60)
    second = mgr.get_recommendations_shows()
    assert len(api.calls) == 2, api.calls
    assert second != first


def test_both_feeds_refresh_not_just_shows(tmp_path):
    cache = _cache(tmp_path)
    api = _Api()
    mgr = _mgr(cache, api)
    mgr.get_recommendations_movies()
    _age(cache, "trakt/u/recommendations/movies", _RECOMMENDATIONS_TTL_S + 60)
    mgr.get_recommendations_movies()
    assert api.calls == ["recommendations/movies", "recommendations/movies"], api.calls


def test_a_26_day_old_cache_is_stale_under_this_ttl(tmp_path):
    """The observed failure, pinned as a number: 26 days must not be servable."""
    assert _RECOMMENDATIONS_TTL_S < 26 * 24 * 3600
    cache = _cache(tmp_path)
    api = _Api()
    mgr = _mgr(cache, api)
    mgr.get_recommendations_shows()
    _age(cache, "trakt/u/recommendations/shows", 26 * 24 * 3600)
    mgr.get_recommendations_shows()
    assert len(api.calls) == 2


def test_the_ttl_matches_the_projects_other_trakt_fetches():
    """Chosen for consistency, not invented: trakt/history uses 86_400."""
    assert _RECOMMENDATIONS_TTL_S == 86_400


def test_a_failed_refetch_serves_the_last_good_copy_rather_than_emptying_it(tmp_path):
    """``regenerate_on_expiry`` only refetches; a rate-limited Trakt call returning None
    must not overwrite good data with nothing (the cache method's own contract)."""
    cache = _cache(tmp_path)
    api = _Api()
    mgr = _mgr(cache, api)
    good = mgr.get_recommendations_shows()
    _age(cache, "trakt/u/recommendations/shows", _RECOMMENDATIONS_TTL_S + 60)
    mgr.trakt_api = type("Dead", (), {"_make_request": lambda *a, **k: None})()
    after = mgr.get_recommendations_shows()
    assert after == good


def test_recommendations_are_not_wired_into_the_intent_index():
    """DECIDED, not overlooked (see the module note). Recommendations are an algorithm's
    inference, they carry no per-item timestamp for the staleness term, and ~70% of the
    feed is already owned — so they must not reach Group-A5 or the delete shield."""
    import inspect
    from scripts.managers.services import _intent_index
    from scripts.managers.machine_learning.next_watch import build_intent_index

    # nothing in the gather reads a recommendations cache key…
    assert "recommendations" not in inspect.getsource(_intent_index)
    # …and the fold has no kwarg that could carry them in
    params = set(inspect.signature(build_intent_index).parameters)
    assert not [p for p in params if "recommend" in p], params
    assert params == {"plex_union", "trakt_shows", "trakt_movies", "mal_shows",
                      "mal_movies", "member_anchors", "trakt_member"}, params

    # The ladder still RANKS the feed at 0.65 — that is deliberate and is not a wiring.
    # ``watchlist_intent_score`` only ever grades sources an entry actually carries, and no
    # entry can carry this one, so the rung documents the ordering without activating it.
    from scripts.managers.machine_learning.next_watch import INTENT_SOURCE_STRENGTH
    assert INTENT_SOURCE_STRENGTH["trakt_recommendations"] == 0.65

    # …while the ACQUISITION scorer, where an algorithmic suggestion belongs, still ranks it
    from scripts.managers.services.acquisition.scorer import _SOURCE_SCORE
    assert _SOURCE_SCORE.get("trakt_recommendations")
