"""
Regression: the watchability breakdown is FREE — return_breakdown adds the
explanation dict but never changes the score.
================================================================================
The explainability feature persists ``score_movie`` / ``score_show``'s breakdown
dict next to the int score. The data layer relies on the score being IDENTICAL
whether or not the breakdown is requested (otherwise the persisted score would
drift from the score the decision paths use). This locks that invariant, plus
``breakdown["_total_final"] == score`` and the presence of every signal-group key.

    python -m scripts.managers.machine_learning.scoring.test_breakdown
"""
from __future__ import annotations

from scripts.managers.machine_learning.scoring._shared import normalize_lang
from scripts.managers.machine_learning.scoring.movie_scorer import score_movie
from scripts.managers.machine_learning.scoring.show_scorer import score_show


def _check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(f"{name}: {detail}")


# Representative movies spanning never-watched, rewatched, acclaimed, penalised.
_MOVIE_CASES = [
    dict(movie={"tmdbId": 1, "genres": ["Action"]}, completion_pct=0.0, completion_threshold=0.9,
         collection_members={}, watched_tmdb_ids=set(), genre_affinity={}, credits={}),
    dict(movie={"tmdbId": 2, "genres": ["Drama"]}, completion_pct=1.0, completion_threshold=0.9,
         collection_members={}, watched_tmdb_ids=set(), genre_affinity={}, credits={},
         watch_count=3, user_rating=9.0, imdb_rating=8.6, keep_policy="keep_movie"),
    dict(movie={"tmdbId": 3, "genres": ["Horror"]}, completion_pct=0.1, completion_threshold=0.9,
         collection_members={}, watched_tmdb_ids=set(), genre_affinity={}, credits={},
         imdb_rating=3.0, original_language="fr", is_available=False),
]

_SHOW_CASES = [
    dict(show={"genres": ["Animation"], "network": "Disney", "certification": "TV-Y"}),
    dict(show={"genres": ["Drama"], "network": "HBO"}, watched_episodes=40, total_episodes=60,
         days_since_last_watch=5, max_episode_watch_count=3, keep_policy="keep_series",
         sonarr_rating=9.1, user_rating=10.0),
    dict(show={"genres": ["Reality"], "original_language": "ko"}, watched_episodes=2,
         total_episodes=50, days_since_last_watch=800, sonarr_rating=3.0),
]

_MOVIE_KEYS = {"A1_keep_policy", "A2_completion", "A3_rewatch", "A4_user_rating",
               "B1_actor_affinity", "B5_studio_affinity", "C3_related_graph",
               "D1_device_capability", "E1_kids_alignment", "F1_critic_consensus",
               "G1_language", "G4_not_available", "_total_raw", "_total_final"}
_SHOW_KEYS = {"A1_keep_policy", "A2_engagement", "A3_rewatch", "A4_user_rating",
              "B1_actor_affinity", "B5_network_affinity", "C3_related_graph",
              "F1_critic_consensus", "G2_abandoned", "_total_final"}


def _assert_free(label, fn, cases, required_keys):
    print(f"{label}:")
    for i, kw in enumerate(cases):
        plain = fn(**kw)
        rich = fn(**kw, return_breakdown=True)
        _check(f"case {i}: plain is int", isinstance(plain, int), f"got {type(plain)}")
        _check(f"case {i}: rich is (int, dict)",
               isinstance(rich, tuple) and isinstance(rich[0], int) and isinstance(rich[1], dict))
        score, bd = rich
        _check(f"case {i}: score byte-identical", plain == score, f"{plain} != {score}")
        _check(f"case {i}: _total_final == score", bd.get("_total_final") == score,
               f"{bd.get('_total_final')} != {score}")
        _check(f"case {i}: 0 <= score <= 100", 0 <= score <= 100, f"score={score}")
        missing = required_keys - set(bd)
        _check(f"case {i}: all signal-group keys present", not missing, f"missing {missing}")


def test_movie_breakdown_is_free():
    _assert_free("test_movie_breakdown_is_free", score_movie, _MOVIE_CASES, _MOVIE_KEYS)


def test_show_breakdown_is_free():
    _assert_free("test_show_breakdown_is_free", score_show, _SHOW_CASES, _SHOW_KEYS)


def test_language_normalization():
    """The G1 penalty compares ISO 639-1, but the *arr APIs hand us the display
    NAME ("English"). normalize_lang bridges them so a preferred-language title
    isn't wrongly penalised, while a genuinely non-preferred one still is."""
    print("test_language_normalization:")
    _check("English name -> en", normalize_lang("English") == "en")
    _check("eng (639-2) -> en", normalize_lang("eng") == "en")
    _check("en passthrough", normalize_lang("en") == "en")
    _check("None -> None", normalize_lang(None) is None)
    _check("unknown passthrough (still non-preferred)", normalize_lang("Klingon") == "klingon")

    def _g1(scorer, **kw):
        return scorer(return_breakdown=True, **kw)[1]["G1_language"]

    # English (display name) against the default preferred ['en'] -> NO penalty.
    _check("English movie not penalised", _g1(
        score_movie, movie={"tmdbId": 1, "genres": ["Drama"]}, completion_pct=0.0,
        completion_threshold=0.9, collection_members={}, watched_tmdb_ids=set(),
        genre_affinity={}, credits={}, original_language="English") == 0.0)
    _check("English show not penalised", _g1(
        score_show, show={"genres": ["Drama"], "original_language": "English"}) == 0.0)
    # Japanese with no household audio history -> still the full −8.
    _check("Japanese movie still penalised", _g1(
        score_movie, movie={"tmdbId": 2, "genres": ["Anime"]}, completion_pct=0.0,
        completion_threshold=0.9, collection_members={}, watched_tmdb_ids=set(),
        genre_affinity={}, credits={}, original_language="Japanese") == -8.0)


if __name__ == "__main__":
    test_movie_breakdown_is_free()
    test_show_breakdown_is_free()
    test_language_normalization()
    print("\nAll breakdown invariant tests passed")
