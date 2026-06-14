"""Tests for playlists/rationale.explain_reason — the concise 'why' for the preview."""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.rationale import explain_reason

# genre_aff is the {genre: weight} dict the builder passes (NOT the full affinity dict);
# people_aff is the merged actor+director weights (dormant until the library has cast/crew).
_GENRES = {"Drama": 35, "Action": 31, "Comedy": 33}
_PEOPLE = {"sylvester stallone": 12, "ryan coogler": 8}


def test_matched_genres_strongest_first():
    # Drama(35) + Action(31) both match; Sci-Fi absent → "Drama·Action" (by weight desc)
    assert explain_reason(["Action", "Sci-Fi", "Drama"], _GENRES) == "Drama·Action"


def test_jit_leads():
    r = explain_reason(["Comedy"], _GENRES, is_jit=True)
    assert r.startswith("watching now") and "Comedy" in r
    r.encode("cp1252")                                       # must not crash the Windows console handler


def test_cast_crew_when_people_aff_present():
    r = explain_reason(["Drama"], _GENRES, cast=["Sylvester Stallone"], crew=["Ryan Coogler"],
                       people_aff=_PEOPLE, franchise_name="Creed Collection")
    assert "Drama" in r and "Stallone" in r and "Creed Collection" in r


def test_universe_beats_bare_franchise_label():
    r = explain_reason([], _GENRES, franchise_name="Some Collection", universe_name="MCU")
    assert "MCU universe" in r and "Some Collection" not in r


def test_household_fallback_when_no_signal():
    assert explain_reason(["Documentary"], _GENRES) == "household pick"   # genre not in affinity
    assert explain_reason([], {}) == "household pick"


def test_cast_crew_silently_absent_without_people_aff():
    # the common case TODAY: library has no cast/crew (or no people_aff) → reason is the genre
    assert explain_reason(["Drama"], _GENRES, cast=["Someone"], crew=[]) == "Drama"
