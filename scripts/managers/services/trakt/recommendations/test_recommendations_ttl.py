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
    """Counts fetches and returns a distinguishable payload each time.

    `params` is CAPTURED, not discarded. It used to be swallowed, which meant nothing
    in the suite could tell whether the recommendations call sent its filters -- and
    for a long time it sent none, so Trakt happily recommended titles the household
    already owned. Measured 2026-08-22: **1,016 of 1,017 candidates came back
    `already in library`**. A fake that drops an argument cannot fail a test about
    that argument.
    """

    def __init__(self):
        self.calls: list = []
        self.params: list = []

    def _make_request(self, endpoint, params=None):
        self.calls.append(endpoint)
        self.params.append(dict(params or {}))
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


#: The cache keys the manager actually uses. Defined ONCE here rather than spelled out
#: at each `_age` call: these tests reach into the cache by path to backdate a file, so a
#: key change in the manager silently makes them age a file nothing reads -- the test
#: then passes or fails for a reason unrelated to what it is checking. That is exactly
#: what happened when `ignore_collected`/`ignore_watchlisted` were added and the keys were
#: bumped to `/v2` to stop TTL serving pre-filter results: all four TTL tests broke, none
#: of them because the TTL behaviour had changed.
_SHOWS_KEY = "trakt/u/recommendations/shows/v2"
_MOVIES_KEY = "trakt/u/recommendations/movies/v2"


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
    _age(cache, _SHOWS_KEY, _RECOMMENDATIONS_TTL_S + 60)
    second = mgr.get_recommendations_shows()
    assert len(api.calls) == 2, api.calls
    assert second != first


def test_both_feeds_refresh_not_just_shows(tmp_path):
    cache = _cache(tmp_path)
    api = _Api()
    mgr = _mgr(cache, api)
    mgr.get_recommendations_movies()
    _age(cache, _MOVIES_KEY, _RECOMMENDATIONS_TTL_S + 60)
    mgr.get_recommendations_movies()
    assert api.calls == ["recommendations/movies", "recommendations/movies"], api.calls


def test_a_26_day_old_cache_is_stale_under_this_ttl(tmp_path):
    """The observed failure, pinned as a number: 26 days must not be servable."""
    assert _RECOMMENDATIONS_TTL_S < 26 * 24 * 3600
    cache = _cache(tmp_path)
    api = _Api()
    mgr = _mgr(cache, api)
    mgr.get_recommendations_shows()
    _age(cache, _SHOWS_KEY, 26 * 24 * 3600)
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
    _age(cache, _SHOWS_KEY, _RECOMMENDATIONS_TTL_S + 60)
    mgr.trakt_api = type("Dead", (), {"_make_request": lambda *a, **k: None})()
    after = mgr.get_recommendations_shows()
    assert after == good


def test_the_owned_and_watchlisted_filters_are_actually_sent(tmp_path):
    """The call must ask Trakt to exclude what the household already has.

    Both params default to FALSE on Trakt's side, so omitting them is an active
    request to INCLUDE owned titles — and that is what happened: of 1,017 candidates
    gathered on 2026-08-22, **1,016 came back `already in library`**. One useful
    suggestion per thousand, every run, at one API call each.

    `ignore_watchlisted` bites immediately (the watchlist is already Trakt-side, and
    is gathered as its OWN source, so recommendations were duplicating it).
    `ignore_collected` is correct but INERT until a collection sync exists — glidearr
    pushes watch history, never collection, so Trakt does not know what is owned.
    It is asserted here anyway: the filter being present and idle is the state we
    intend, and a future collection sync must not have to remember to add it.
    """
    api = _Api()
    mgr = _mgr(_cache(tmp_path), api)
    mgr.get_recommendations_shows()
    mgr.get_recommendations_movies()
    assert len(api.params) == 2, api.params
    for sent in api.params:
        assert sent.get("ignore_collected") == "true", sent
        assert sent.get("ignore_watchlisted") == "true", sent
        assert "limit" in sent, sent


def test_the_cache_key_changed_with_the_filters(tmp_path):
    """A params change with an unchanged cache key is a change that does nothing.

    `get_or_generate_cache` serves the stored copy until the TTL expires, so adding
    the filters without bumping the key would have kept serving PRE-FILTER results
    for up to a day — and the operator would have read that as "the fix did not
    work". The `/v2` suffix forces a fresh fetch on the very first run after the
    change. This pins the two together so neither can move without the other.
    """
    mgr = _mgr(_cache(tmp_path), _Api())
    import inspect
    src = inspect.getsource(type(mgr))
    assert "recommendations/shows/v2" in src
    assert "recommendations/movies/v2" in src


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
