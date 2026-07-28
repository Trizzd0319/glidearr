"""space/test_unscored_deferral.py — an unscored row DEFERS; it is never guessed at.
================================================================================
Both space planners used to hand a row with no watchability score the literal ``5`` and
carry on (``score_map.get(idx, 5)``; the service-side pool builders spelled it
``... if pd.notna(sc) else 5``).

That literal was a POINT ON THE SCORE AXIS, so it silently changed meaning every time the
axis moved. When it was written it sat at **p1.3** of the movie distribution — "an
unscored row is the least valuable thing in the library, delete it first". After Group D
v2 turned a near-constant +12 bonus into a 0-to-negative transcode-risk penalty, the same
literal 5 sits at **p32.4** — "delete it mid-pack". Nobody chose either behaviour.

The policy the sentinel was standing in for is already stated, correctly, one level up:
both delete-pool builders refuse to contribute ANY candidate when the whole
``watchability_score`` column is empty ("won't delete on fallback scores"). The row-level
sentinel contradicted that for the partial case. Now they agree — an unscored row is
DEFERRED, the same answer every other missing-data branch in this codebase gives.

What is pinned here:
  * an unscored row never enters the delete queue, and never enters the downgrade queue
    either (they are two stages of one decision and take one answer to missing data);
  * it is COUNTED, so a persistently-unscored row shows up as a number instead of
    becoming quietly immortal;
  * a fully-scored frame is BYTE-IDENTICAL to before (the deferral is reachable only
    through the missing-value branch);
  * NaN counts as missing, not as a score;
  * if something ever does reach the ranker without a score, it sorts LAST, not first.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.managers.machine_learning.space.coordinator_ranker import (
    UNSCORED_RANK,
    select_for_target,
)
from scripts.managers.machine_learning.space.delete_planner import (
    build_movie_delete_candidates,
)
from scripts.managers.machine_learning.space.downgrade_planner import (
    plan_movie_downgrades,
    row_score,
)

_NOW = pd.Timestamp("2026-07-27T00:00:00Z")
_CUTOFF = _NOW - pd.Timedelta(days=30)
_GB = 1024 ** 3


def _movie_df(n=3):
    return pd.DataFrame({
        "movie_id": list(range(1, n + 1)),
        "movie_file_id": [float(i) for i in range(1, n + 1)],
        "size_bytes": [10 * _GB] * n,
        "resolution": [720] * n,
        "keep_policy": [None] * n,
        "is_franchise_entry": [False] * n,
        "marked_for_deletion": [False] * n,
        "date_added": ["2020-01-01T00:00:00Z"] * n,
        "last_watched_at": [None] * n,
        "quality_profile_name": ["HD 720p"] * n,
        "runtime_minutes": [100] * n,
        "is_watched": [False] * n,
        "collection_name": [None] * n,
    })


def _marked(df):
    return pd.Series(False, index=df.index)


def _delete(df, score_map, *, ceiling=17, stats=None):
    return build_movie_delete_candidates(
        df, score_map, _marked(df), franchise_file_ids=frozenset(),
        no_delete_cutoff=_CUTOFF, include_unwatched=True, ceiling=ceiling,
        universe_age_days=None, now=_NOW, stats=stats, floor_resolution=None,
    )


# ── row_score: what counts as "no score" ──────────────────────────────────────

def test_row_score_treats_absent_none_and_nan_alike():
    assert row_score({}, 0) is None                 # key absent
    assert row_score({0: None}, 0) is None          # explicit None
    assert row_score({0: np.nan}, 0) is None        # NaN from a Parquet column
    assert row_score({0: pd.NA}, 0) is None
    assert row_score({0: 0}, 0) == 0                # a real ZERO is a score, not a gap
    assert row_score({0: 12}, 0) == 12


def test_a_zero_score_is_still_a_score():
    """The most dangerous confusion available here: 0 is the BOTTOM of the axis and a
    perfectly ordinary value (unwatched titles land there). It must never read as
    'missing' — that would make the least-watchable rows undeletable."""
    df = _movie_df(1)
    assert len(_delete(df, {df.index[0]: 0})) == 1


# ── delete pool ───────────────────────────────────────────────────────────────

def test_unscored_rows_never_enter_the_delete_pool():
    df = _movie_df(3)
    stats = {}
    cands = _delete(df, {0: 1, 2: 3}, stats=stats)          # index 1 has no score
    assert {c[4] for c in cands} == {0, 2}
    assert stats["skipped_unscored"] == 1


def test_the_old_sentinel_would_have_deleted_it():
    """Regression witness: with the old ``get(idx, 5)`` the unscored row scored 5, which
    is below the 17 ceiling, so it WAS delete-eligible — and under the pre-Group-D-v2 axis
    it sorted to the very front of the queue."""
    df = _movie_df(3)
    old_style = {0: 1, 1: 5, 2: 3}                            # what the sentinel produced
    assert len(_delete(df, old_style)) == 3                   # it used to be admitted
    assert len(_delete(df, {0: 1, 2: 3})) == 2                # now it defers


def test_nan_scores_defer_too():
    df = _movie_df(2)
    stats = {}
    cands = _delete(df, {0: 4, 1: float("nan")}, stats=stats)
    assert [c[4] for c in cands] == [0]
    assert stats["skipped_unscored"] == 1


def test_a_fully_scored_frame_is_unchanged():
    """The deferral is reachable ONLY through the missing-value branch: a frame with a
    score for every row produces exactly the queue it always did."""
    df = _movie_df(4)
    full = {i: 3 * i for i in df.index}
    stats = {}
    cands = _delete(df, full, ceiling=100, stats=stats)
    assert [c[4] for c in cands] == [0, 1, 2, 3]
    assert [c[1] for c in cands] == [0, 3, 6, 9]
    assert "skipped_unscored" not in stats


def test_the_deferral_counter_is_only_bumped_when_it_fires():
    df = _movie_df(2)
    stats = {}
    _delete(df, {0: 1, 1: 2}, stats=stats)
    assert stats.get("skipped_unscored", 0) == 0


# ── downgrade pool ────────────────────────────────────────────────────────────

_RANKED = [
    {"name": "HD 720p", "items": [{"allowed": True, "quality": {"resolution": 720}}]},
    {"name": "WEBDL 1080p", "items": [{"allowed": True, "quality": {"resolution": 1080}}]},
    {"name": "Remux 2160p", "items": [{"allowed": True, "quality": {"resolution": 2160}}]},
]


def _downgrade_df():
    df = _movie_df(2)
    df["resolution"] = [2160, 2160]
    df["size_bytes"] = [80 * _GB, 80 * _GB]
    df["quality_profile_name"] = ["Remux 2160p", "Remux 2160p"]
    return df


def test_unscored_rows_never_enter_the_downgrade_pool_either():
    """A step-down and a deletion are two stages of ONE decision (deletion is only
    reachable once a title is at the 720p floor), so they must take the same answer to
    missing data. Shrinking a file on an invented score is the same error as deleting on
    one — and a deferred row is inert: not downgraded, therefore never at the floor,
    therefore not deletable. It simply waits for a real score."""
    df = _downgrade_df()
    cands, stats = plan_movie_downgrades(
        df, {0: 0}, _RANKED, need_gb=500.0, recent_cutoff=_CUTOFF,
        active_colls=set(), protect_threshold=6, floor_resolution=720,
    )
    assert [c["movie_id"] for c in cands] == [1]
    assert stats["skipped_unscored"] == 1


def test_downgrade_pool_unchanged_when_every_row_is_scored():
    df = _downgrade_df()
    cands, stats = plan_movie_downgrades(
        df, {0: 0, 1: 1}, _RANKED, need_gb=500.0, recent_cutoff=_CUTOFF,
        active_colls=set(), protect_threshold=6, floor_resolution=720,
    )
    assert [c["movie_id"] for c in cands] == [1, 2]
    assert stats.get("skipped_unscored", 0) == 0


# ── ranker backstop ───────────────────────────────────────────────────────────

def test_an_unscored_candidate_sorts_last_not_first():
    """The builders defer unscored rows, so nothing scoreless should reach the ranker.
    If one ever does, it must not JUMP the queue the way the old literal-5 default made
    it do — 5 sat near the bottom of the axis, i.e. "delete this first"."""
    pool = [
        {"service": "movie", "score": 40, "critic": None, "size_gb": 5.0, "fid": 1},
        {"service": "movie", "critic": None, "size_gb": 5.0, "fid": 2},          # no score
        {"service": "movie", "score": 2, "critic": None, "size_gb": 5.0, "fid": 3},
    ]
    picked, _ = select_for_target(pool, need_gb=15.0)
    assert [c["fid"] for c in picked] == [3, 1, 2]


def test_the_unscored_rank_is_above_the_whole_axis():
    assert UNSCORED_RANK > 100


def test_unscored_backstop_survives_the_tier_size_bucketing():
    """``tier_size`` takes ``math.floor(score / tier_size)`` — a float('inf') backstop
    would raise OverflowError here, which is why the constant is finite."""
    pool = [
        {"service": "movie", "score": 2, "critic": None, "size_gb": 5.0, "fid": 1},
        {"service": "movie", "critic": None, "size_gb": 9.0, "fid": 2},
    ]
    picked, _ = select_for_target(pool, need_gb=99.0, tier_size=10.0)
    assert [c["fid"] for c in picked] == [1, 2]


def test_unscored_backstop_survives_utility_per_gb_ranking():
    pool = [
        {"service": "movie", "score": 2, "critic": None, "size_gb": 5.0, "fid": 1},
        {"service": "movie", "critic": None, "size_gb": 5.0, "fid": 2},
    ]
    picked, _ = select_for_target(pool, need_gb=99.0, ranking_mode="utility_per_gb")
    assert [c["fid"] for c in picked] == [1, 2]
