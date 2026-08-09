"""enrich_daemon watchlist/recommendation tiers + Trakt id addressing.

Two things are pinned here.

**The tier.** Every other daemon pool is derived from the Radarr/Sonarr libraries, so
before this existed the people-matrix only held titles the household already owned —
exactly the set the acquisition scorer discards as "already in library".

**The id split.** The cache bucket is named after the EXTERNAL id (tmdbId/tvdbId), but the
Trakt URL needs a Trakt id, slug or IMDB id. Conflating the two silently cached another
title's data: ``movies/1271`` (300's tmdbId) resolved Trakt movie 1271, an unrelated film,
while 300's real Trakt id is 884. Every value in these indexes is therefore
``(label, fetch_id)`` — never a bare label.

The two watchlist sources also have different shapes: Trakt nests the title under a
``movie``/``show`` key, Plex is flat with a ``type`` field and carries BOTH a tmdb and a
tvdb id on every row regardless of medium.
"""
from __future__ import annotations

import json

import pytest

from scripts.support.daemons import enrich_daemon as ed


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def caches(tmp_path, monkeypatch):
    """Redirect both watchlist roots at a temp tree."""
    trakt, plex = tmp_path / "trakt", tmp_path / "plex"
    monkeypatch.setattr(ed, "CACHE_TRAKT", trakt)
    monkeypatch.setattr(ed, "_PLEX_CACHE", plex)
    return trakt, plex


# ─── Trakt-addressable id selection ───────────────────────────────────────────

def test_prefers_trakt_id_over_imdb():
    assert ed._trakt_addressable_id({"trakt": 884, "imdb": "tt0416449", "tmdb": 1271}) == 884


def test_falls_back_to_imdb_when_no_trakt_id():
    assert ed._trakt_addressable_id({"imdb": "tt0416449", "tmdb": 1271}) == "tt0416449"


def test_never_falls_back_to_the_external_id():
    """The bug being fixed: a tmdb/tvdb id must NEVER be offered as a Trakt path id."""
    assert ed._trakt_addressable_id({"tmdb": 1271, "tvdb": 99}) is None
    assert ed._trakt_addressable_id({}) is None
    assert ed._trakt_addressable_id({"imdb": "1271"}) is None      # not a tt-id


def test_accepts_a_stringified_trakt_id():
    assert ed._trakt_addressable_id({"trakt": "884"}) == 884


# ─── watchlists ───────────────────────────────────────────────────────────────

def test_reads_trakt_nested_shape_with_fetch_id(caches):
    trakt, _ = caches
    _write(trakt / "BuckI" / "watchlist" / "movies.json",
           [{"type": "movie", "movie": {"title": "300", "year": 2007,
                                        "ids": {"tmdb": 1271, "trakt": 884,
                                                "imdb": "tt0416449"}}}])
    movies, shows = ed.watchlist_index()
    assert movies == {1271: ("300 (2007)", 884)}     # keyed by tmdb, fetched by trakt
    assert shows == {}


def test_plex_rows_keyed_by_type_not_by_whichever_id_exists(caches):
    """Plex carries tmdb AND tvdb on every row; a show must be taken by tvdb, not tmdb."""
    _, plex = caches
    _write(plex / "users" / "Aiden" / "watchlist.json",
           [{"title": "See", "year": 2019, "type": "show",
             "ids": {"tmdb": 80752, "tvdb": 361565, "imdb": "tt7949218"}}])
    movies, shows = ed.watchlist_index()
    assert shows == {361565: ("See (2019)", "tt7949218")}
    assert 80752 not in movies          # the tmdb id must NOT leak into the movie pool


def test_unions_every_account_and_dedups(caches):
    trakt, plex = caches
    _write(trakt / "A" / "watchlist" / "movies.json",
           [{"type": "movie", "movie": {"title": "Drive", "year": 2011,
                                        "ids": {"tmdb": 1, "imdb": "tt0780504"}}}])
    _write(trakt / "B" / "watchlist" / "movies.json",
           [{"type": "movie", "movie": {"title": "Heat", "year": 1995,
                                        "ids": {"tmdb": 2, "imdb": "tt0113277"}}}])
    _write(plex / "users" / "Trizzd" / "watchlist.json",
           [{"title": "Drive", "year": 2011, "type": "movie",
             "ids": {"tmdb": 1, "imdb": "tt0780504"}},
            {"title": "Ronin", "year": 1998, "type": "movie",
             "ids": {"tmdb": 3, "imdb": "tt0122690"}}])
    movies, _ = ed.watchlist_index()
    assert set(movies) == {1, 2, 3}


