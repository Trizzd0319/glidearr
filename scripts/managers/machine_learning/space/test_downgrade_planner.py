"""Tests for space.downgrade_planner — STEP-DOWN by resolution tier, spread.

Both planners step the lowest-watchability titles DOWN the resolution ladder one tier at
a time (4K->1080->720), spread across the eligible pool, until ~need_gb is reclaimed —
never crushing one title straight to the floor. Titles floor at ``floor_resolution``
(720); already-at-floor and would-upgrade (first-tier reclaim<=0) titles skip.

Ladder: 480/720/1080/2160 (ids 10/11/12/13). With unknown quality names the size model
falls back to the resolution table (480->12, 720->30, 1080->70, 2160->200 MiB/min), so a
100-min item: est(GiB)=rate*100/1024 -> 2160~19.5, 1080~6.84, 720~2.93.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from scripts.managers.machine_learning.space.downgrade_planner import (
    _profile_max_res,
    plan_movie_downgrades,
    plan_series_downgrades,
    step_targets,
)

_CUTOFF = datetime(2026, 1, 1, tzinfo=timezone.utc)
_GIB = 1024 ** 3

# representative profile per resolution tier (id encodes the tier)
_RANKED = [
    {"id": 10, "name": "P480",  "items": [{"allowed": True, "quality": {"resolution": 480,  "name": "q480"}}]},
    {"id": 11, "name": "P720",  "items": [{"allowed": True, "quality": {"resolution": 720,  "name": "q720"}}]},
    {"id": 12, "name": "P1080", "items": [{"allowed": True, "quality": {"resolution": 1080, "name": "q1080"}}]},
    {"id": 13, "name": "P2160", "items": [{"allowed": True, "quality": {"resolution": 2160, "name": "q2160"}}]},
]


# ── step_targets precompute parity (B2: profile_res hoisted once per plan) ───────
def test_step_targets_precomputed_profile_res_is_byte_identical():
    # The planners now pass profile_res (max-resolution per profile, computed once) to
    # avoid re-walking every profile per title. The precomputed path must produce the
    # EXACT same (targets, cum) as recomputing inside step_targets.
    def _est(p):
        # size monotonic in resolution so each tier is a real reduction from a big 4K file
        return {480: 1.0, 720: 2.0, 1080: 6.0, 2160: 19.0}[_profile_max_res(p)]
    precomp = [_profile_max_res(p) for p in _RANKED]
    for cur_res in (2160, 1080, 720, 480, "junk"):
        a_t, a_c = step_targets(_RANKED, cur_res, 30.0, _est, 720)
        b_t, b_c = step_targets(_RANKED, cur_res, 30.0, _est, 720, profile_res=precomp)
        assert [p["id"] for p in a_t] == [p["id"] for p in b_t] and a_c == b_c


# ── movies ────────────────────────────────────────────────────────────────────
def _row(movie_id, resolution, size_gib, runtime=100.0, keep_policy=None, universe_credit=None):
    return dict(
        movie_id=movie_id, resolution=resolution, quality_profile_name=f"p{resolution}",
        size_bytes=int(size_gib * _GIB), runtime_minutes=runtime, keep_policy=keep_policy,
        is_watched=False, last_watched_at=None, collection_name=None, title=f"m{movie_id}",
        universe_credit=universe_credit,
    )


def _plan(df, *, need_gb, score_map=None, protect_threshold=6):
    score_map = score_map if score_map is not None else {i: 0 for i in df.index}
    return plan_movie_downgrades(
        df, score_map, _RANKED, need_gb=need_gb, recent_cutoff=_CUTOFF,
        active_colls=set(), protect_threshold=protect_threshold, floor_resolution=720,
    )


def test_spread_one_tier_each_not_one_to_floor():
    # Two 4K titles; a small need is covered by ONE tier each (4K->1080). Neither is
    # crushed to the 720 floor — the downgrade is spread.
    df = pd.DataFrame([_row(1, 2160, 20.0), _row(2, 2160, 20.0)])
    cands, stats = _plan(df, need_gb=20.0, score_map={0: 0, 1: 5})
    assert {c["movie_id"] for c in cands} == {1, 2}
    assert all(c["target_id"] == 12 for c in cands)   # 1080, one tier down — NOT 720
    assert stats["target_met"]


def test_lowest_score_steps_first():
    df = pd.DataFrame([_row(1, 2160, 20.0), _row(2, 2160, 20.0)])
    cands, _ = _plan(df, need_gb=13.0, score_map={0: 0, 1: 5})
    assert [c["movie_id"] for c in cands] == [1]
    assert cands[0]["target_id"] == 12


def test_deep_pressure_steps_to_floor_only():
    df = pd.DataFrame([_row(1, 2160, 20.0), _row(2, 2160, 20.0)])
    cands, stats = _plan(df, need_gb=999.0, score_map={0: 0, 1: 5})
    assert all(c["target_id"] == 11 for c in cands)   # both floored at 720, not SD(480)
    assert not stats["target_met"]


def test_already_at_floor_skipped():
    df = pd.DataFrame([_row(1, 720, 5.0)])
    cands, stats = _plan(df, need_gb=50.0)
    assert cands == [] and stats["already_at_720p"] == 1


def test_first_tier_would_upgrade_is_skipped():
    # 1080 file but tiny 0.5 GiB: est(720,100)~2.9 > 0.5, stepping down re-grabs bigger -> skip.
    df = pd.DataFrame([_row(1, 1080, 0.5)])
    cands, stats = _plan(df, need_gb=50.0)
    assert cands == [] and stats["already_at_720p"] == 1


_TIER = [
    {"id": 1, "name": "P720",  "items": [{"allowed": True, "quality": {"resolution": 720,  "name": "WEBDL-720p"}}]},
    {"id": 3, "name": "Remux", "items": [{"allowed": True, "quality": {"resolution": 1080, "name": "Remux-1080p"}}]},   # ~235 MiB/min
    {"id": 2, "name": "WEB",   "items": [{"allowed": True, "quality": {"resolution": 1080, "name": "WEBDL-1080p"}}]},   # ~56 MiB/min
]


def test_appropriate_profile_per_tier_is_best_quality_reduction():
    # Big 4K file (50 GiB, 100 min): est(Remux-1080p)~23 GiB < 50, so the 1080 tier lands
    # in the BEST-quality profile that still reduces (Remux-1080p, id 3) sized from runtime
    # — NOT the absolute-lowest encode (WEBDL, id 2).
    df = pd.DataFrame([_row(1, 2160, 50.0)])
    cands, _ = plan_movie_downgrades(
        df, {0: 0}, _TIER, need_gb=5.0, recent_cutoff=_CUTOFF,
        active_colls=set(), protect_threshold=6, floor_resolution=720,
    )
    assert cands[0]["target_id"] == 3


def test_oversized_lower_tier_profile_excluded():
    # Small 4K file (15 GiB, 100 min): est(Remux-1080p)~23 GiB would be BIGGER than the
    # current file, so it's excluded; lands in WEBDL-1080p (~5.5 GiB, id 2) instead.
    df = pd.DataFrame([_row(1, 2160, 15.0)])
    cands, _ = plan_movie_downgrades(
        df, {0: 0}, _TIER, need_gb=2.0, recent_cutoff=_CUTOFF,
        active_colls=set(), protect_threshold=6, floor_resolution=720,
    )
    assert cands[0]["target_id"] == 2


def test_universe_left_to_universe_manager():
    df = pd.DataFrame([
        _row(1, 2160, 20.0, keep_policy="universe"),
        _row(2, 2160, 20.0, keep_policy="keep_universe"),
        _row(3, 2160, 20.0, keep_policy=None),
    ])
    cands, stats = _plan(df, need_gb=5.0, score_map={0: 0, 1: 0, 2: 0})
    assert [c["movie_id"] for c in cands] == [3]
    assert stats["skipped_protected"] == 2


def test_movie_untagged_hot_collection_protected_from_downgrade():
    # An UNTAGGED saga member (keep_policy=None — the common case, membership from its TMDB collection)
    # with borrowed credit >= UNIVERSE_PROTECT_MIN is held at tier by the plan_movie_downgrades guard.
    df = pd.DataFrame([_row(1, 2160, 20.0, keep_policy=None, universe_credit=2.0)])
    cands, stats = _plan(df, need_gb=50.0)
    assert cands == []
    assert stats["skipped_universe"] == 1


def test_movie_stale_collection_credit_is_droppable():
    # The same untagged movie with a DECAYED credit (< UNIVERSE_PROTECT_MIN) is no longer protected —
    # the recency bias makes a stale saga's members droppable again.
    df = pd.DataFrame([_row(1, 2160, 20.0, keep_policy=None, universe_credit=0.4)])
    cands, stats = _plan(df, need_gb=13.0)
    assert [c["movie_id"] for c in cands] == [1]
    assert stats["skipped_universe"] == 0


# NOTE: KEEP-TAGGED universe movies never reach this guard (skipped at the keep_policy guard) — their
# credit-gated step-down floor is the universe manager's (space.universe_quality.downgrade_target,
# tested in test_universe_quality.py).


# ── series (TV) ─────────────────────────────────────────────────────────────────
def _ep(series_id, resolution, size_gib, score, runtime_sec=1800, keep_policy=None,
        last_watched=None, air_date=None, universe_credit=None):
    return dict(
        series_id=series_id, watchability_score=score, keep_policy=keep_policy,
        size_bytes=int(size_gib * _GIB), series_title=f"s{series_id}", resolution=resolution,
        last_watched_at=last_watched, runtime_seconds=runtime_sec, air_date_utc=air_date,
        universe_credit=universe_credit,
    )


def _plan_series(df, *, need_gb, ceiling=20):
    return plan_series_downgrades(
        df, _RANKED, need_gb=need_gb, ceiling=ceiling, watch_cutoff=_CUTOFF, air_cutoff=_CUTOFF,
        keep_tags=frozenset({"keep_series", "keep_season"}), default_runtime_min=45.0,
        floor_resolution=720,
    )


def test_series_spread_one_tier_each():
    # Two 4K series (2 eps x 10 GiB each = 20 GiB); small need -> each steps ONE tier (->1080).
    df = pd.DataFrame([
        _ep(1, 2160, 10.0, 0), _ep(1, 2160, 10.0, 0),
        _ep(2, 2160, 10.0, 5), _ep(2, 2160, 10.0, 5),
    ])
    cands, stats = _plan_series(df, need_gb=20.0)
    assert {c["sid"] for c in cands} == {1, 2}
    assert all(c["target_id"] == 12 for c in cands)   # 1080, not 720
    assert stats["target_met"]


def test_series_lowest_score_first():
    df = pd.DataFrame([
        _ep(1, 2160, 10.0, 0), _ep(1, 2160, 10.0, 0),
        _ep(2, 2160, 10.0, 5), _ep(2, 2160, 10.0, 5),
    ])
    cands, _ = _plan_series(df, need_gb=15.0)
    assert [c["sid"] for c in cands] == [1]
    assert cands[0]["target_id"] == 12


def test_series_deep_pressure_floors_at_720():
    df = pd.DataFrame([_ep(1, 2160, 10.0, 0), _ep(1, 2160, 10.0, 0)])
    cands, stats = _plan_series(df, need_gb=999.0)
    assert [c["target_id"] for c in cands] == [11]    # floored at 720
    assert not stats["target_met"]


def test_series_guards():
    df = pd.DataFrame([
        _ep(1, 2160, 10.0, 0, keep_policy="keep_series"),            # keep -> protected
        _ep(2, 2160, 10.0, 99),                                      # high score
        _ep(3, 720, 10.0, 0),                                        # already at floor
        _ep(4, 2160, 10.0, 0, last_watched="2026-06-01T00:00:00Z"),  # recently watched
    ])
    cands, stats = _plan_series(df, need_gb=50.0)
    assert cands == []
    assert stats["skipped_protected"] == 1
    assert stats["skipped_high_score"] == 1
    assert stats["skipped_already"] == 1
    assert stats["skipped_recent"] == 1


def test_series_hot_universe_credit_protected_from_downgrade():
    # A low-score 4K series that WOULD be a downgrade candidate is held at tier because it
    # carries borrowed franchise/universe credit >= UNIVERSE_PROTECT_MIN (a hot saga sibling).
    df = pd.DataFrame([
        _ep(1, 2160, 10.0, 0, universe_credit=2.0),   # hot saga -> protected
        _ep(2, 2160, 10.0, 0, universe_credit=2.0),
    ])
    cands, stats = _plan_series(df, need_gb=50.0)
    assert cands == []
    assert stats["skipped_universe"] == 2


def test_series_stale_universe_credit_is_droppable():
    # The same series with a DECAYED credit (< UNIVERSE_PROTECT_MIN) is no longer protected —
    # the recency bias makes a stale saga's members droppable again.
    df = pd.DataFrame([
        _ep(1, 2160, 10.0, 0, universe_credit=0.4),   # decayed -> not protected
        _ep(1, 2160, 10.0, 0, universe_credit=0.4),
    ])
    cands, stats = _plan_series(df, need_gb=15.0)
    assert [c["sid"] for c in cands] == [1]
    assert stats["skipped_universe"] == 0


def _plan_series_tier(df, *, need_gb):
    # Same as _plan_series but with the multi-1080p _TIER ladder (Remux vs WEBDL).
    return plan_series_downgrades(
        df, _TIER, need_gb=need_gb, ceiling=20, watch_cutoff=_CUTOFF, air_cutoff=_CUTOFF,
        keep_tags=frozenset(), default_runtime_min=45.0, floor_resolution=720,
    )


def test_series_appropriate_profile_per_tier_is_best_quality_reduction():
    # Big 4K series (4 eps x 10 GiB = 40 GiB, 30-min eps): est(whole-series Remux-1080p)
    # ~27.5 GiB < 40, so the 1080 tier lands in the BEST-quality profile that still reduces
    # (Remux-1080p, id 3), sized from runtime x episode count — NOT the cheapest (WEBDL).
    df = pd.DataFrame([_ep(1, 2160, 10.0, 0) for _ in range(4)])
    cands, _ = _plan_series_tier(df, need_gb=5.0)
    assert cands[0]["target_id"] == 3


def test_series_oversized_lower_tier_profile_excluded():
    # Small 4K series (4 eps x 5 GiB = 20 GiB): whole-series Remux-1080p (~27.5 GiB) would
    # be BIGGER than the current series -> excluded; lands in WEBDL-1080p (id 2).
    df = pd.DataFrame([_ep(1, 2160, 5.0, 0) for _ in range(4)])
    cands, _ = _plan_series_tier(df, need_gb=2.0)
    assert cands[0]["target_id"] == 2


# ══ EXHAUSTIVE mode (space_exhaustive_downgrade, DEFAULT ON at the service) ══════
# "Deletion is the TRUE last resort": downgrade EVERYTHING that can still be downgraded
# before ANYTHING is deleted. The planner therefore (a) drops the watchability CEILING as
# an eligibility filter and (b) spreads to FULL DEPTH instead of stopping at need_gb — the
# APPLYING pass is what stops at the band top U. Ordering and every other guard are unchanged.
def _plan_x(df, *, need_gb, score_map=None, protect_threshold=6):
    score_map = score_map if score_map is not None else {i: 0 for i in df.index}
    return plan_movie_downgrades(
        df, score_map, _RANKED, need_gb=need_gb, recent_cutoff=_CUTOFF,
        active_colls=set(), protect_threshold=protect_threshold, floor_resolution=720,
        exhaustive=True,
    )


def test_exhaustive_ignores_the_score_ceiling_but_keeps_ordering():
    # A 4K title scoring 99 (far over the protect threshold) is normally excluded outright.
    # Exhaustive ADMITS it — it still has quality to shed before any title is deleted — and it
    # sorts LAST, behind the score-0 title (ascending watchability is unchanged).
    df = pd.DataFrame([_row(1, 2160, 20.0), _row(2, 2160, 20.0)])
    cands, stats = _plan_x(df, need_gb=5.0, score_map={0: 99, 1: 0})
    assert [c["movie_id"] for c in cands] == [2, 1]          # lowest score first
    assert stats["skipped_high_score"] == 0
    assert stats["over_ceiling_included"] == 1 and stats["exhaustive"] is True
    # …and the legacy path still excludes it:
    legacy, lstats = _plan(df, need_gb=5.0, score_map={0: 99, 1: 0})
    assert [c["movie_id"] for c in legacy] == [2] and lstats["skipped_high_score"] == 1


def test_exhaustive_preserves_keep_universe_and_recent_guards():
    # Only the CEILING is relaxed. Every other guard is untouched, so a keep-tagged title, a
    # keep_universe title, a hot-saga (credit) title and a recently-watched title all still skip.
    df = pd.DataFrame([
        _row(1, 2160, 20.0, keep_policy="keep_movie"),
        _row(2, 2160, 20.0, keep_policy="keep_universe"),
        _row(3, 2160, 20.0, universe_credit=2.0),
        dict(_row(4, 2160, 20.0), is_watched=True, last_watched_at="2026-06-01T00:00:00Z"),
        _row(5, 720, 20.0),                                   # already at the floor
        _row(6, 2160, 20.0),                                  # the only real candidate
    ])
    cands, stats = _plan_x(df, need_gb=999.0, score_map={i: 99 for i in range(6)})
    assert [c["movie_id"] for c in cands] == [6]
    assert stats["skipped_protected"] == 2 and stats["skipped_universe"] == 1
    assert stats["skipped_recent"] == 1 and stats["already_at_720p"] == 1


def test_exhaustive_plans_every_title_down_to_the_floor():
    # Legacy: a small need stops the spread after ONE tier each (4K -> 1080).
    # Exhaustive: no early stop — every eligible title is planned all the way to 720.
    df = pd.DataFrame([_row(i, 2160, 20.0) for i in (1, 2, 3)])
    legacy, _ = _plan(df, need_gb=20.0, score_map={0: 0, 1: 1, 2: 2})
    assert all(c["target_id"] == 12 for c in legacy)           # 1080 only
    cands, stats = _plan_x(df, need_gb=20.0, score_map={0: 0, 1: 1, 2: 2})
    assert [c["movie_id"] for c in cands] == [1, 2, 3]
    assert all(c["target_id"] == 11 for c in cands)            # 720 floor — nothing left to shrink
    assert stats["target_met"]


def test_exhaustive_never_steps_below_the_720_floor():
    # The 480 rung (id 10) is in the ladder but is BELOW the floor — full-depth exhaustion
    # still stops at 720. (The sub-720 escape hatch lives in the release picker, and only
    # fires when no >=720 release exists at all.)
    df = pd.DataFrame([_row(1, 2160, 40.0)])
    cands, _ = _plan_x(df, need_gb=10_000.0)
    assert cands[0]["target_id"] == 11


def test_series_exhaustive_ignores_ceiling_and_plans_to_the_floor():
    df = pd.DataFrame([
        _ep(1, 2160, 10.0, 99), _ep(1, 2160, 10.0, 99),        # over the ceiling
        _ep(2, 2160, 10.0, 0), _ep(2, 2160, 10.0, 0),
    ])
    cands, stats = plan_series_downgrades(
        df, _RANKED, need_gb=5.0, ceiling=20, watch_cutoff=_CUTOFF, air_cutoff=_CUTOFF,
        keep_tags=frozenset({"keep_series", "keep_season"}), default_runtime_min=45.0,
        floor_resolution=720, exhaustive=True,
    )
    assert [c["sid"] for c in cands] == [2, 1]                 # ascending score, unchanged
    assert all(c["target_id"] == 11 for c in cands)            # both planned to the floor
    assert stats["skipped_high_score"] == 0 and stats["over_ceiling_included"] == 1


def test_series_exhaustive_preserves_keep_recent_and_universe_guards():
    df = pd.DataFrame([
        _ep(1, 2160, 10.0, 99, keep_policy="keep_series"),
        _ep(2, 2160, 10.0, 99, universe_credit=2.0),
        _ep(3, 2160, 10.0, 99, last_watched="2026-06-01T00:00:00Z"),
        _ep(4, 2160, 10.0, 99, air_date="2026-06-01T00:00:00Z"),
        _ep(5, 720, 10.0, 99),
    ])
    cands, stats = plan_series_downgrades(
        df, _RANKED, need_gb=999.0, ceiling=20, watch_cutoff=_CUTOFF, air_cutoff=_CUTOFF,
        keep_tags=frozenset({"keep_series", "keep_season"}), default_runtime_min=45.0,
        floor_resolution=720, exhaustive=True,
    )
    assert cands == []
    assert stats["skipped_protected"] == 1 and stats["skipped_universe"] == 1
    assert stats["skipped_recent"] == 2 and stats["skipped_already"] == 1


# ── exhaustive=False is byte-identical to today's planner on a synthetic library ──
def _synthetic_library():
    """A 12-title mixed library: 4K/1080/720 at a spread of scores, plus every guard
    class (keep tag, keep_universe, hot-saga credit, recently watched, at-floor, and a
    would-upgrade tiny file). Exercises ordering, spread depth and every skip counter."""
    rows = [
        _row(1, 2160, 40.0),
        _row(2, 2160, 22.0),
        _row(3, 1080, 9.0),
        _row(4, 1080, 7.5),
        _row(5, 720, 3.0),                                     # at floor
        _row(6, 2160, 30.0, keep_policy="keep_movie"),
        _row(7, 2160, 30.0, keep_policy="keep_universe"),
        _row(8, 2160, 30.0, universe_credit=2.0),
        dict(_row(9, 2160, 30.0), is_watched=True, last_watched_at="2026-06-15T00:00:00Z"),
        _row(10, 1080, 0.4),                                   # step-down would re-grab BIGGER
        _row(11, 2160, 18.0, runtime=140.0),
        _row(12, 1080, 12.0, runtime=140.0),
    ]
    scores = {i: s for i, s in enumerate([2, 8, 1, 30, 0, 0, 0, 0, 0, 0, 15, 4])}
    return pd.DataFrame(rows), scores


def test_exhaustive_off_is_byte_identical_to_todays_planner():
    # FROZEN oracle: the exact candidate list + stats today's planner produces for the
    # synthetic library. space_exhaustive_downgrade=false must reproduce it verbatim.
    df, scores = _synthetic_library()
    cands, stats = plan_movie_downgrades(
        df, scores, _RANKED, need_gb=45.0, recent_cutoff=_CUTOFF, active_colls=set(),
        protect_threshold=20, floor_resolution=720, exhaustive=False,
    )
    assert [(c["movie_id"], c["target_id"], c["reclaim_gb"], c["score"]) for c in cands] == [
        (3, 11, 6.07, 1),
        (1, 12, 33.164, 2),
        (12, 11, 7.898, 4),
    ]
    assert stats == {
        "candidates_found": 3, "already_at_720p": 2, "skipped_protected": 2,
        "skipped_high_score": 1, "skipped_recent": 1, "skipped_universe": 1,
        "est_reclaim_gb": 47.13, "target_met": True,
        # the two exhaustive-mode counters are the ONLY additions, and are inert here
        "exhaustive": False, "over_ceiling_included": 0,
    }


def test_exhaustive_on_extends_that_same_library_to_the_floor():
    # Same synthetic library, exhaustive: the score-30 title (over the ceiling) joins, the
    # spread runs past need_gb, and EVERY candidate lands on the 720 floor. Ordering is still
    # ascending watchability, and the three guard classes still hold.
    df, scores = _synthetic_library()
    cands, stats = plan_movie_downgrades(
        df, scores, _RANKED, need_gb=45.0, recent_cutoff=_CUTOFF, active_colls=set(),
        protect_threshold=20, floor_resolution=720, exhaustive=True,
    )
    assert [(c["movie_id"], c["target_id"], c["score"]) for c in cands] == [
        (3, 11, 1), (1, 11, 2), (12, 11, 4), (2, 11, 8), (11, 11, 15), (4, 11, 30),
    ]
    assert stats["over_ceiling_included"] == 1 and stats["skipped_high_score"] == 0
    # the guards are untouched: same keep/universe/recent/at-floor counts as the legacy run
    assert (stats["skipped_protected"], stats["skipped_universe"],
            stats["skipped_recent"], stats["already_at_720p"]) == (2, 1, 1, 2)


def test_exhaustive_off_matches_the_pre_change_spread_oracle():
    # Independent check of the same claim: re-implement the ORIGINAL _spread_to_target
    # verbatim and confirm the shipped one (exhaustive defaulted off) still agrees.
    def _legacy_spread(eligible, need_gb):
        for e in eligible:
            e["_depth"] = 0
        total = 0.0
        progressed = True
        while total < need_gb and progressed:
            progressed = False
            for e in eligible:
                d = e["_depth"]
                cum = e["cum_reclaim"]
                if d >= len(cum):
                    continue
                prev = cum[d - 1] if d > 0 else 0.0
                total += cum[d] - prev
                e["_depth"] = d + 1
                progressed = True
                if total >= need_gb:
                    break
        return total

    from scripts.managers.machine_learning.space.downgrade_planner import _spread_to_target
    for need in (0.0, 3.0, 12.5, 40.0, 999.0):
        items = [{"cum_reclaim": [2.0, 5.0, 6.0]}, {"cum_reclaim": [1.0]},
                 {"cum_reclaim": []}, {"cum_reclaim": [4.0, 9.0]}]
        legacy_items = [dict(i) for i in items]
        assert _spread_to_target(items, need) == _legacy_spread(legacy_items, need)
        assert [i["_depth"] for i in items] == [i["_depth"] for i in legacy_items]
