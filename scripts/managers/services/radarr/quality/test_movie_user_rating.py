"""radarr/quality/test_movie_user_rating.py — Group-A4 on the MOVIE side.

THE GAP THIS CLOSES. ``score_movie`` has accepted a ``user_rating`` kwarg since it was
written, and NOTHING ever passed it: ``build_movie_feature_row`` had no such field and
``score_movie_features`` never forwarded one, so every movie in this library reported
``A4_user_rating: 0.0`` — 1,997 of 1,997 — while ``score_show`` has read
``trakt/{user}/ratings/shows`` through ``_build_user_show_rating_map`` from its first line.
64 personally-rated films sat in ``trakt/{user}/ratings/movies`` and nothing read them.

Three things are pinned here:

  * the movie map MIRRORS the show map (same cache namespace, same username fallback,
    same filter) — the two feed one shared formula, so divergence would mean a 7/10 counted
    differently for a film than for a series;
  * PRECEDENCE is deterministic and legible — Trakt is A4's only movie source, the
    F-group critic ratings are a different question and are untouched, and the breakdown
    still says which term produced what;
  * the ratings map reaches the score MEMO's context hash, or a re-rated film would keep
    serving the score it had when it was unrated (the exact failure the A5 watchlist
    fingerprint exists to prevent, one signal over).
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.features.movie_features import (
    build_movie_feature_row,
    score_movie_features,
)
from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager

_TMDB = 5150


class _Cache:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, *a, **k):
        return self.data.get(key)

    def set(self, key, value, *a, **k):
        self.data[key] = value
        return True


class _Logger:
    def __init__(self):
        self.infos: list = []

    def log_info(self, m):
        self.infos.append(str(m))

    log_warning = log_info

    def log_debug(self, m):
        pass

    def log_error(self, m):
        pass

    def log_table(self, *a, **k):
        pass

    def log_grid(self, *a, **k):
        pass


def _rating_row(tmdb, rating):
    return {"rated_at": "2026-07-10T11:23:38.000Z", "rating": rating, "type": "movie",
            "movie": {"title": "T", "year": 2020, "ids": {"trakt": 1, "tmdb": tmdb}}}


def _mgr(cache, config=None):
    m = object.__new__(RadarrSpacePressureManager)
    m.global_cache = cache
    m.logger = _Logger()
    m.config = config if config is not None else {"scoring": {"show_score_memo_audit_pct": 0.0}}
    m.registry = None
    m.radarr_api = None
    m.dry_run = True
    return m


def _df():
    return pd.DataFrame([{
        "tmdb_id": _TMDB, "movie_id": 1, "movie_file_id": 10, "title": "Rated Film",
        "year": 2020, "genres": "Drama", "percent_complete": 0.0, "watch_count": 0,
        "resolution": 1080, "video_codec": "h264", "size_bytes": 5 * 1024 ** 3,
        "runtime_minutes": 120, "keep_policy": None, "is_franchise_entry": False,
        "has_file": True, "is_available": True,
        "physical_release_date": "2020-06-01T00:00:00Z",
        "digital_release_date": "2020-06-01T00:00:00Z", "imdb_rating": 7.0,
    }])


def _score(cache, config=None):
    mgr = _mgr(cache, config)
    out = mgr._build_score_map(_df(), "standard")
    return list(out.values())[0], mgr.logger.infos


# ── 1. the map mirrors the show map ───────────────────────────────────────────

def test_the_movie_rating_map_reads_the_same_namespace_the_show_map_does():
    mgr = _mgr(_Cache({"trakt/BuckITrizzd/ratings/movies": [_rating_row(_TMDB, 9)]}),
               config={"trakt": {"username": "BuckITrizzd"}})
    assert mgr._build_user_movie_rating_map() == {_TMDB: 9.0}


def test_a_blank_username_falls_back_to_default_exactly_as_the_writer_does():
    """TraktRatingsManager namespaces on ``.get("username", "default")``; a reader with a
    different fallback silently misses every key it wrote."""
    for cfg in ({}, {"trakt": {}}, {"trakt": {"username": ""}}, None):
        mgr = _mgr(_Cache({"trakt/default/ratings/movies": [_rating_row(_TMDB, 8)]}), config=cfg)
        assert mgr._build_user_movie_rating_map() == {_TMDB: 8.0}, cfg


def test_rows_without_a_tmdb_id_or_a_rating_are_skipped_not_crashed_on():
    cache = _Cache({"trakt/default/ratings/movies": [
        {"movie": {"ids": {}}, "rating": 9},                 # no tmdb
        {"movie": {"ids": {"tmdb": 7}}},                     # no rating
        {"movie": {"ids": {"tmdb": 8}}, "rating": 0},        # 0 is "unrated" on Trakt
        "junk", None,
        _rating_row(_TMDB, 10),
    ]})
    assert _mgr(cache)._build_user_movie_rating_map() == {_TMDB: 10.0}


def test_an_absent_ratings_cache_yields_an_empty_map_not_an_error():
    assert _mgr(_Cache())._build_user_movie_rating_map() == {}
    m = _mgr(_Cache())
    m.global_cache = None
    assert m._build_user_movie_rating_map() == {}


def test_the_movie_map_and_the_show_map_agree_field_for_field():
    """Same shape, same precedence — the twin implementations must not drift."""
    from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager
    show = object.__new__(SonarrCacheEpisodeFilesManager)
    show.global_cache = _Cache({"trakt/default/ratings/shows": [
        {"rating": 7, "show": {"ids": {"tvdb": 247808}}}]})
    show.config = {}
    movie = _mgr(_Cache({"trakt/default/ratings/movies": [_rating_row(_TMDB, 7)]}))
    assert show._build_user_show_rating_map() == {247808: 7.0}
    assert movie._build_user_movie_rating_map() == {_TMDB: 7.0}


# ── 2. it reaches the score ───────────────────────────────────────────────────

def test_a4_fires_for_a_rated_movie_through_the_real_score_pass():
    unrated = _Cache()
    rated = _Cache({"trakt/default/ratings/movies": [_rating_row(_TMDB, 10)]})
    assert _score(rated)[0] > _score(unrated)[0]


def test_a4_is_symmetric_about_five_and_penalises_a_panned_film():
    """The shared formula: linear about 5/10, +10 at 10/10, floored at -5. A 2/10 the
    household actually sat through is a real negative keep signal."""
    fr = build_movie_feature_row(_df().iloc[0].to_dict(), user_rating=10.0)
    _, hi = score_movie_features(fr, genre_affinity={}, watched_tmdb_ids=set(),
                                 collection_members={}, return_breakdown=True)
    fr = build_movie_feature_row(_df().iloc[0].to_dict(), user_rating=5.0)
    _, mid = score_movie_features(fr, genre_affinity={}, watched_tmdb_ids=set(),
                                  collection_members={}, return_breakdown=True)
    fr = build_movie_feature_row(_df().iloc[0].to_dict(), user_rating=2.0)
    _, low = score_movie_features(fr, genre_affinity={}, watched_tmdb_ids=set(),
                                  collection_members={}, return_breakdown=True)
    assert hi["A4_user_rating"] == 10.0
    assert mid["A4_user_rating"] == 0.0                 # 5/10 is "no opinion", not a bonus
    assert low["A4_user_rating"] == -5.0                # clamped, not -6


def test_an_unrated_movie_is_byte_identical_to_before_the_thread():
    """1,944 of this library's 1,997 films are unrated. They must score EXACTLY as they
    did — the whole change has to be invisible to them."""
    row = _df().iloc[0].to_dict()
    plain = score_movie_features(build_movie_feature_row(row), genre_affinity={},
                                 watched_tmdb_ids=set(), collection_members={},
                                 return_breakdown=True)
    threaded = score_movie_features(build_movie_feature_row(row, user_rating=None),
                                    genre_affinity={}, watched_tmdb_ids=set(),
                                    collection_members={}, return_breakdown=True)
    assert plain == threaded
    assert plain[1]["A4_user_rating"] == 0.0


# ── 3. precedence ─────────────────────────────────────────────────────────────

def test_precedence_trakt_is_a4s_only_movie_source_and_critics_stay_in_group_f():
    """DETERMINISM, stated explicitly. A title with a Trakt rating AND critic ratings has
    ONE answer for each term: A4 reads the household's own verdict, F1 reads the world's.
    They are separate lines in the breakdown and neither overwrites the other."""
    row = _df().iloc[0].to_dict()
    row.update({"imdb_rating": 8.5, "tmdb_rating": 8.0, "trakt_rating": 8.2,
                "metacritic_score": 88.0, "rotten_tomatoes_score": 95.0})
    fr = build_movie_feature_row(row, user_rating=3.0)
    _, bd = score_movie_features(fr, genre_affinity={}, watched_tmdb_ids=set(),
                                 collection_members={}, return_breakdown=True)
    assert bd["A4_user_rating"] == -4.0        # the household disliked it
    assert bd["F1_critic_consensus"] > 0       # …the critics did not; both are reported
    # and the household's verdict does not silently rewrite the critic term
    fr_unrated = build_movie_feature_row(row)
    _, bd0 = score_movie_features(fr_unrated, genre_affinity={}, watched_tmdb_ids=set(),
                                  collection_members={}, return_breakdown=True)
    assert bd["F1_critic_consensus"] == bd0["F1_critic_consensus"]


def test_the_breakdown_stays_legible_about_which_term_won():
    row = _df().iloc[0].to_dict()
    fr = build_movie_feature_row(row, user_rating=9.0)
    total, bd = score_movie_features(fr, genre_affinity={}, watched_tmdb_ids=set(),
                                     collection_members={}, return_breakdown=True)
    assert bd["A4_user_rating"] == 8.0
    assert bd["_total_final"] == total
    assert abs(sum(v for k, v in bd.items() if not k.startswith("_")) - bd["_total_raw"]) < 0.01


def test_the_service_looks_the_rating_up_by_tmdb_id_not_by_row_order():
    cache = _Cache({"trakt/default/ratings/movies": [_rating_row(_TMDB + 1, 10)]})
    assert _score(cache)[0] == _score(_Cache())[0]      # a DIFFERENT film's rating: no effect


# ── 4. memo invalidation ──────────────────────────────────────────────────────

def test_rating_a_movie_invalidates_its_memoized_score():
    """THE ONE THAT MATTERS. The rating lives in ``trakt/{user}/ratings/movies``, outside
    the parquet — ``_h(row)`` cannot see it. Without the ratings map in the CONTEXT hash,
    rating a film you already own would never move its score and A4 would look dead."""
    cache = _Cache()
    before, _ = _score(cache)
    assert "radarr/standard/movie_score_memo" in cache.data

    cache.data["trakt/default/ratings/movies"] = [_rating_row(_TMDB, 10)]   # the only change
    after, infos = _score(cache)
    assert after > before, (before, after)
    assert not any("1/1 unchanged" in i for i in infos), infos


def test_changing_an_existing_rating_invalidates_it_too():
    cache = _Cache({"trakt/default/ratings/movies": [_rating_row(_TMDB, 10)]})
    loved, _ = _score(cache)
    cache.data["trakt/default/ratings/movies"] = [_rating_row(_TMDB, 2)]
    hated, infos = _score(cache)
    assert hated < loved
    assert not any("1/1 unchanged" in i for i in infos), infos


def test_removing_a_rating_invalidates_it_too():
    cache = _Cache({"trakt/default/ratings/movies": [_rating_row(_TMDB, 10)]})
    rated, _ = _score(cache)
    cache.data["trakt/default/ratings/movies"] = []
    unrated, _ = _score(cache)
    assert unrated < rated


def test_an_unchanged_ratings_map_still_HITS_the_memo():
    """The other half: a key that churned every run would cost more than it saves — a full
    reseed takes this household's 39s run to 211s."""
    cache = _Cache({"trakt/default/ratings/movies": [_rating_row(_TMDB, 10)]})
    _score(cache)
    _, infos = _score(cache)
    assert any("1/1 unchanged" in i for i in infos), infos
