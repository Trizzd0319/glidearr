"""Tests for acquisition.next_episode_planner — the pure derivation slices of the
Sonarr next-episode prefetch walk (ML Step 8). The service keeps the stateful walk
(per-series fetches, file resolution, df writes); these cover the extracted cores:
the per-series resume point, the runtime lookup, and the episode cap.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pandas as pd

from scripts.managers.machine_learning.acquisition.next_episode_planner import (
    DEFAULT_BUDGET_RAMP,
    DEFAULT_GRADUATED_CAP,
    DEFAULT_RECENCY_GATE,
    build_runtime_lookup,
    episode_cap,
    group_key_for_series,
    group_members,
    is_cold_series,
    last_watched_per_series,
    order_groups_by_recency,
    order_series_by_recency,
    series_budget_multiplier,
)


# ── group resolution (franchise / universe aware prefetch) ──────────────────────
def test_group_key_for_series_timeline_vs_airdate_vs_singleton():
    fran = {10: "one chicago", 11: "one chicago", 20: "mcu", 21: "mcu", 30: "rel", 31: "rel"}
    timeline = {10: 0, 11: 1, 20: 0, 21: 1}     # one chicago (curated) + mcu have timeline; 'rel' not
    assert group_key_for_series(10, fran, timeline) == ("one chicago", "timeline")  # curated saga order
    assert group_key_for_series(20, fran, timeline) == ("mcu", "timeline")
    assert group_key_for_series(30, fran, timeline) == ("rel", "airdate")   # grouped, no timeline
    assert group_key_for_series(99, fran, timeline) == ("series:99", "series")  # ungrouped → singleton


def test_group_key_empty_maps_make_every_series_a_singleton():
    # Feature OFF (empty maps) → each series is its own group → group walk == per-series walk.
    assert group_key_for_series(5, {}, {}) == ("series:5", "series")
    assert group_key_for_series(5, None, None) == ("series:5", "series")


def test_group_members_orders_timeline_by_index_airdate_by_id():
    fran = {11: "one chicago", 10: "one chicago", 21: "mcu", 20: "mcu", 22: "mcu", 41: "rel", 40: "rel"}
    timeline = {10: 0, 11: 1, 20: 0, 21: 1, 22: 2}   # one chicago + mcu timeline; 'rel' has none
    groups = group_members([22, 11, 20, 10, 21, 41, 40], fran, timeline)
    assert groups["mcu"]["members"] == [20, 21, 22] and groups["mcu"]["kind"] == "timeline"
    assert groups["one chicago"]["members"] == [10, 11] and groups["one chicago"]["kind"] == "timeline"
    assert groups["rel"]["members"] == [40, 41] and groups["rel"]["kind"] == "airdate"  # by series_id


def test_group_members_mixed_group_is_order_independent_timeline():
    # A mixed group (member 5 has a timeline_index, 6 doesn't) must resolve to the SAME kind +
    # member order regardless of iteration order — a group is timeline if ANY member is indexed.
    fran = {5: "fast", 6: "fast"}
    timeline = {5: 0}
    g_fwd = group_members([5, 6], fran, timeline)["fast"]
    g_rev = group_members([6, 5], fran, timeline)["fast"]
    assert g_fwd["kind"] == "timeline" == g_rev["kind"]
    assert g_fwd["members"] == [5, 6] == g_rev["members"]   # indexed (5) first, non-indexed (6) last


def test_order_groups_by_recency_most_recent_member_first():
    last = pd.DataFrame([
        {"series_id": 10, "last_watched_at": "2026-06-01T00:00:00Z"},   # one chicago (older)
        {"series_id": 20, "last_watched_at": "2026-06-19T00:00:00Z"},   # mcu (newer)
    ])
    group_of = {10: "one chicago", 20: "mcu"}
    assert order_groups_by_recency(last, group_of) == ["mcu", "one chicago"]


# ── last_watched_per_series ─────────────────────────────────────────────────────
def test_last_watched_per_series_picks_highest_watched():
    df = pd.DataFrame([
        {"series_id": 1, "series_title": "A", "season_number": 1, "episode_number": 1, "is_watched": True,  "keep_policy": None},
        {"series_id": 1, "series_title": "A", "season_number": 1, "episode_number": 3, "is_watched": True,  "keep_policy": "keep_series"},
        {"series_id": 1, "series_title": "A", "season_number": 1, "episode_number": 5, "is_watched": False, "keep_policy": None},  # unwatched -> ignored
        {"series_id": 2, "series_title": "B", "season_number": 1, "episode_number": 9, "is_watched": True,  "keep_policy": None},
        {"series_id": 2, "series_title": "B", "season_number": 2, "episode_number": 1, "is_watched": True,  "keep_policy": None},  # S02E01 > S01E09 by sort key
    ])
    out = last_watched_per_series(df).set_index("series_id")
    assert (out.at[1, "season_number"], out.at[1, "episode_number"]) == (1, 3)
    assert out.at[1, "keep_policy"] == "keep_series"        # carries the resume row's columns
    assert (out.at[2, "season_number"], out.at[2, "episode_number"]) == (2, 1)


def test_last_watched_per_series_empty_when_nothing_watched():
    df = pd.DataFrame([
        {"series_id": 1, "season_number": 1, "episode_number": 1, "is_watched": False},
    ])
    assert last_watched_per_series(df).empty


def test_last_watched_per_series_handles_nan_watched_flag():
    # NaN is_watched -> treated as False (not watched)
    df = pd.DataFrame([
        {"series_id": 1, "season_number": 1, "episode_number": 2, "is_watched": None},
        {"series_id": 1, "season_number": 1, "episode_number": 4, "is_watched": True},
    ])
    out = last_watched_per_series(df).set_index("series_id")
    assert (out.at[1, "season_number"], out.at[1, "episode_number"]) == (1, 4)


# ── build_runtime_lookup ────────────────────────────────────────────────────────
def test_build_runtime_lookup_keeps_positive_only():
    df = pd.DataFrame([
        {"series_id": 1, "season_number": 1, "episode_number": 1, "runtime_seconds": 1800},
        {"series_id": 1, "season_number": 1, "episode_number": 2, "runtime_seconds": 0},      # zero -> skip
        {"series_id": 1, "season_number": 1, "episode_number": 3, "runtime_seconds": None},   # NaN  -> skip
        {"series_id": 2, "season_number": 2, "episode_number": 5, "runtime_seconds": 2400.0},
    ])
    assert build_runtime_lookup(df) == {(1, 1, 1): 1800.0, (2, 2, 5): 2400.0}


def test_build_runtime_lookup_skips_nan_keys():
    df = pd.DataFrame([
        {"series_id": None, "season_number": 1, "episode_number": 1, "runtime_seconds": 1800},  # NaN sid -> skip
        {"series_id": 3, "season_number": 1, "episode_number": 1, "runtime_seconds": 1500},
    ])
    assert build_runtime_lookup(df) == {(3, 1, 1): 1500.0}


def test_build_runtime_lookup_no_column():
    df = pd.DataFrame([{"series_id": 1, "season_number": 1, "episode_number": 1}])
    assert build_runtime_lookup(df) == {}


# ── episode_cap ─────────────────────────────────────────────────────────────────
def test_episode_cap_normal_length_capped():
    assert episode_cap(2700.0, short_episode_s=600.0, max_ep=6) == 6


def test_episode_cap_short_episodes_uncapped():
    assert math.isinf(episode_cap(300.0, short_episode_s=600.0, max_ep=6))


def test_episode_cap_boundary_is_capped():
    # exactly short_episode_s counts as normal-length (>=) -> capped
    assert episode_cap(600.0, short_episode_s=600.0, max_ep=6) == 6


def test_episode_cap_graduated_disabled_is_the_cliff():
    # falsy / disabled graduated -> byte-identical to the legacy cliff
    for g in (None, {}, {"enabled": False}):
        assert episode_cap(300.0, short_episode_s=600.0, max_ep=6, graduated=g) == float("inf")
        assert episode_cap(2700.0, short_episode_s=600.0, max_ep=6, graduated=g) == 6


def test_episode_cap_graduated_scales_inversely_with_length():
    g = {"enabled": True, "reference_minutes": 45, "base_cap": 6, "hard_cap": 24}
    assert episode_cap(2700.0, short_episode_s=600.0, max_ep=6, graduated=g) == 6   # 45 min -> base
    assert episode_cap(1320.0, short_episode_s=600.0, max_ep=6, graduated=g) == 12  # 22 min -> 2x
    assert episode_cap(660.0,  short_episode_s=600.0, max_ep=6, graduated=g) == 24  # 11 min -> hard cap
    assert episode_cap(3600.0, short_episode_s=600.0, max_ep=6, graduated=g) == 6   # 60 min -> floored at base


def test_episode_cap_graduated_zero_runtime_falls_back():
    # zero/missing runtime must not divide-by-zero; uses short_episode_s, then clamps
    g = {"enabled": True, "reference_minutes": 45, "base_cap": 6, "hard_cap": 24}
    assert episode_cap(0.0, short_episode_s=600.0, max_ep=6, graduated=g) == 24


def test_episode_cap_graduated_misconfigured_hard_below_base_stays_sane():
    # base_cap > hard_cap must not invert into a constant floor that ignores length;
    # hard_cap is lifted to >= base_cap so the band stays sane (benign, no cliff).
    g = {"enabled": True, "reference_minutes": 45, "base_cap": 10, "hard_cap": 4}
    assert episode_cap(2700.0, short_episode_s=600.0, max_ep=6, graduated=g) == 10  # 45min -> base
    assert episode_cap(300.0,  short_episode_s=600.0, max_ep=6, graduated=g) == 10  # short, but hard lifted to base


# ── order_series_by_recency ─────────────────────────────────────────────────────
def test_order_series_by_recency_most_recent_first_nat_last():
    df = pd.DataFrame([
        {"series_id": 1, "last_watched_at": "2026-01-01T00:00:00Z"},
        {"series_id": 2, "last_watched_at": "2026-06-01T00:00:00Z"},  # most recent
        {"series_id": 3, "last_watched_at": None},                    # NaT -> last
        {"series_id": 4, "last_watched_at": "2026-03-01T00:00:00Z"},
    ])
    out = order_series_by_recency(df)
    assert list(out["series_id"]) == [2, 4, 1, 3]


def test_order_series_by_recency_missing_column_is_noop():
    df = pd.DataFrame([{"series_id": 1}, {"series_id": 2}])
    assert list(order_series_by_recency(df)["series_id"]) == [1, 2]


# ── is_cold_series ──────────────────────────────────────────────────────────────
_NOW = datetime(2026, 6, 9, tzinfo=timezone.utc)


def test_is_cold_series_cold_when_old_and_no_upcoming():
    assert is_cold_series("2026-01-01T00:00:00Z", _NOW, cold_days=30, has_upcoming=False) is True


def test_is_cold_series_recent_is_not_cold():
    assert is_cold_series("2026-06-01T00:00:00Z", _NOW, cold_days=30, has_upcoming=False) is False


def test_is_cold_series_airing_soon_exemption():
    # old last-watch but an episode airing soon -> never cold (mid-season break)
    assert is_cold_series("2025-01-01T00:00:00Z", _NOW, cold_days=30, has_upcoming=True) is False


def test_is_cold_series_disabled_and_unparseable():
    assert is_cold_series("2025-01-01T00:00:00Z", _NOW, cold_days=None, has_upcoming=False) is False  # off
    assert is_cold_series(None, _NOW, cold_days=30, has_upcoming=False) is False                       # no date
    assert is_cold_series("not-a-date", _NOW, cold_days=30, has_upcoming=False) is False               # unparseable


# ── series_budget_multiplier ────────────────────────────────────────────────────
def test_series_budget_multiplier_off_is_exactly_one():
    # unconfigured / disabled ramp -> EXACTLY 1.0 for every input (byte-identical parity)
    for pct in (None, float("nan"), 0, 50, 100, "bad"):
        m = series_budget_multiplier(pct, {})
        assert m == 1.0 and type(m) is float
    assert series_budget_multiplier(50, None) == 1.0
    # enabled:false disables even with mults present (the {enabled} contract / footgun guard)
    assert series_budget_multiplier(0, {"enabled": False, "low_mult": 0.5, "high_mult": 1.5}) == 1.0


def test_series_budget_multiplier_identity_ramp_is_one():
    # an explicit 1.0..1.0 ramp is still exactly 1.0 everywhere (no float drift)
    r = {"enabled": True, "low_mult": 1.0, "high_mult": 1.0}
    assert series_budget_multiplier(0, r) == 1.0
    assert series_budget_multiplier(73, r) == 1.0


def test_series_budget_multiplier_interpolates_and_clamps():
    r = {"enabled": True, "low_mult": 0.5, "high_mult": 1.5}
    assert series_budget_multiplier(0, r) == 0.5
    assert series_budget_multiplier(100, r) == 1.5
    assert series_budget_multiplier(50, r) == 1.0
    assert series_budget_multiplier(150, r) == 1.5   # clamp high
    assert series_budget_multiplier(-10, r) == 0.5   # clamp low


def test_series_budget_multiplier_null_percentile_neutral_when_ramped():
    # stale/absent percentile -> neutral 1.0 even with a ramp configured
    r = {"enabled": True, "low_mult": 0.5, "high_mult": 1.5}
    assert series_budget_multiplier(None, r) == 1.0
    assert series_budget_multiplier(float("nan"), r) == 1.0


# ── active-by-default constants ─────────────────────────────────────────────────
def test_default_constants_are_on_and_drive_the_enabled_paths():
    # The recommended defaults the service falls back to must all be ENABLED and
    # produce the graduated / skip / ramp behavior (not the legacy off paths).
    assert DEFAULT_GRADUATED_CAP.get("enabled") is True
    assert DEFAULT_RECENCY_GATE.get("enabled") is True
    assert DEFAULT_BUDGET_RAMP.get("enabled") is True
    # graduated: 11-min episode caps at hard_cap (24), not the legacy inf
    assert episode_cap(660.0, short_episode_s=600.0, max_ep=6, graduated=DEFAULT_GRADUATED_CAP) == 24
    # ramp: bottom/top percentile actually spread around neutral
    assert series_budget_multiplier(0, DEFAULT_BUDGET_RAMP) == 0.5
    assert series_budget_multiplier(100, DEFAULT_BUDGET_RAMP) == 1.5
    # recency cold_days is a positive horizon
    assert DEFAULT_RECENCY_GATE["cold_days"] > 0
