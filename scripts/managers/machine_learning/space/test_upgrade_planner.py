"""Tests for space.upgrade_planner.plan_movie_upgrades (ML Step 7c, movie twin).

Guards the extraction of the radarr run_active_watcher_upgrades decision core. The
likelihood/ladder/sizing brain funcs are mocked to deterministic behaviour so the
planner's GUARDS, target step-down, reclaim formula, reason string, and stats are
asserted against a hand-computed oracle (independent of the real ladder config).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import scripts.managers.machine_learning.space.upgrade_planner as up

NOW = datetime(2026, 6, 8, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(days=30)
RECENT = (NOW - timedelta(days=5)).isoformat()
OLD = (NOW - timedelta(days=120)).isoformat()

# Deterministic ladder: profile id -> rank (ascending quality).
LADDER = [("sd", 1), ("720p", 2), ("1080p", 3), ("4k", 4)]
PROFILES = [
    {"id": 1, "name": "SD", "est_gb": 1.0},
    {"id": 2, "name": "720p", "est_gb": 2.0},
    {"id": 3, "name": "1080p", "est_gb": 5.0},
    {"id": 4, "name": "4K", "est_gb": 20.0},
]


@pytest.fixture
def mocked(monkeypatch):
    monkeypatch.setattr(up, "radarr_ladder", lambda config=None: LADDER)
    monkeypatch.setattr(up, "ladder_rank",
                        lambda pid, config=None: next((i for i, (_n, p) in enumerate(LADDER) if p == pid), -1))
    monkeypatch.setattr(up, "watch_likelihood",
                        lambda row, config=None: float(row.get("likelihood", 0) or 0))
    monkeypatch.setattr(up, "profile_id_for_likelihood",
                        lambda L, config=None: 4 if L >= 70 else 3 if L >= 40 else 2)
    monkeypatch.setattr(up, "estimate_gb_for_profile",
                        lambda prof, rt, n: float(prof.get("est_gb", 0.0)) * n)


def _df(rows):
    df = pd.DataFrame(rows)
    if "is_watched" in df.columns:
        df["is_watched"] = df["is_watched"].fillna(False).astype(bool)
    return df


def test_plan_movie_upgrades_oracle(mocked):
    df = _df([
        dict(movie_id=np.nan, likelihood=80, is_watched=True, last_watched_at=RECENT,
             quality_profile_id=2),                                                    # 0 no id -> skip
        dict(movie_id=11, keep_policy="keep_universe", likelihood=80, is_watched=True,
             last_watched_at=RECENT, quality_profile_id=2),                            # 1 keep -> skip
        dict(movie_id=12, certification="PG", likelihood=80, is_watched=True,
             last_watched_at=RECENT, quality_profile_id=2),                            # 2 kids
        dict(movie_id=13, likelihood=80, is_watched=False, quality_profile_id=2),      # 3 not active
        dict(movie_id=14, likelihood=80, is_watched=True, last_watched_at=OLD,
             quality_profile_id=2),                                                    # 4 stale watch
        dict(movie_id=15, likelihood=80, is_watched=True, last_watched_at=RECENT,
             quality_profile_id=2, runtime_minutes=120.0, size_bytes=3 * 1024**3),     # 5 UPGRADE -> 4K
        dict(movie_id=16, likelihood=80, is_watched=True, last_watched_at=RECENT,
             quality_profile_id=4),                                                    # 6 already best
        dict(movie_id=17, likelihood=10, is_watched=True, last_watched_at=RECENT,
             quality_profile_id=1, runtime_minutes=90.0, size_bytes=int(0.5 * 1024**3)),  # 7 UPGRADE -> 720p
    ])
    cands, stats = up.plan_movie_upgrades(df, PROFILES, active_cutoff=CUTOFF, config={})

    assert stats == {"checked": 8, "already_best": 1, "skipped_kids": 1, "skipped_not_active": 2}, stats
    assert [c["idx"] for c in cands] == [5, 7]

    c5 = cands[0]
    assert (c5["movie_id"], c5["target_id"], c5["target_name"]) == (15, 4, "4K")
    assert c5["likelihood"] == 80.0
    assert c5["reclaim_gb"] == pytest.approx(-(20.0 - 3.0))      # -(est_target - cur)
    assert c5["reason"] == "actively watched (L=80%) → 4K"

    c7 = cands[1]
    assert (c7["movie_id"], c7["target_id"], c7["target_name"]) == (17, 2, "720p")
    assert c7["reclaim_gb"] == pytest.approx(-(2.0 - 0.5))
    assert c7["reason"] == "actively watched (L=10%) → 720p"


def test_no_present_profile_in_ladder_range_skips(mocked):
    """target earns rank 3 (4K) but no profile at/below it down to current exists
    -> no candidate (the step-down finds nothing)."""
    profiles_missing_top = [p for p in PROFILES if p["id"] != 4]   # drop 4K
    df = _df([
        dict(movie_id=20, likelihood=80, is_watched=True, last_watched_at=RECENT,
             quality_profile_id=3, runtime_minutes=120.0, size_bytes=5 * 1024**3),
    ])
    cands, stats = up.plan_movie_upgrades(df, profiles_missing_top, active_cutoff=CUTOFF, config={})
    # target rank 3 (pid 4) absent; range(3, 2, -1) only checks pid 4 -> None -> skip.
    assert cands == []
    assert stats["already_best"] == 0 and stats["checked"] == 1


def test_reclaim_zero_when_runtime_missing(mocked):
    """No runtime -> est_target 0 -> reclaim = -max(0, 0 - cur) = 0.0 (never positive)."""
    df = _df([
        dict(movie_id=30, likelihood=80, is_watched=True, last_watched_at=RECENT,
             quality_profile_id=2, size_bytes=3 * 1024**3),   # no runtime_minutes
    ])
    cands, _ = up.plan_movie_upgrades(df, PROFILES, active_cutoff=CUTOFF, config={})
    assert len(cands) == 1
    assert cands[0]["reclaim_gb"] == 0.0
