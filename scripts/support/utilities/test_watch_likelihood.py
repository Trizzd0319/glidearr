"""
Unit tests for watch_likelihood.py — the max(engagement, affinity) likelihood model,
the explicit Radarr profile-id ladder, and the Sonarr resolution cap.

    python -m scripts.support.utilities.test_watch_likelihood

Locks in (recalibrated curve, 2026-06):
  * affinity alone (unwatched) tops out at the 1080 ceiling (id 8) and NEVER reaches 4K;
  * TOP-4K (id 10) is reserved for REGULAR rewatches (watch_count >= 4);
  * a cold unwatched title (Alien) stays at the floor profile (HD-720p);
  * engagement and affinity don't invert (watched + high affinity climbs).
"""
from __future__ import annotations

from scripts.support.utilities.watch_likelihood import (
    ladder_rank,
    profile_id_for_likelihood,
    resolution_cap_for_likelihood,
    watch_likelihood,
)


def _check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(f"{name}: {detail}")


def test_likelihood_model():
    print("test_likelihood_model:")
    _check("rewatched 3x -> 78 (graded floor)", watch_likelihood({"watch_count": 3}) == 78.0)
    _check("rewatched 4x -> top-4K band (>=90)", watch_likelihood({"watch_count": 4}) >= 90)
    _check("watched once, low affinity -> 50", watch_likelihood({"watch_count": 1, "watchability_score": 0}) == 50.0)
    _check("started (50%) -> 40", watch_likelihood({"completion_pct": 50, "watchability_score": 0}) == 40.0)
    _check("abandoned (10%) <= 25", watch_likelihood({"completion_pct": 10, "watchability_score": 5}) <= 25.0)
    # Engagement + affinity DON'T invert: watched-once with strong affinity climbs.
    L = watch_likelihood({"watch_count": 1, "watchability_score": 60})
    _check("watched + high affinity climbs above floor", L > 50, f"L={L}")


def test_affinity_caps_below_4k():
    print("test_affinity_caps_below_4k:")
    # Cold unwatched (Alien): low score -> low -> floor profile (HD-720p id 3).
    alien = {"watch_count": 0, "is_watched": False, "completion_pct": 0, "watchability_score": 5}
    _check("Alien -> floor profile (3)", profile_id_for_likelihood(watch_likelihood(alien)) == 3)
    # Max-affinity UNWATCHED tops out at the 1080 ceiling (id 8) and NEVER reaches 4K — taste != rewatch.
    hot = {"watch_count": 0, "is_watched": False, "completion_pct": 0, "watchability_score": 100}
    Lh = watch_likelihood(hot)
    _check("hot unwatched capped at affinity_cap (75)", Lh == 75, f"L={Lh}")
    _check("hot unwatched reaches 1080 ceiling (8), not 4K", profile_id_for_likelihood(Lh) == 8, f"L={Lh}->{profile_id_for_likelihood(Lh)}")


def test_top4k_reserved_for_rewatch():
    print("test_top4k_reserved_for_rewatch:")
    _check("regular rewatch (4x) -> top-4K (10)", profile_id_for_likelihood(watch_likelihood({"watch_count": 4})) == 10)
    _check("twice-watched -> 1080 (8), not 4K", profile_id_for_likelihood(watch_likelihood({"watch_count": 2})) == 8)
    _check("watched once -> 1080 (7)", profile_id_for_likelihood(watch_likelihood({"watch_count": 1})) == 7)


def test_radarr_ladder():
    print("test_radarr_ladder:")
    cases = [(0, 3), (39, 3), (40, 4), (44, 4), (45, 7), (54, 7), (55, 8), (76, 8),
             (77, 5), (84, 5), (85, 9), (89, 9), (90, 10), (100, 10)]
    for L, pid in cases:
        _check(f"L={L} -> profile {pid}", profile_id_for_likelihood(L) == pid, f"got {profile_id_for_likelihood(L)}")
    # Rank is ascending in the ladder; absent ids -> -1.
    _check("rank(3) < rank(7) < rank(10)", ladder_rank(3) < ladder_rank(7) < ladder_rank(10))
    _check("rank(absent id 99) == -1", ladder_rank(99) == -1)
    _check("rank(None) == -1", ladder_rank(None) == -1)


def test_sonarr_resolution_cap():
    print("test_sonarr_resolution_cap:")
    _check("L>=77 -> 2160", resolution_cap_for_likelihood(77) == 2160)
    _check("L=70 -> 1080 (below uhd_cutoff)", resolution_cap_for_likelihood(70) == 1080)
    _check("L=40 -> 1080", resolution_cap_for_likelihood(40) == 1080)
    _check("L=20 -> 720", resolution_cap_for_likelihood(20) == 720)
    _check("L=0 -> 720", resolution_cap_for_likelihood(0) == 720)
    _check("None -> 720 (safe)", resolution_cap_for_likelihood(None) == 720)


def test_config_override():
    print("test_config_override:")
    cfg = {"radarr_quality_ladder": [[0, 3], [50, 10]]}  # only two rungs
    _check("custom ladder honored (low->3)", profile_id_for_likelihood(10, config=cfg) == 3)
    _check("custom ladder honored (high->10)", profile_id_for_likelihood(60, config=cfg) == 10)


def test_percentile_mode():
    print("test_percentile_mode (Option 1, opt-in):")
    PCT = {"watch_likelihood": {"untouched_mode": "percentile"}}
    # Untouched titles spread by percentile rank (percentile mode, floor 0); affinity caps at 1080.
    _check("pct 100 untouched -> 1080 ceiling (8)", profile_id_for_likelihood(watch_likelihood({"watchability_percentile": 100}, config=PCT)) == 8)
    _check("pct 70 untouched -> mid-1080 (7)", profile_id_for_likelihood(watch_likelihood({"watchability_percentile": 70}, config=PCT)) == 7)
    _check("pct 0 untouched -> floor (3)", profile_id_for_likelihood(watch_likelihood({"watchability_percentile": 0}, config=PCT)) == 3)
    # Engagement floor still overrides a low percentile: a regular rewatch reaches top-4K.
    _check("rewatch overrides low pct -> top-4K (10)", profile_id_for_likelihood(watch_likelihood({"watch_count": 4, "watchability_percentile": 0}, config=PCT)) == 10)
    # Floor knob: only the top (100-floor)% climb.
    cfg = {"watch_likelihood": {"untouched_mode": "percentile", "untouched_pct_floor": 60}}
    _check("floor=60: pct 60 -> floor (3)", profile_id_for_likelihood(watch_likelihood({"watchability_percentile": 60}, config=cfg), config=cfg) == 3)
    _check("floor=60: pct 100 -> 1080 ceiling (8)", profile_id_for_likelihood(watch_likelihood({"watchability_percentile": 100}, config=cfg), config=cfg) == 8)
    # DEFAULT (absolute) ignores the percentile column and uses the score.
    _check("default absolute -> uses score not pct", profile_id_for_likelihood(watch_likelihood({"watchability_percentile": 100, "watchability_score": 0})) == 3)
    # Percentile mode with no column -> falls back to absolute (score-based).
    _check("percentile mode, no column -> absolute fallback", watch_likelihood({"watchability_score": 5}, config=PCT) == 17.0)


if __name__ == "__main__":
    test_likelihood_model()
    test_affinity_caps_below_4k()
    test_top4k_reserved_for_rewatch()
    test_radarr_ladder()
    test_sonarr_resolution_cap()
    test_config_override()
    test_percentile_mode()
    print("\nAll watch_likelihood tests passed")
