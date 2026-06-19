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


# ── shows: CSM age is a kids CEILING ONLY — never routes to Kids by itself ────────
def test_show_csm_age_alone_does_not_route_to_kids():
    # "Never trust Common Sense alone": a low CSM age no longer pulls a show into Kids
    # without a corroborating kids signal. Star Trek: DS9 (adult drama, CSM ~10) → series.
    assert _show(["Drama"], recommended_age=8) == "series"
    assert _show(["Drama", "Science Fiction"], "TV-PG", recommended_age=10) == "series"   # DS9
    # CSM permits but does not promote: a Reality show at CSM 6 stays Reality (no kids signal).
    assert _show(["Reality"], recommended_age=6) == "reality"
    # A corroborating signal is still required AND honoured: a kid-safe cert at a low CSM age → kids.
    assert _show(["Comedy"], "TV-Y7", recommended_age=8) == "kids"


# ── shows: a genuine KIDS NETWORK is a positive kids signal (the Trek franchise split) ──
def test_show_kids_network_routes_to_kids():
    # A kids network routes to Kids even with no kids genre/cert (sparse metadata).
    assert _show(["Drama"], network="Disney Junior") == "kids"
    assert _show(["Adventure"], network="Nickelodeon") == "kids"
    # Star Trek: Prodigy (Nickelodeon kids Trek, CSM ~10) → Kids via the network…
    assert _show(["Science Fiction", "Adventure"], network="Nickelodeon", recommended_age=10) == "kids"
    # …while the adult Treks on general networks (no kids signal) stay in Series.
    assert _show(["Science Fiction", "Drama"], "TV-PG", network="Syndication", recommended_age=10) == "series"
    assert _show(["Science Fiction", "Drama"], "TV-PG", network="Paramount+", recommended_age=10) == "series"


def test_show_kids_network_is_gated():
    # The network route respects the same gates as the cert route.
    assert _show(["Reality"], network="Nickelodeon") == "reality"                     # lifestyle veto wins
    assert _show(["Drama"], network="Cartoon Network", recommended_age=16) == "series"  # CSM over cutoff
    assert _show(["Drama"], "TV-MA", network="Cartoon Network") == "series"           # adult cert (Adult Swim-style)
    assert _show(["Drama"], network="HBO") == "series"                               # not a kids network


def test_show_csm_over_cutoff_blocks_soft_family():
    # Without CSM this kid-safe-rated 'Family' show is Kids; a CSM age over the cutoff
    # (CSM says NOT kids) suppresses the soft-Family kids route → series.
    assert _show(["Drama", "Family"], "TV-PG") == "kids"               # baseline (no CSM)
    assert _show(["Drama", "Family"], "TV-PG", recommended_age=15) == "series"


def test_show_csm_over_cutoff_overrides_hard_genre_and_cert():
    # CSM over the cutoff beats BOTH a hard Children genre and the TV-G cert route.
    assert _show(["Children"], recommended_age=16) == "series"         # hard kids genre suppressed
    assert _show(["Comedy"], "TV-G", recommended_age=16) == "series"   # cert route suppressed


def test_show_csm_over_cutoff_still_reality_and_documentary():
    # CSM>cutoff blocks the KIDS routes but the show must still classify normally elsewhere.
    assert _show(["Reality"], recommended_age=16) == "reality"
    assert _show(["Documentary"], recommended_age=16) == "documentary"


def test_show_preschool_beats_csm():
    # An explicit 'Preschool' GENRE is unambiguous toddler content — it wins even when CSM
    # rates the title older (documented exception: preschool sits ABOVE CSM).
    assert _show(["Preschool"], recommended_age=16) == "kids"


def test_show_anime_beats_csm():
    # Anime precedence is preserved: a kid-rated anime still routes to the Anime library.
    assert _show(["Anime"], recommended_age=8) == "anime"
    assert _show(["Animation"], original_language="Japanese", recommended_age=8) == "anime"


def test_show_no_csm_leaves_genre_cert_flow_unchanged():
    # Regression: with no CSM age the existing genre/cert routing is unchanged.
    assert _show(["Comedy", "Family"], "TV-PG") == "kids"
    assert _show(["Comedy"], "TV-G") == "kids"
    assert _show(["Drama"]) == "series"


# ── movies: CSM age is a kids CEILING ONLY — a kids STUDIO is the only positive ───
def test_movie_csm_age_alone_does_not_route_to_kids():
    # "Never trust Common Sense alone": a low CSM age no longer routes a movie to Kids
    # without a kids studio (genre is not a movie kids route).
    assert _movie(["Drama"], recommended_age=8) == "standard"
    assert _movie(["Family", "Comedy"], "PG", recommended_age=7) == "standard"
    # CSM still DEMOTES: an age over the cutoff blocks even a kids studio.
    assert _movie(["Comedy"], "G", studio="Pixar", recommended_age=15) == "standard"
    # A kids studio at an in-range CSM age → kids (the studio is the positive signal).
    assert _movie(["Comedy"], "G", studio="Pixar", recommended_age=8) == "kids"


# ── movies: GENRE is no longer a kids route (only anime keeps genre/language) ────
def test_movie_genre_is_not_a_kids_route():
    # No CSM age + no kids studio → genre alone never routes a movie to Kids.
    assert _movie(["Family", "Comedy"], "PG") == "standard"
    assert _movie(["Children", "Comedy"]) == "standard"                 # even a hard 'Children' tag
    assert _movie(["Animation", "Family", "Comedy"], "NR") == "standard"
    assert _movie(["Preschool", "Adventure"]) == "standard"


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


# ── movies: kids/family STUDIO is the only fallback when CSM has no rating ───────
def test_movie_studio_fallback_when_no_csm():
    assert _movie(["Comedy"], "G", studio="Pixar") == "kids"               # kid-safe cert + kids studio
    assert _movie(["Comedy"], studio="Walt Disney Pictures") == "kids"     # unrated + kids studio
    assert _movie(["Comedy"], "PG-13", studio="Pixar") == "standard"       # adult cert disqualifies studio
    assert _movie(["Comedy"], "G", studio="A24") == "standard"             # not a kids studio
    # CSM is authoritative: an older CSM age overrides the kids studio.
    assert _movie(["Comedy"], studio="Pixar", recommended_age=15) == "standard"


# ── anime keeps its genre/language route — now including Chinese (donghua) ───────
def test_movie_anime_includes_chinese():
    assert _movie(["Animation"], original_language="Japanese") == "anime"
    assert _movie(["Animation"], original_language="Korean") == "anime"
    assert _movie(["Animation"], original_language="Chinese") == "anime"    # NEW: donghua
    assert _movie(["Anime"]) == "anime"
    # English animation is NOT anime; with no kids studio it is standard (CSM age alone no
    # longer routes a movie to Kids).
    assert _movie(["Animation"], original_language="English", recommended_age=8) == "standard"
    assert _movie(["Animation"], original_language="English") == "standard"


# ── shows: 'NR'/'Not Rated' still normalises to UNRATED for the soft-Family route ─
def test_show_nr_cert_treated_as_unrated():
    # TV keeps genre routing; a kid-safe Family show rated 'NR' still reaches Kids.
    assert _show(["Comedy", "Family"], "NR") == "kids"
    assert _show(["Drama", "Family"], "Not Rated") == "kids"
