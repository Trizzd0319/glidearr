"""Regression test for the space-pressure ↔ universe ledger-stamp collision.

Bug (found via the 9000 GB fake-floor dry-run): under pressure, space-pressure
downgrades a bare-"universe" "deletable last-resort" title and stamps the ledger
'downgrade'; the universe pass then ran a BLANKET clear of every universe-row
upgrade/downgrade stamp, erasing that fresh stamp, and — because the title was
already at its earned tier — never re-stamped it. Net: the dry-run ledger (the
ML-migration parity oracle) under-counted downgrades by one (15 instead of 16).

Fix (two edits, mirrored here as a per-run lifecycle simulation):
  1. RadarrCacheMovieFilesManager.run resets BOTH 'delete' AND 'downgrade' stamps
     each run (space-pressure can't reap its own when it short-circuits healthy).
  2. RadarrQualityUniverseManager.apply_quality_actions reaps ONLY universe-authored
     stamps (plan_reason "universe <action>"), never space-pressure's last-resort
     downgrade.

The test drives the REAL ledger brain functions (stamp / stamp_universe_plan) so
the reason strings it keys on are the production ones, then applies the exact two
mask expressions from the edited code sites and asserts the invariants.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.ledger.decision_ledger import (
    stamp,
    stamp_universe_plan,
)

# Reasons the producers actually write (mirrors downgrade_planner / decision_ledger).
SP_LAST_RESORT = "universe-tagged (score=13, deletable last-resort)"  # space-pressure
TARGET_720 = {"id": 7, "name": "HD-720p", "items": [{"quality": {"resolution": 720}, "allowed": True}]}


def _movie_files_reset(df):
    """Mirror of RadarrCacheMovieFilesManager.run's per-run reset (movie_files.py)."""
    _prior_sp = df["planned_action"].isin(["delete", "downgrade"])
    df.loc[_prior_sp, "planned_action"] = None
    df.loc[_prior_sp, "plan_reason"] = None
    df.loc[_prior_sp, "plan_reclaim_gb"] = None


def _universe_reap(df):
    """Mirror of apply_quality_actions' scoped stale-stamp reap (universe.py)."""
    universe_mask = df["keep_policy"] == "universe"
    _uni_plan = universe_mask & df["plan_reason"].isin(["universe upgrade", "universe downgrade"])
    df.loc[_uni_plan, "planned_action"] = None
    df.loc[_uni_plan, "plan_reason"] = None
    df.loc[_uni_plan, "plan_reclaim_gb"] = None


def _frame():
    df = pd.DataFrame([
        # 0: bare-universe last-resort title (the Black Panther case)
        dict(keep_policy="universe", title="Black Panther", runtime_minutes=161.0,
             size_bytes=40 * 1024**3),
        # 1: bare-universe with a STALE universe upgrade stamp from a prior run
        dict(keep_policy="universe", title="Iron Man", runtime_minutes=126.0,
             size_bytes=20 * 1024**3),
        # 2: non-universe with a STALE space-pressure downgrade from a prior run
        dict(keep_policy=None, title="Old Downgrade", runtime_minutes=100.0,
             size_bytes=10 * 1024**3),
        # 3: non-universe with a STALE delete stamp from a prior run
        dict(keep_policy=None, title="Old Delete", runtime_minutes=100.0,
             size_bytes=5 * 1024**3),
    ])
    for c in ("planned_action", "plan_reason", "plan_reclaim_gb"):
        df[c] = None
    df[["planned_action", "plan_reason"]] = df[["planned_action", "plan_reason"]].astype(object)
    return df


def test_space_pressure_downgrade_survives_universe_reap():
    df = _frame()
    # ── prior run leaves stale stamps on rows 1/2/3 ──
    stamp_universe_plan(df, 1, "upgrade", TARGET_720)        # stale universe upgrade
    stamp(df, 2, "downgrade", SP_LAST_RESORT, 5.0)           # stale space-pressure downgrade
    stamp(df, 3, "delete", "watched, grace expired", 5.0)    # stale delete

    # ── 1. movie_files.run reset (delete + downgrade) ──
    _movie_files_reset(df)
    assert df.at[2, "planned_action"] is None, "stale SP downgrade must be reaped upstream"
    assert df.at[3, "planned_action"] is None, "stale delete must be reaped upstream"
    assert df.at[1, "planned_action"] == "upgrade", "universe upgrade is NOT space-pressure-owned"

    # ── 2. space-pressure stamps THIS run's last-resort downgrade on Black Panther ──
    stamp(df, 0, "downgrade", SP_LAST_RESORT, 30.0)

    # ── 3. universe pass reaps its OWN stale stamps, then (at earned tier) does NOT re-stamp row 0 ──
    _universe_reap(df)

    # INVARIANT: the fresh space-pressure downgrade survives into the ledger…
    assert df.at[0, "planned_action"] == "downgrade", "Black Panther's fresh downgrade was clobbered"
    assert df.at[0, "plan_reason"] == SP_LAST_RESORT
    assert df.at[0, "plan_reclaim_gb"] == 30.0
    # …while the stale universe-authored upgrade is correctly reaped.
    assert df.at[1, "planned_action"] is None, "stale universe upgrade should be reaped"


def test_universe_reap_clears_its_own_stale_downgrade():
    """A stale UNIVERSE downgrade (reason 'universe downgrade') is reaped, even though
    a same-named space-pressure 'downgrade' action with a different reason is not."""
    df = _frame()
    stamp_universe_plan(df, 0, "downgrade", TARGET_720)   # universe-authored downgrade
    assert df.at[0, "plan_reason"] == "universe downgrade"
    _universe_reap(df)
    assert df.at[0, "planned_action"] is None
