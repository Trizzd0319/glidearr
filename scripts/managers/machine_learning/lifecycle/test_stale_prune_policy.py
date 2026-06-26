"""Tests for lifecycle.stale_prune_policy — the pure decision slices of the Radarr
owned-movie stale prune (ML Step 8). The service keeps the scoring, the global_cache
clock, and the unmonitor/delete APPLY; these cover the extracted cores: the
pressure→dwell expedite curve, the clock-age parse, and the per-movie action.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.managers.machine_learning.lifecycle.stale_prune_policy import (
    budget_delete_cohort,
    clock_age,
    expedite_dwell,
    franchise_delete_exempt,
    prune_below_floor_action,
    prune_score_gate,
    restore_cooldown_active,
)

_NOW = datetime(2026, 6, 10, tzinfo=timezone.utc)


# ── expedite_dwell ──────────────────────────────────────────────────────────────
def test_expedite_dwell_legacy_when_no_floor_or_unknown_free():
    # T<=0 (no free_space_limit) -> always active, full dwell
    assert expedite_dwell(500.0, 0.0, 0.0, 90, 7) == (90, True)
    # unknown free space (inf) -> always active, full dwell
    assert expedite_dwell(float("inf"), 100.0, 110.0, 90, 7) == (90, True)


def test_expedite_dwell_shortens_under_pressure():
    T, U = 100.0, 110.0
    assert expedite_dwell(200.0, T, U, 90, 7) == (90, False)   # free >= U -> full dwell, not pressured
    assert expedite_dwell(100.0, T, U, 90, 7) == (7, True)     # free <= T -> min dwell, pressured
    # halfway in the band -> halfway between min and full (p=0.5)
    eff, pressure = expedite_dwell(105.0, T, U, 90, 7)
    assert pressure is True and eff == round(7 + 0.5 * (90 - 7))


def test_expedite_dwell_no_expedite_when_min_ge_delete():
    # min_delete_days >= delete_days -> eff stays at delete_days (no curve)
    assert expedite_dwell(100.0, 100.0, 110.0, 30, 30) == (30, True)


# ── clock_age ───────────────────────────────────────────────────────────────────
def test_clock_age_parses_and_resets_on_garbage():
    iso, age = clock_age("2026-05-11T00:00:00+00:00", _NOW)
    assert iso == "2026-05-11T00:00:00+00:00" and age == 30
    # naive timestamp treated as UTC
    _, age2 = clock_age("2026-06-09T00:00:00", _NOW)
    assert age2 == 1
    # garbage resets to now (age 0, since reflects the reset)
    iso3, age3 = clock_age("nonsense", _NOW)
    assert iso3 == _NOW.isoformat() and age3 == 0


# ── restore_cooldown_active ───────────────────────────────────────────────────────
def test_restore_cooldown_off_is_byte_identical():
    # min_age_days <= 0 / None / unparseable -> never blocks, regardless of timestamp
    just_deleted = _NOW.isoformat()
    for bad in (0, -5, None, "x"):
        assert restore_cooldown_active(just_deleted, _NOW, bad) is False


def test_restore_cooldown_boundary():
    # deleted exactly 7 days ago: cooldown of 7d has elapsed -> NOT active (restorable)
    deleted = datetime(2026, 6, 3, tzinfo=timezone.utc).isoformat()   # _NOW is 2026-06-10
    assert restore_cooldown_active(deleted, _NOW, 7) is False
    # one day short of 7 -> still active (blocked)
    deleted6 = datetime(2026, 6, 4, tzinfo=timezone.utc).isoformat()
    assert restore_cooldown_active(deleted6, _NOW, 7) is True
    # just deleted -> blocked
    assert restore_cooldown_active(_NOW.isoformat(), _NOW, 7) is True


def test_restore_cooldown_naive_and_garbage():
    # naive timestamp treated as UTC (5 days < 7 -> active)
    assert restore_cooldown_active("2026-06-05T00:00:00", _NOW, 7) is True
    # garbage / blank -> fail-open (not active, restore allowed) even with cooldown on
    for bad in ("nonsense", "", None):
        assert restore_cooldown_active(bad, _NOW, 7) is False


# ── prune_score_gate ────────────────────────────────────────────────────────────
def test_prune_score_gate():
    assert prune_score_gate(3, False, 20) == "defer"      # no credits
    assert prune_score_gate(-1, True, 20) == "error"      # scoring error sentinel
    assert prune_score_gate(25, True, 20) == "recovered"  # at/above floor
    assert prune_score_gate(20, True, 20) == "recovered"  # exactly floor -> recovered
    assert prune_score_gate(10, True, 20) == "below_floor"


# ── prune_below_floor_action ────────────────────────────────────────────────────
def _act(**kw):
    base = dict(age_days=100, delete_enabled=True, delete_active=True, has_fid=True,
                eff_delete_days=90, pressure_active=True, unmonitor_days=30, monitored=True)
    base.update(kw)
    return prune_below_floor_action(**base)


def test_prune_below_floor_delete_takes_precedence():
    assert _act() == "delete"


def test_prune_below_floor_unmonitor_when_no_delete():
    assert _act(delete_enabled=False) == "unmonitor"        # delete off -> unmonitor (dwell met)
    assert _act(has_fid=False) == "unmonitor"               # no file -> can't delete -> unmonitor
    assert _act(age_days=40) == "unmonitor"                 # past unmonitor dwell, not delete dwell


def test_prune_below_floor_age_when_comfortable_or_short():
    assert _act(delete_enabled=False, pressure_active=False) == "age"   # no pressure -> just clock
    assert _act(delete_enabled=False, age_days=10) == "age"             # dwell not met
    assert _act(delete_enabled=False, monitored=False) == "age"        # already unmonitored


# ── franchise_delete_exempt ───────────────────────────────────────────────────────
def _exempt(**kw):
    base = dict(collection_tmdb_id=10, sibling_tmdb_ids={1, 2, 3, 4},
                watched_tmdb_ids={1, 2, 3}, movie_tmdb_id=4, threshold=0.5, enabled=True)
    base.update(kw)
    return franchise_delete_exempt(**base)


def test_franchise_delete_exempt_off_is_byte_identical():
    for kw in (dict(enabled=False), dict(collection_tmdb_id=None), dict(threshold=0)):
        assert _exempt(**kw) is False


def test_franchise_delete_exempt_fraction_gate():
    # siblings {1,2,3} (excl self=4), watched {1,2,3} -> 3/3 = 1.0 >= 0.5 -> exempt
    assert _exempt() is True
    # only 1 of 3 siblings watched -> 0.33 < 0.5 -> not exempt
    assert _exempt(watched_tmdb_ids={1}) is False
    # exactly at threshold counts (2/4 siblings when self not in set)
    assert _exempt(sibling_tmdb_ids={1, 2, 3, 4}, movie_tmdb_id=99,
                   watched_tmdb_ids={1, 2}, threshold=0.5) is True   # 2/4 == 0.5
    # singleton collection (self only) -> no others -> not exempt
    assert _exempt(sibling_tmdb_ids={4}, movie_tmdb_id=4) is False


# ── budget_delete_cohort ──────────────────────────────────────────────────────────
def _cand(mid, score, size_gb):
    return {"mid": mid, "score": score, "size_gb": size_gb}


def test_budget_delete_cohort_off_returns_all_in_order():
    cands = [_cand(1, 5, 2), _cand(2, 1, 9), _cand(3, 3, 4)]
    # disabled / no need / non-positive / infinite -> ALL, original order (byte-identical)
    for kw in (dict(enabled=False, need_gb=5), dict(enabled=True, need_gb=None),
               dict(enabled=True, need_gb=0), dict(enabled=True, need_gb=float("inf"))):
        out = budget_delete_cohort([dict(c) for c in cands], **kw)
        assert [c["mid"] for c in out] == [1, 2, 3]


def test_budget_delete_cohort_worst_biggest_first_until_need():
    # worst score first (1 -> mid 2, 9GB), then 3 (score 3, 4GB), then 1 (score 5).
    cands = [_cand(1, 5, 2), _cand(2, 1, 9), _cand(3, 3, 4)]
    out = budget_delete_cohort(cands, need_gb=10.0, enabled=True)
    assert [c["mid"] for c in out] == [2, 3]   # 9 + 4 = 13 >= 10; stops before mid 1
    # tie on score -> bigger file first
    tie = [_cand(1, 2, 3), _cand(2, 2, 8)]
    out2 = budget_delete_cohort(tie, need_gb=5.0, enabled=True)
    assert [c["mid"] for c in out2] == [2]      # 8 >= 5 in one delete
