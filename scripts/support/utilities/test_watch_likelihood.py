"""
Unit tests for watch_likelihood.py — the max(engagement, affinity) likelihood model,
the explicit Radarr profile-id ladder, and the Sonarr resolution cap.

    python -m scripts.support.utilities.test_watch_likelihood

Locks in:
  * affinity alone (unwatched) can reach HIGH-4K but never TOP-4K (id 10);
  * TOP-4K is reserved for REWATCHED content;
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
    _check("rewatched -> >=85 (top-4K band)", watch_likelihood({"watch_count": 3}) >= 85)
    _check("watched once, low affinity -> 50", watch_likelihood({"watch_count": 1, "watchability_score": 0}) == 50.0)
    _check("started (50%) -> 40", watch_likelihood({"completion_pct": 50, "watchability_score": 0}) == 40.0)
    _check("abandoned (10%) <= 25", watch_likelihood({"completion_pct": 10, "watchability_score": 5}) <= 25.0)
    # Engagement + affinity DON'T invert: watched-once with strong affinity climbs.
    L = watch_likelihood({"watch_count": 1, "watchability_score": 60})
    _check("watched + high affinity climbs above floor", L > 50, f"L={L}")


def test_affinity_caps_below_top4k():
    print("test_affinity_caps_below_top4k:")
    # Cold unwatched (Alien): low score -> low -> floor profile (HD-720p id 3).
    alien = {"watch_count": 0, "is_watched": False, "completion_pct": 0, "watchability_score": 5}
    _check("Alien -> floor profile (3)", profile_id_for_likelihood(watch_likelihood(alien)) == 3)
    # Max-affinity UNWATCHED can reach high-4K (9) but NEVER top-4K (10).
    hot = {"watch_count": 0, "is_watched": False, "completion_pct": 0, "watchability_score": 100}
    Lh = watch_likelihood(hot)
    _check("hot unwatched capped < 85 (top-4K)", Lh < 85, f"L={Lh}")
    _check("hot unwatched reaches high-4K (9)", profile_id_for_likelihood(Lh) == 9, f"L={Lh}->{profile_id_for_likelihood(Lh)}")


def test_top4k_reserved_for_rewatch():
    print("test_top4k_reserved_for_rewatch:")
    _check("rewatched -> top-4K (10)", profile_id_for_likelihood(watch_likelihood({"watch_count": 2})) == 10)
    _check("watched once -> high-1080 (7)", profile_id_for_likelihood(watch_likelihood({"watch_count": 1})) == 7)


def test_radarr_ladder():
    print("test_radarr_ladder:")
    cases = [(0, 3), (19, 3), (20, 4), (30, 6), (40, 7), (55, 8), (65, 5), (70, 9), (85, 10), (100, 10)]
    for L, pid in cases:
        _check(f"L={L} -> profile {pid}", profile_id_for_likelihood(L) == pid, f"got {profile_id_for_likelihood(L)}")
    # Rank is ascending in the ladder; absent ids -> -1.
    _check("rank(3) < rank(7) < rank(10)", ladder_rank(3) < ladder_rank(7) < ladder_rank(10))
    _check("rank(absent id 99) == -1", ladder_rank(99) == -1)
    _check("rank(None) == -1", ladder_rank(None) == -1)


def test_sonarr_resolution_cap():
    print("test_sonarr_resolution_cap:")
    _check("L>=70 -> 2160", resolution_cap_for_likelihood(70) == 2160)
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
    # Untouched titles spread by percentile rank (percentile mode, floor 0).
    _check("pct 100 untouched -> high-4K (9)", profile_id_for_likelihood(watch_likelihood({"watchability_percentile": 100}, config=PCT)) == 9)
    _check("pct 50 untouched -> mid-1080 (6)", profile_id_for_likelihood(watch_likelihood({"watchability_percentile": 50}, config=PCT)) == 6)
    _check("pct 0 untouched -> floor (3)", profile_id_for_likelihood(watch_likelihood({"watchability_percentile": 0}, config=PCT)) == 3)
    # Engagement floor still overrides a low percentile.
    _check("rewatch overrides low pct -> top-4K (10)", profile_id_for_likelihood(watch_likelihood({"watch_count": 2, "watchability_percentile": 0}, config=PCT)) == 10)
    # Floor knob: only the top (100-floor)% climb.
    cfg = {"watch_likelihood": {"untouched_mode": "percentile", "untouched_pct_floor": 60}}
    _check("floor=60: pct 60 -> floor (3)", profile_id_for_likelihood(watch_likelihood({"watchability_percentile": 60}, config=cfg), config=cfg) == 3)
    _check("floor=60: pct 100 -> high-4K (9)", profile_id_for_likelihood(watch_likelihood({"watchability_percentile": 100}, config=cfg), config=cfg) == 9)
    # DEFAULT (absolute) ignores the percentile column and uses the score.
    _check("default absolute -> uses score not pct", profile_id_for_likelihood(watch_likelihood({"watchability_percentile": 100, "watchability_score": 0})) == 3)
    # Percentile mode with no column -> falls back to absolute (score-based).
    _check("percentile mode, no column -> absolute fallback", watch_likelihood({"watchability_score": 5}, config=PCT) == 17.0)


if __name__ == "__main__":
    test_likelihood_model()
    test_affinity_caps_below_top4k()
    test_top4k_reserved_for_rewatch()
    test_radarr_ladder()
    test_sonarr_resolution_cap()
    test_config_override()
    test_percentile_mode()
    print("\nAll watch_likelihood tests passed")
