"""Tests for lifecycle.viewer_retention — the per-viewer protected interval that
replaces "3 hours after ANYONE watched it".

Covers the position/pace math (including the two degenerate cases: a single play,
where pace is UNDEFINED, and a same-day binge, where the naive denominator is
zero), the dormant path, the backward buffer across a season boundary, an account
with no history, and Tautulli's watched verdict + its pre-``watched_status``
fallback.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.managers.machine_learning.lifecycle.viewer_retention import (
    DEFAULT_RETENTION,
    account_dormant,
    account_facts,
    account_interval,
    account_pace,
    account_position,
    episode_ordinal,
    format_ordinal,
    forward_reach,
    interval_from_state,
    merge_state,
    resolve_retention_config,
    series_holds,
    series_intervals,
    series_protected_from_states,
    series_protected_ordinals,
    watched_by_tautulli,
)

_NOW = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
_CFG = resolve_retention_config({})


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _play(season, episode, days_ago, watched=True):
    return {"ordinal": episode_ordinal(season, episode),
            "at": _iso(days_ago), "watched": watched}


def _order(*specs):
    """Sorted ordinal list from (season, first_ep, last_ep) triples."""
    out = []
    for sn, first, last in specs:
        out.extend(episode_ordinal(sn, en) for en in range(first, last + 1))
    return sorted(out)


# ── ordinals ────────────────────────────────────────────────────────────────────
def test_episode_ordinal_matches_the_planner_key():
    assert episode_ordinal(2, 15) == 2 * 10_000 + 15 == 20015
    assert episode_ordinal(0, 1) == 1                     # specials sort first
    assert episode_ordinal(None, 3) is None               # pilot stub row
    assert episode_ordinal(1, None) is None
    assert format_ordinal(20015) == "S02E15"
    assert format_ordinal(None) == "S??E??"


# ── config ──────────────────────────────────────────────────────────────────────
def test_defaults_are_on_with_robert_s_numbers():
    cfg = resolve_retention_config({})
    assert cfg["enabled"] is True
    assert cfg["backward_buffer"] == 2
    assert cfg["horizon_days"] == 14
    assert cfg["watched_percent"] == 85
    assert cfg["dormant_days"] == 90          # inherited fallback


def test_dormant_days_inherits_the_prefetch_recency_gate():
    cfg = resolve_retention_config(
        {"acquisition": {"next_episode": {"recency_gate": {"enabled": True, "cold_days": 45}}}})
    assert cfg["dormant_days"] == 45          # one definition of "cold"
    # explicit override wins over the inherited value
    cfg2 = resolve_retention_config({
        "episode_retention": {"dormant_days": 120},
        "acquisition": {"next_episode": {"recency_gate": {"cold_days": 45}}}})
    assert cfg2["dormant_days"] == 120


def test_config_coerces_junk_and_clamps():
    cfg = resolve_retention_config({"episode_retention": {
        "backward_buffer": "-4", "horizon_days": "oops", "watched_percent": 500,
        "default_pace": -1, "pace_window_days": 0}})
    assert cfg["backward_buffer"] == 0
    assert cfg["horizon_days"] == 14           # unparseable → default
    assert cfg["watched_percent"] == 100
    assert cfg["default_pace"] == 0.0
    assert cfg["pace_window_days"] == 1.0
    assert resolve_retention_config({"episode_retention": {"enabled": False}})["enabled"] is False


# ── position ────────────────────────────────────────────────────────────────────
def test_position_is_the_furthest_watched_across_seasons():
    plays = [_play(1, 22, 40), _play(2, 3, 2), _play(1, 5, 50)]
    assert account_position(plays) == episode_ordinal(2, 3)


def test_position_ignores_sub_threshold_plays_and_empty_history():
    assert account_position([]) is None
    assert account_position([_play(3, 1, 1, watched=False)]) is None
    # a sample of a much later episode does NOT move the resume point
    plays = [_play(1, 4, 5), _play(9, 20, 1, watched=False)]
    assert account_position(plays) == episode_ordinal(1, 4)


# ── pace ────────────────────────────────────────────────────────────────────────
def test_pace_single_play_is_undefined_not_zero():
    assert account_pace([_play(1, 1, 3)], window_days=30) is None
    # ...and an undefined pace still projects, at default_pace
    assert forward_reach(None, horizon_days=14, default_pace=1.0, dormant=False) == 14


def test_pace_same_day_binge_does_not_divide_by_zero():
    # ten episodes, all at the same instant → span 0 days, clamped to 1
    plays = [{"ordinal": episode_ordinal(1, e), "at": _iso(2), "watched": True}
             for e in range(1, 11)]
    pace = account_pace(plays, window_days=30)
    assert pace == 9.0                               # (10 - 1) / 1 day
    assert forward_reach(pace, horizon_days=14, default_pace=1.0, dormant=False) == 126


def test_pace_is_measured_from_the_accounts_own_last_play_not_now():
    # 5 episodes over 4 days, but the binge ended 200 days ago. A now-anchored
    # window would see zero plays and read pace 0; the account-anchored window
    # still reports how fast they were moving.
    plays = [_play(1, e, 200 + (5 - e)) for e in range(1, 6)]
    assert account_pace(plays, window_days=30) == 1.0     # (5-1) / 4 days


def test_pace_counts_distinct_episodes_so_a_rewatch_burst_cannot_inflate_it():
    plays = [_play(1, 1, 10), _play(1, 2, 9)]
    burst = plays + [{"ordinal": episode_ordinal(1, 1), "at": _iso(9.5), "watched": True}]
    assert account_pace(plays, window_days=30) == account_pace(burst, window_days=30)


def test_pace_window_excludes_old_plays():
    # one episode 100 days before the last → outside a 30d window → only 1 in window
    plays = [_play(1, 1, 105), _play(1, 2, 5)]
    assert account_pace(plays, window_days=30) is None
    assert account_pace(plays, window_days=365) is not None


def test_pace_needs_timestamps():
    plays = [{"ordinal": episode_ordinal(1, 1), "at": None, "watched": True},
             {"ordinal": episode_ordinal(1, 2), "at": None, "watched": True}]
    assert account_pace(plays, window_days=30) is None


# ── dormancy ────────────────────────────────────────────────────────────────────
def test_dormant_after_the_cold_window_and_not_before():
    assert account_dormant(_iso(91), _NOW, dormant_days=90) is True
    assert account_dormant(_iso(90), _NOW, dormant_days=90) is False
    assert account_dormant(_iso(45), _NOW, dormant_days=90) is False
    assert account_dormant(_iso(45), _NOW, dormant_days=30) is True
    # missing/garbled timestamp → NOT dormant (fail-open: keep protecting)
    assert account_dormant(None, _NOW, dormant_days=90) is False
    assert account_dormant("not-a-date", _NOW, dormant_days=90) is False


def test_dormant_holds_position_and_buffer_but_drops_forward_reach():
    order = _order((1, 1, 22), (2, 1, 22))
    plays = [_play(1, e, 300 - e) for e in range(1, 11)]      # binged, then went quiet
    rec = account_interval("Stale", plays, order, _NOW, cfg=_CFG)
    assert rec["dormant"] is True
    assert rec["forward"] == 0
    assert rec["position_label"] == "S01E10"
    assert (rec["lo_label"], rec["hi_label"]) == ("S01E08", "S01E10")
    assert rec["span"] == 3                                   # buffer + the position itself


# ── the interval ────────────────────────────────────────────────────────────────
def test_backward_buffer_crosses_a_season_boundary_on_real_episodes():
    """S02E01 minus 2 must be S01E22/S01E23 — NOT the ordinal 19999 ('S01E9999')."""
    order = _order((1, 1, 23), (2, 1, 10))
    plays = [_play(2, 1, 1)]                       # single play → default pace
    rec = account_interval("Viewer", plays, order, _NOW, cfg=_CFG)
    assert rec["position_label"] == "S02E01"
    assert rec["lo_label"] == "S01E22"             # two REAL episodes back
    holds = series_holds([rec], order)
    assert episode_ordinal(1, 22) in holds and episode_ordinal(1, 23) in holds
    assert episode_ordinal(1, 21) not in holds     # the third episode back is releasable
    assert 19_999 not in holds                     # the nonsense ordinal never appears


def test_backward_buffer_clamps_at_the_start_of_the_series():
    order = _order((1, 1, 10))
    rec = account_interval("Viewer", [_play(1, 1, 1)], order, _NOW, cfg=_CFG)
    assert rec["lo_label"] == "S01E01"
    assert rec["lo_idx"] == 0


def test_forward_reach_is_bounded_by_what_the_series_actually_has():
    order = _order((1, 1, 6))
    plays = [_play(1, 1, 4), _play(1, 2, 3)]        # 1 ep/day
    rec = account_interval("Viewer", plays, order, _NOW, cfg=_CFG)
    assert rec["forward"] == 14                     # projected …
    assert rec["hi_label"] == "S01E06"              # … but clamped to the last owned ep
    assert rec["span"] == 6


def test_position_below_everything_owned_protects_from_the_front():
    """Watched S01E01 but only S02+ is on disk: the buffer contributes nothing and
    the forward reach starts at the first owned episode."""
    order = _order((2, 1, 10))
    plays = [_play(1, 1, 5), _play(1, 2, 4)]
    rec = account_interval("Viewer", plays, order, _NOW, cfg=_CFG)
    assert rec["lo_idx"] == 0 and rec["lo_label"] == "S02E01"


def test_position_below_everything_and_dormant_protects_nothing():
    order = _order((2, 1, 10))
    plays = [_play(1, 1, 300), _play(1, 2, 299)]
    rec = account_interval("Viewer", plays, order, _NOW, cfg=_CFG)
    assert rec["dormant"] is True and rec["span"] == 0 and rec["lo_idx"] is None


def test_account_with_zero_history_protects_nothing():
    order = _order((1, 1, 10))
    assert account_interval("Mom", [], order, _NOW, cfg=_CFG) is None
    holds, intervals = series_protected_ordinals(
        {"Mom": [], "Wyatt": []}, order, _NOW, cfg=_CFG)
    assert holds == {} and intervals == []


def test_two_accounts_union_their_intervals_and_holds_name_the_viewer():
    """The second-viewer case the rule exists for: one household member is on
    S03, another is still on S02 — S02 stays because of the trailing viewer."""
    order = _order((1, 1, 22), (2, 1, 22), (3, 1, 22))
    ahead = [_play(3, e, 20 - e) for e in range(1, 4)]
    behind = [_play(2, e, 20 - e) for e in range(1, 4)]
    holds, intervals = series_protected_ordinals(
        {"Ahead": ahead, "Behind": behind}, order, _NOW, cfg=_CFG)
    assert holds[episode_ordinal(2, 4)] == ["Behind"]
    assert holds[episode_ordinal(3, 1)] == ["Ahead"]        # inside Ahead's buffer
    assert episode_ordinal(1, 1) not in holds               # long behind everyone
    assert {r["account"] for r in intervals} == {"Ahead", "Behind"}


def test_disabled_config_protects_nothing():
    order = _order((1, 1, 10))
    cfg = resolve_retention_config({"episode_retention": {"enabled": False}})
    holds, intervals = series_protected_ordinals(
        {"Viewer": [_play(1, 5, 1)]}, order, _NOW, cfg=cfg)
    assert holds == {} and intervals == []


def test_empty_series_order_protects_nothing():
    assert account_interval("Viewer", [_play(1, 1, 1)], [], _NOW, cfg=_CFG)["span"] == 0


def test_zero_backward_buffer_still_holds_the_position_itself():
    order = _order((1, 1, 10))
    cfg = resolve_retention_config({"episode_retention": {
        "backward_buffer": 0, "horizon_days": 0}})
    rec = account_interval("Viewer", [_play(1, 5, 1)], order, _NOW, cfg=cfg)
    assert rec["span"] == 1 and rec["lo_label"] == rec["hi_label"] == "S01E05"


def test_series_intervals_are_account_ordered_and_skip_empty_accounts():
    order = _order((1, 1, 10))
    recs = series_intervals(
        {"Zed": [_play(1, 2, 1)], "Amy": [_play(1, 3, 1)], "Nobody": []},
        order, _NOW, cfg=_CFG)
    assert [r["account"] for r in recs] == ["Amy", "Zed"]


# ── Tautulli's watched verdict ──────────────────────────────────────────────────
def test_watched_status_is_preferred_over_the_percentage():
    # Tautulli says watched even though the percentage looks low (its own rules)
    assert watched_by_tautulli(1, 12, threshold_pct=85) is True
    # ...and says NOT watched even at a high percentage
    assert watched_by_tautulli(0.5, 99, threshold_pct=85) is False
    assert watched_by_tautulli(0, 99, threshold_pct=85) is False
    assert watched_by_tautulli("1", 0, threshold_pct=85) is True


def test_percent_fallback_when_watched_status_is_absent():
    assert watched_by_tautulli(None, 90, threshold_pct=85) is True
    assert watched_by_tautulli(None, 85, threshold_pct=85) is True
    assert watched_by_tautulli(None, 84, threshold_pct=85) is False
    assert watched_by_tautulli(None, 2, threshold_pct=85) is False


def test_rows_with_neither_field_stay_watched_so_the_cache_transition_is_silent():
    """Older cached rows carry no watched_status; they DO carry percent_complete,
    so the fallback covers them. A row with neither must not silently un-watch —
    a delete guard never shrinks a viewer's interval on missing data."""
    assert watched_by_tautulli(None, None, threshold_pct=85) is True
    assert watched_by_tautulli(None, "", threshold_pct=85) is True