def test_unreadable_or_empty_watchlist_does_not_fail_the_cycle(caches):
    """Best-effort per file: a corrupt watchlist contributes nothing, it doesn't raise."""
    trakt, plex = caches
    (trakt / "A" / "watchlist").mkdir(parents=True)
    (trakt / "A" / "watchlist" / "movies.json").write_text("{ not json", encoding="utf-8")
    _write(plex / "users" / "Mom" / "watchlist.json", [])
    _write(plex / "users" / "Wyatt" / "watchlist.json",
           [{"title": "Ronin", "type": "movie", "ids": {"tmdb": 3, "imdb": "tt0122690"}}])
    movies, shows = ed.watchlist_index()
    assert movies == {3: ("Ronin", "tt0122690")} and shows == {}


def test_rows_without_a_usable_external_id_are_skipped(caches):
    _, plex = caches
    _write(plex / "users" / "T" / "watchlist.json",
           [{"title": "No ids", "type": "movie"},
            {"title": "Null id", "type": "movie", "ids": {"tmdb": None}},
            {"title": "Good", "type": "movie", "ids": {"tmdb": 7, "imdb": "tt7"}}])
    movies, _ = ed.watchlist_index()
    assert movies == {7: ("Good", "tt7")}


def test_row_with_external_id_but_no_trakt_id_yields_none_fetch(caches):
    """It still enters the pool — enrich_pool is what declines to fetch it."""
    _, plex = caches
    _write(plex / "users" / "T" / "watchlist.json",
           [{"title": "Orphan", "type": "movie", "ids": {"tmdb": 9}}])
    movies, _ = ed.watchlist_index()
    assert movies == {9: ("Orphan", None)}


def test_missing_cache_roots_return_empty(caches):
    assert ed.watchlist_index() == ({}, {})


# ─── recommendations ride in the same tier ────────────────────────────────────

def test_recommendation_rows_are_flat_and_keyed_by_filename(caches):
    """Unlike watchlists, Trakt recommendation rows have no movie/show wrapper — the
    medium comes from the filename, so a shows.json row must land in the show map."""
    trakt, _ = caches
    _write(trakt / "BuckI" / "recommendations" / "movies.json",
           [{"title": "Heat", "year": 1995,
             "ids": {"tmdb": 949, "tvdb": 111, "trakt": 12}}])
    _write(trakt / "BuckI" / "recommendations" / "shows.json",
           [{"title": "Fargo", "year": 2014,
             "ids": {"tmdb": 60622, "tvdb": 269613, "trakt": 34}}])
    movies, shows = ed.recommendation_index()
    assert movies == {949: ("Heat (1995)", 12)}
    assert shows == {269613: ("Fargo (2014)", 34)}     # tvdb key, not the tmdb 60622


def test_recommendation_index_tolerates_wrapped_rows(caches):
    trakt, _ = caches
    _write(trakt / "B" / "recommendations" / "movies.json",
           [{"movie": {"title": "Ronin", "year": 1998,
                       "ids": {"tmdb": 4104, "imdb": "tt0122690"}}}])
    movies, _ = ed.recommendation_index()
    assert movies == {4104: ("Ronin (1998)", "tt0122690")}


def test_recommendation_index_ignores_other_files(caches):
    trakt, _ = caches
    _write(trakt / "B" / "recommendations" / "cursor.json", [{"ids": {"tmdb": 5}}])
    assert ed.recommendation_index() == ({}, {})


def test_recommendations_do_not_displace_watchlist_labels(caches):
    """Both indexes are unioned in run_cycle; the watchlist entry must win on overlap."""
    trakt, _ = caches
    _write(trakt / "B" / "watchlist" / "movies.json",
           [{"type": "movie", "movie": {"title": "Watchlisted",
                                        "ids": {"tmdb": 42, "trakt": 1}}}])
    _write(trakt / "B" / "recommendations" / "movies.json",
           [{"title": "Recommended", "ids": {"tmdb": 42, "trakt": 2}}])
    wl, _ = ed.watchlist_index()
    rec, _ = ed.recommendation_index()
    for ext, entry in rec.items():
        wl.setdefault(ext, entry)
    assert wl == {42: ("Watchlisted", 1)}
