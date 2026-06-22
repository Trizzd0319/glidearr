"""AcquisitionManager._log_elevation_breakdown — the per-title plain-language "why was this
elevated" block logged under the decision table. Names the real drivers (matched genres +
household weights, source feed, rating, votes, year) and the household cast/crew taste profile.
Verifies the named output + that it stays ASCII (cp1252 log sinks)."""
from __future__ import annotations

from scripts.managers.services.acquisition import AcquisitionManager


class _CapLogger:
    def __init__(self): self.lines = []
    def log_info(self, m): self.lines.append(str(m))
    def log_debug(self, *a, **k): pass


class _Scorer:
    def taste_profile(self, k=5):
        return {"genres": ["sci-fi"], "directors": ["Denis Villeneuve"], "actors": ["Zendaya"]}


def _mgr():
    m = object.__new__(AcquisitionManager)
    m.logger = _CapLogger()
    return m


def test_breakdown_names_all_drivers_and_is_ascii():
    m = _mgr()
    elevated = [{
        "title": "Dune: Part Two", "score": 88,
        "evidence": {"matched_genres": [("sci-fi", 0.95), ("action", 0.70)],
                     "source_feed": "trakt_watchlist", "rating10": 8.4,
                     "votes": 412000.0, "year": 2024,
                     "people": {"score": 71.0, "matched": 3}},
    }]
    m._log_elevation_breakdown(elevated, _Scorer())
    text = "\n".join(m.logger.lines)

    assert "Dune: Part Two  (score 88)" in text
    assert "genres: sci-fi(0.95) + action(0.70)" in text
    assert "signals: Trakt watchlist, rating 8.4/10, 412K votes, 2024" in text
    assert "3 household-favourite people on this title (people-affinity 71.0" in text
    assert "context only" not in text                        # people now actually scores
    assert "top directors: Denis Villeneuve" in text and "top cast: Zendaya" in text
    text.encode("cp1252")                                    # no un-encodable unicode


def test_breakdown_handles_no_genre_overlap_and_no_people():
    m = _mgr()
    elevated = [{"title": "Some Older Film", "score": 61,
                 "evidence": {"matched_genres": [], "source_feed": "plex_playlist",
                              "rating10": None, "votes": None, "year": 2011}}]
    m._log_elevation_breakdown(elevated, _Scorer())
    text = "\n".join(m.logger.lines)

    assert "genres: none matched household taste" in text
    assert "signals: Plex playlist, 2011" in text
    assert "cast/crew:" not in text                          # people block omitted


def test_breakdown_names_profile_reason_and_saga():
    m = _mgr()
    elevated = [{
        "title": "Doctor Strange in the Multiverse of Madness", "score": 78,
        "type": "movie", "instance": "standard", "is_anime": False,
        "quality_profile": {"name": "English - UHD Bluray + WEB"},
        "profile_reason": "score 78 picks up to the 2160p tier",
        "saga_names": ["Marvel Cinematic Universe"],
        "evidence": {"matched_genres": [], "source_feed": "trakt_watchlist",
                     "rating10": None, "votes": None, "year": 2022},
    }]
    m._log_elevation_breakdown(elevated, _Scorer())
    text = "\n".join(m.logger.lines)

    assert "saga: part of Marvel Cinematic Universe" in text
    assert ("profile: English - UHD Bluray + WEB  (score 78 picks up to the 2160p tier) "
            "-> standard") in text
    assert "[anime route]" not in text                       # non-anime title
    text.encode("cp1252")                                    # ASCII-safe


def test_breakdown_flags_anime_route_and_omits_absent_profile_saga():
    m = _mgr()
    elevated = [
        {"title": "Ao no Hako", "score": 40, "type": "show", "instance": "standard",
         "route_category": "anime", "quality_profile": {"name": "[Anime] Remux-1080p"},
         "profile_reason": "score 40 picks up to the 1080p tier",
         "evidence": {"matched_genres": [], "source_feed": "mal_plantowatch",
                      "rating10": None, "votes": None, "year": 2024}},
        {"title": "Bare Title Only", "score": 22,
         "evidence": {"matched_genres": [], "source_feed": "trakt_watchlist",
                      "rating10": None, "votes": None, "year": 2010}},
    ]
    m._log_elevation_breakdown(elevated, _Scorer())
    text = "\n".join(m.logger.lines)

    assert "[anime route]" in text                           # route_category=anime flagged
    assert "Bare Title Only  (score 22)" in text
    # the second title carries no profile/saga keys → those lines are simply omitted (no crash)
    assert text.count("profile:") == 1 and "saga:" not in text


def test_votes_and_feed_humanizers():
    assert AcquisitionManager._fmt_votes(412000) == "412K votes"
    assert AcquisitionManager._fmt_votes(1_500_000) == "1.5M votes"
    assert AcquisitionManager._fmt_votes(980) == "980 votes"
    assert AcquisitionManager._fmt_votes(None) == ""
    assert AcquisitionManager._fmt_feed("trakt_recommendations") == "Trakt recommendations"
    assert AcquisitionManager._fmt_feed("mal_plantowatch") == "MAL plantowatch"
    assert AcquisitionManager._fmt_feed(None) == ""