def test_default_retention_block_matches_the_resolved_defaults():
    cfg = resolve_retention_config({})
    for k, v in DEFAULT_RETENTION.items():
        assert cfg[k] == v


# ── the durable position sidecar ────────────────────────────────────────────────
def test_account_facts_are_the_three_things_the_sidecar_persists():
    plays = [_play(1, e, 10 - e) for e in range(1, 5)]
    facts = account_facts(plays, cfg=_CFG)
    assert facts["position"] == episode_ordinal(1, 4)
    assert facts["episodes"] == 4
    assert facts["pace"] == 1.0
    assert facts["last_at"].startswith("2026-07-2")
    assert account_facts([], cfg=_CFG) is None


def test_merge_state_keeps_the_position_monotonic():
    """Tautulli prunes; a viewer's furthest-watched episode does not un-happen."""
    prior = {"position": episode_ordinal(4, 22), "last_at": _iso(40), "pace": 1.5, "episodes": 88}
    current = {"position": episode_ordinal(1, 2), "last_at": _iso(1), "pace": None, "episodes": 2}
    out = merge_state(prior, current)
    assert out["position"] == episode_ordinal(4, 22)     # remembered position wins
    assert out["last_at"] == _iso(1)                     # newest evidence wins
    assert out["pace"] == 1.5                            # perishable → last known
    assert out["episodes"] == 88


