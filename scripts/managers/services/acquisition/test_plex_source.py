"""Acquisition Plex-watchlist source seam (DESIGN §5.2).

The union is already GUID-resolved by the fetcher, so its ids de-dupe cleanly: a
Plex+Trakt overlap collapses to ONE candidate, and because Plex is gathered first
its stronger explicit intent (source=plex_watchlist) wins. scorer ranks it top-tier."""
from __future__ import annotations

from scripts.managers.services.acquisition.candidates import CandidateGatherer
from scripts.managers.services.acquisition.scorer import _SOURCE_SCORE


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


class _WL:
    def __init__(self, union): self._u = union
    def acquisition_candidates(self): return self._u


class _Plex:
    def __init__(self, union): self.watchlist = _WL(union)


def test_plex_source_normalized_shape():
    union = [{"title": "Dune", "year": 2021, "type": "movie",
              "ids": {"tmdb": 438631, "tvdb": None, "imdb": "tt1160419"},
              "watchlisted_by": ["Rob"]}]
    g = CandidateGatherer(trakt=None, mal=None, logger=_Logger(), sources_cfg={},
                          plex=_Plex(union))
    out = g._plex()
    assert out == [{
        "title": "Dune", "year": 2021, "type": "movie",
        "ids": {"trakt": None, "tvdb": None, "tmdb": 438631, "imdb": "tt1160419"},
        "genres": [], "rating": None, "votes": None, "runtime": None,
        "source": "plex_watchlist", "is_anime": False,
    }]


def test_plex_disabled_when_source_flag_off():
    g = CandidateGatherer(None, None, _Logger(), {"plex_watchlist": False},
                          plex=_Plex([{"title": "X", "ids": {"tmdb": 1}}]))
    assert g.gather() == []


def test_plex_wins_dedup_against_trakt():
    union = [{"title": "Dune", "type": "movie", "ids": {"tmdb": 438631}, "watchlisted_by": ["Rob"]}]

    class _TraktAPI:
        class watchlist:
            @staticmethod
            def get_watchlist_shows(): return []
            @staticmethod
            def get_watchlist_movies():
                return [{"movie": {"title": "Dune", "year": 2021,
                                   "ids": {"tmdb": 438631, "imdb": "tt1160419"}}}]
        class recommendations:
            @staticmethod
            def get_recommendations_shows(n): return []
            @staticmethod
            def get_recommendations_movies(n): return []

    trakt = type("T", (), {"trakt_api": _TraktAPI()})()
    g = CandidateGatherer(trakt, None, _Logger(),
                          {"plex_watchlist": True, "trakt_watchlist": True,
                           "trakt_recommendations": False}, plex=_Plex(union))
    out = g.gather()
    dune = [c for c in out if c["title"] == "Dune"]
    assert len(dune) == 1                                  # deduped to one
    assert dune[0]["source"] == "plex_watchlist"          # Plex (gathered first) wins


def test_scorer_ranks_plex_watchlist_top_tier():
    assert _SOURCE_SCORE["plex_watchlist"] == 100
