"""File-aware G1 language gate, PER-EPISODE: a series is penalised in proportion to
the fraction of episodes that have NEITHER a preferred-language audio (dub) NOR
subtitle (sub) track. English on only some episodes does NOT pass the whole series.
Byte-identical to the legacy household penalty when no fraction is passed (gate off)."""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.features.show_features import _consumable_fraction
from scripts.managers.machine_learning.scoring._shared import preferred_language_available
from scripts.managers.machine_learning.scoring.movie_scorer import score_movie
from scripts.managers.machine_learning.scoring.show_scorer import score_show


# ── pure helper (per-file) ───────────────────────────────────────────────────
def test_helper_english_dub():
    assert preferred_language_available("jpn/eng", "eng/eng", ["en"]) is True


def test_helper_english_subs_only():
    assert preferred_language_available("jpn", "eng/eng", ["en"]) is True      # Demon Slayer (sub)


def test_helper_no_english_anywhere():
    assert preferred_language_available("jpn", "jpn", ["en"]) is False


# ── per-episode fraction (the union-is-wrong fix) ────────────────────────────
def test_fraction_counts_each_episode():
    # ep1 English dub, ep2 English subs, ep3 Japanese-only → 2 of 3 watchable
    df = pd.DataFrame({"audio_languages": ["jpn/eng", "jpn", "jpn"],
                       "subtitles":       ["eng",     "eng", "jpn"]})
    assert _consumable_fraction(df) == 2 / 3


def test_fraction_rejects_the_union_trap():
    # English dub on ONLY episode 1 of 5 → 1/5 watchable, NOT "the series has English"
    df = pd.DataFrame({"audio_languages": ["jpn/eng", "jpn", "jpn", "jpn", "jpn"],
                       "subtitles":       ["jpn",     "jpn", "jpn", "jpn", "jpn"]})
    assert _consumable_fraction(df) == 1 / 5


def test_fraction_none_when_no_track_columns():
    assert _consumable_fraction(pd.DataFrame({"x": [1, 2]})) is None


# ── show G1 (proportional) ───────────────────────────────────────────────────
def _show_g1(orig, frac):
    _, bd = score_show({"original_language": orig, "genres": [], "network": "", "certification": ""},
                       credits={}, genre_affinity={}, preferred_languages=["en"],
                       language_consumable_fraction=frac, return_breakdown=True)
    return bd["G1_language"]


def test_show_all_consumable_zero():
    assert _show_g1("Japanese", 1.0) == 0.0


def test_show_none_consumable_full_penalty():
    assert _show_g1("Japanese", 0.0) == -8.0


def test_show_proportional():
    assert _show_g1("Japanese", 0.5) == -4.0
    assert _show_g1("Japanese", 0.9) == -0.8


def test_show_byte_identical_legacy_when_fraction_none():
    assert _show_g1("Japanese", None) == -8.0          # legacy (empty histogram)
    assert _show_g1("English", None) == 0.0            # preferred origin never penalised


# ── movie G1 (binary single file) ────────────────────────────────────────────
def _movie_g1(orig, frac):
    _, bd = score_movie({"tmdbId": 1, "genres": []}, 0.0, 0.9, {}, set(), {}, {},
                        original_language=orig, preferred_languages=["en"],
                        language_consumable_fraction=frac, return_breakdown=True)
    return bd["G1_language"]


def test_movie_consumable_zero_else_penalty():
    assert _movie_g1("Korean", 1.0) == 0.0             # has English dub/sub
    assert _movie_g1("Korean", 0.0) == -8.0            # neither → re-acquire candidate
    assert _movie_g1("Japanese", None) == -8.0         # legacy when no track data
