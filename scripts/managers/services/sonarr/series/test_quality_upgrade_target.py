"""Regression test: the active-watcher upgrade picks its target profile *within the
series' seriesType family*, so a standard/reality series is never routed onto the
anime quality profile.

Bug: the old code chose a single global `max(resolution)` target. With several 2160p
profiles, the stable sort tie-broke onto whichever sorted last — the `[Anime]` one —
so every actively-watched series (reality, true-crime, documentary) was "upgraded" to
`[Anime] Remux-1080p`.
"""
from __future__ import annotations

from scripts.managers.services.sonarr.series.quality import (
    _is_anime_profile,
    _profile_max_resolution,
    select_upgrade_targets,
)


def _prof(pid: int, name: str, res: int) -> dict:
    return {"id": pid, "name": name,
            "items": [{"allowed": True, "quality": {"resolution": res}}]}


# The real-world Sonarr profile set: three profiles tie at 2160p (Ultra-HD,
# WEB-2160p, [Anime]). The anime one is last by id — exactly the tie-break that
# used to win the global argmax.
_REAL_PROFILES = [
    _prof(2, "SD", 480),
    _prof(4, "HD-1080p", 1080),
    _prof(5, "Ultra-HD", 2160),
    _prof(8, "WEB-2160p (Combined)", 2160),
    _prof(9, "[Anime] Remux-1080p", 2160),
]


def test_standard_target_is_not_the_anime_profile():
    best_standard, best_anime = select_upgrade_targets(_REAL_PROFILES)
    assert best_standard is not None and best_anime is not None
    assert not _is_anime_profile(best_standard), best_standard["name"]
    assert _profile_max_resolution(best_standard) == 2160          # still the best HD target
    assert best_anime["name"] == "[Anime] Remux-1080p"


def test_anime_only_library_falls_back_to_anime_for_both():
    anime_only = [_prof(9, "[Anime] Remux-1080p", 2160)]
    best_standard, best_anime = select_upgrade_targets(anime_only)
    assert best_standard is best_anime
    assert best_anime["name"] == "[Anime] Remux-1080p"


def test_no_anime_profiles_falls_back_to_standard_for_both():
    standard_only = [_prof(4, "HD-1080p", 1080), _prof(5, "Ultra-HD", 2160)]
    best_standard, best_anime = select_upgrade_targets(standard_only)
    assert best_anime is best_standard
    assert best_standard["name"] == "Ultra-HD"


def test_empty_profile_list_returns_none():
    assert select_upgrade_targets([]) == (None, None)


def test_is_anime_profile_matches_bracketed_prefix_only():
    assert _is_anime_profile({"name": "[Anime] Remux-1080p"})
    assert _is_anime_profile({"name": "  [anime] web-1080p  "})   # whitespace + case
    assert not _is_anime_profile({"name": "HD-1080p"})
    assert not _is_anime_profile({"name": "Anime Movies"})        # no brackets
    assert not _is_anime_profile({})                              # missing name


def test_max_resolution_reads_nested_allowed_items_only():
    prof = {"items": [
        {"allowed": False, "quality": {"resolution": 2160}},      # disallowed -> ignored
        {"allowed": True, "items": [
            {"allowed": True, "quality": {"resolution": 1080}},
            {"allowed": False, "quality": {"resolution": 2160}},  # disallowed nested -> ignored
        ]},
    ]}
    assert _profile_max_resolution(prof) == 1080
    assert _profile_max_resolution(None) == 0
