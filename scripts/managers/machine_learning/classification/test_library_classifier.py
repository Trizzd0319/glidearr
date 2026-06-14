"""Tests for the Kids/Family classification rules (2026-06-11 revision).

Pins the behaviour:
  • the "Family requires Animation" gate is GONE — a live-action Family show counts
    as Kids on its own, so curated family shows aren't evicted to Series;
  • but the SOFT Family genre is rating-gated: it routes to Kids only when kid-safe
    rated (≤ TV-PG/PG) or unrated, so adult "family drama" (TV-14/TV-MA) stays out
    of the Kids library;
  • a HARD Children/Kids/Preschool genre always wins — it beats the lifestyle/reality
    veto AND the rating gate (a 'Children, Food' kids-cooking show is still kids).
"""
from __future__ import annotations

from scripts.managers.machine_learning.classification.library_classifier import (
    classify_movie_explained,
    classify_show_explained,
)


def _show(genres, cert="", **kw):
    return classify_show_explained(genres=genres, certification=cert, **kw)[0]


def _movie(genres, cert="", **kw):
    return classify_movie_explained(genres=genres, certification=cert, **kw)[0]


# ── live-action Family is no longer evicted to Series (animation gate dropped) ──
def test_family_unrated_is_kids():
    assert _show(["Drama", "Family"]) == "kids"
    assert _show(["Adventure", "Comedy", "Family"]) == "kids"


def test_family_kid_safe_rating_is_kids():
    assert _show(["Comedy", "Family"], "TV-PG") == "kids"
    assert _show(["Drama", "Family"], "TV-G") == "kids"
    assert _show(["Comedy", "Family"], "PG") == "kids"


# ── the rating gate keeps adult "family drama" OUT of Kids ──────────────────────
def test_family_adult_rated_is_not_kids():
    assert _show(["Drama", "Family", "Fantasy"], "TV-14") == "series"   # His Dark Materials
    assert _show(["Drama", "Family", "Mystery"], "TV-MA") == "series"   # Apples Never Fall
    assert _show(["Comedy", "Family"], "TV-MA") == "series"             # #blackAF
    assert _show(["Comedy", "Family"], "16") == "series"


# ── hard Children/Kids genre beats BOTH the lifestyle veto and the rating gate ──
def test_children_genre_beats_lifestyle_veto():
    assert _show(["Children", "Food"], "TV-Y") == "kids"                # The Tiny Chef Show
    assert _show(["Children", "Drama", "Soap"]) == "kids"               # kids telenovela
    # even adult-rated, an explicit Children tag is honoured
    assert _show(["Children", "Drama"], "TV-14") == "kids"


def test_preschool_beats_anime_and_veto():
    # Preschool + Japanese animation → Kids (preschool beats anime)
    assert _show(["Preschool", "Animation"], original_language="Japanese") == "kids"
    # Preschool + a lifestyle-veto genre → still Kids
    assert _show(["Preschool", "Food"]) == "kids"


# ── soft Family is still blocked by the lifestyle/reality veto ──────────────────
def test_family_with_reality_veto_is_not_kids():
    # Family + Reality → the reality library, not Kids (veto blocks the soft family route)
    assert _show(["Family", "Reality"]) == "reality"
    # Family + Soap (vetoed) → not kids
    assert _show(["Comedy", "Family", "Soap"], "TV-PG") == "series"


# ── anime still beats the (soft) family/kids genres ────────────────────────────
def test_anime_beats_family():
    assert _show(["Anime", "Family"], "TV-PG") == "anime"


# ── movies: same Family rating gate (kids-friendly films stay, adult drama out) ─
def test_movie_family_rating_gate():
    assert _movie(["Family", "Comedy"], "PG") == "kids"
    assert _movie(["Family", "Adventure"]) == "kids"                    # unrated → kids
    assert _movie(["Family", "Drama"], "R") == "standard"               # adult → out of kids
    assert _movie(["Family", "Drama"], "PG-13") == "standard"           # > PG → out of kids
    # hard Children genre still wins regardless of rating
    assert _movie(["Children", "Family"], "R") == "kids"


# ── movies: NO bare-cert route — a G/PG certificate ALONE must not route to Kids ──
def test_movie_g_pg_cert_alone_is_not_kids():
    # Rating inflation means classics/franchises carry G/PG but aren't kids films.
    assert _movie(["Action", "Adventure", "Science Fiction", "Thriller"], "PG") == "standard"  # Star Trek II
    assert _movie(["Adventure", "History", "War"], "PG") == "standard"                          # Lawrence of Arabia
    assert _movie(["Drama", "Romance", "War"], "G") == "standard"                               # Gone with the Wind
    assert _movie(["Comedy"], "PG") == "standard"                                               # bare PG comedy


def test_tv_cert_route_unchanged():
    # TV KEEPS its certificate route — a TV-G/TV-Y7 show with no genre signal is still Kids.
    assert _show(["Comedy"], "TV-G") == "kids"
    assert _show(["Adventure"], "TV-Y7") == "kids"


# ── movies: adult-genre veto blocks the soft Family route ───────────────────────
def test_movie_family_adult_genre_veto():
    assert _movie(["Family", "War"], "PG") == "standard"          # PG 'Family, War' classic → out
    assert _movie(["Family", "Crime", "Drama"]) == "standard"     # family-tagged crime → out
    assert _movie(["Family", "Thriller"], "PG") == "standard"
    assert _movie(["Family", "Horror"]) == "standard"
    # a hard kids genre still wins even with an adult-signal genre present
    assert _movie(["Children", "Crime"], "PG") == "kids"
    # plain kid-safe family with NO adult genre still routes to Kids
    assert _movie(["Family", "Comedy"], "PG") == "kids"


# ── 'NR' / 'Not Rated' normalises to UNRATED (rescues kid-safe Family titles) ────
def test_nr_cert_treated_as_unrated():
    # Disney animated shorts carry the literal cert "NR" — must still reach Kids via Family.
    assert _movie(["Animation", "Family", "Comedy"], "NR") == "kids"
    assert _movie(["Animation", "Music", "Family"], "Not Rated") == "kids"
    assert _show(["Comedy", "Family"], "NR") == "kids"