def test_merge_state_takes_a_fresh_pace_over_the_remembered_one():
    out = merge_state({"position": 10, "last_at": _iso(5), "pace": 9.0, "episodes": 3},
                      {"position": 20, "last_at": _iso(1), "pace": 0.5, "episodes": 4})
    assert (out["position"], out["pace"], out["episodes"]) == (20, 0.5, 4)


def test_merge_state_handles_either_side_missing():
    cur = {"position": 5, "last_at": _iso(1), "pace": None, "episodes": 1}
    assert merge_state(None, cur) == cur
    assert merge_state(cur, None) == cur
    assert merge_state(None, None) is None


def test_a_remembered_position_still_protects_when_history_is_gone():
    """The failure the sidecar exists to prevent: recompute-from-scratch drops the
    position to None and silently releases everything the rule was holding."""
    order = _order((1, 1, 22))
    remembered = {"position": episode_ordinal(1, 10), "last_at": _iso(5),
                  "pace": 1.0, "episodes": 10}
    holds, intervals = series_protected_from_states(
        {"Trizzd": remembered}, order, _NOW, cfg=_CFG)
    assert intervals[0]["position_label"] == "S01E10"
    assert episode_ordinal(1, 8) in holds and episode_ordinal(1, 9) in holds
    # ...vs no memory and no history:
    assert series_protected_from_states({}, order, _NOW, cfg=_CFG) == ({}, [])


def test_states_path_and_plays_path_agree():
    order = _order((1, 1, 22))
    plays = [_play(1, e, 10 - e) for e in range(1, 6)]
    a = series_protected_ordinals({"V": plays}, order, _NOW, cfg=_CFG)[0]
    b = series_protected_from_states(
        {"V": account_facts(plays, cfg=_CFG)}, order, _NOW, cfg=_CFG)[0]
    assert a == b


def test_interval_from_state_tolerates_junk():
    order = _order((1, 1, 5))
    assert interval_from_state("V", None, order, _NOW, cfg=_CFG) is None
    assert interval_from_state("V", {}, order, _NOW, cfg=_CFG) is None
    assert interval_from_state("V", {"position": None}, order, _NOW, cfg=_CFG) is None
    rec = interval_from_state("V", {"position": episode_ordinal(1, 3), "last_at": "garbled",
                                    "pace": "oops"}, order, _NOW, cfg=_CFG)
    assert rec["dormant"] is False and rec["pace"] is None and rec["forward"] == 14
