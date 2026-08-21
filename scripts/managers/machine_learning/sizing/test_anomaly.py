"""Tests for the 'wildly out of size profile' detector (sizing/anomaly.py)."""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.sizing.anomaly import (
    config_for,
    find_size_anomalies,
    implied_tier,
    prune_attempts,
    recommend_action,
    record_attempt,
    should_attempt,
)


def _row(title, quality, runtime_min, size_gb, resolution=1080):
    return {"title": title, "year": 2009, "quality_name": quality,
            "runtime_minutes": runtime_min, "resolution": resolution,
            "size_bytes": int(size_gb * 1024 ** 3)}


def test_flags_the_transformers_case():
    # A 150-min movie graded Bluray-720p (calibrated ~52 MiB/min → ~7.6 GB) at 45 GB is ~6x.
    # The 720p cohort needs >= min_samples siblings for the measured-mean baseline; give it some.
    rows = [_row(f"normal {i}", "Bluray-720p", 150, 7.5) for i in range(8)]
    rows.append(_row("Transformers ROTF", "Bluray-720p", 150, 45.0))
    out = find_size_anomalies(pd.DataFrame(rows), id_cols=("title",), over_ratio=3.0)
    titles = [r["title"] for r in out]
    assert "Transformers ROTF" in titles
    hit = next(r for r in out if r["title"] == "Transformers ROTF")
    assert hit["verdict"] == "oversized"
    assert hit["ratio"] >= 3.0
    assert hit["reclaim_gb"] > 30          # ~37 GB reclaimable at the in-profile size
    # the bitrate implies a far higher tier than the 720p grade
    assert "720p" not in hit["looks_like"]


def test_normal_files_are_not_flagged():
    rows = [_row(f"ok {i}", "Bluray-720p", 120, 6.0) for i in range(10)]
    out = find_size_anomalies(pd.DataFrame(rows), id_cols=("title",))
    assert out == []


def test_flags_undersized_fake():
    # A 2160p remux that is only 0.5 GB for 120 min is far too small to be real → undersized.
    rows = [_row(f"real remux {i}", "Remux-2160p", 120, 45.0) for i in range(8)]
    rows.append(_row("fake remux", "Remux-2160p", 120, 0.5))
    out = find_size_anomalies(pd.DataFrame(rows), id_cols=("title",))
    fake = next((r for r in out if r["title"] == "fake remux"), None)
    assert fake is not None and fake["verdict"] == "undersized"


def test_oversized_sorted_by_reclaim_first():
    rows = [_row(f"base {i}", "Bluray-1080p", 120, 8.0) for i in range(10)]
    rows.append(_row("big", "Bluray-1080p", 120, 60.0))     # huge reclaim
    rows.append(_row("medium", "Bluray-1080p", 120, 30.0))  # smaller reclaim
    out = find_size_anomalies(pd.DataFrame(rows), id_cols=("title",))
    over = [r for r in out if r["verdict"] == "oversized"]
    assert over[0]["title"] == "big"        # biggest reclaim leads


def test_thin_cohort_falls_back_to_calibrated_table():
    # Only ONE Bluray-720p file (below min_samples) — its own size can't define "normal";
    # the calibrated table (~52 MiB/min) is used, so a 45 GB 720p is still flagged.
    out = find_size_anomalies(pd.DataFrame([_row("lonely", "Bluray-720p", 150, 45.0)]),
                              id_cols=("title",), min_samples=8)
    assert out and out[0]["verdict"] == "oversized"


def test_runtime_unit_seconds_for_episodes():
    rows = [{"series_title": f"S{i}", "quality_name": "Bluray-1080p",
             "runtime_seconds": 2700, "resolution": 1080,
             "size_bytes": int(3 * 1024 ** 3)} for i in range(10)]
    rows.append({"series_title": "bloated ep", "quality_name": "Bluray-1080p",
                 "runtime_seconds": 2700, "resolution": 1080, "size_bytes": int(30 * 1024 ** 3)})
    out = find_size_anomalies(pd.DataFrame(rows), id_cols=("series_title",),
                              runtime_col="runtime_seconds", runtime_unit="seconds")
    assert any(r["series_title"] == "bloated ep" and r["verdict"] == "oversized" for r in out)


def test_implied_tier_diagnostic():
    assert "2160p" in implied_tier(389.0)   # remux-class bitrate
    assert implied_tier(0) == ""


def test_missing_columns_returns_empty():
    assert find_size_anomalies(pd.DataFrame([{"title": "x"}]), id_cols=("title",)) == []
    assert find_size_anomalies(None) == []


def test_config_for_merges_over_defaults():
    cfg = config_for({"size_anomaly": {"over_ratio": 2.5, "enabled": False}})
    assert cfg["over_ratio"] == 2.5 and cfg["enabled"] is False
    assert cfg["under_ratio"] == 0.3 and cfg["min_samples"] == 8   # untouched defaults
    assert config_for({})["enabled"] is True                       # bare default
    # ledger knobs ship with defaults so an existing config keeps the bound
    assert config_for({})["max_regrab_attempts"] == 3
    assert config_for({})["regrab_retry_days"] == 7


# ── Grade routing: a bloated BROADCAST capture is mis-graded, not over-bitrated ──────

def test_broadcast_grades_route_to_rescan_not_regrab():
    """Broadcast bitrate is capped well below disc bitrate, so an HDTV-graded file at several
    times its expected rate is a disc source labelled wrong. Re-grabbing cannot fix that — the
    search asks for an UPGRADE and a smaller file is never one — so it must rescan."""
    assert recommend_action("oversized", "HDTV-1080p") == "rescan"   # every Dragon Ball Z row
    assert recommend_action("oversized", "HDTV-720p") == "rescan"
    assert recommend_action("oversized", "SDTV") == "rescan"         # unchanged, junk/SD


def test_disc_and_uhd_broadcast_grades_still_regrab():
    """A disc tier IS the top of its resolution class, so a bloated file there is genuinely
    over-bitrated. HDTV-2160p stays out of the mis-grade set: UHD broadcast is legitimately
    high-bitrate and has no higher HDTV tier to be mistaken for."""
    assert recommend_action("oversized", "Bluray-1080p") == "regrab"
    assert recommend_action("oversized", "Bluray-2160p") == "regrab"
    assert recommend_action("oversized", "HDTV-2160p") == "regrab"
    assert recommend_action("oversized", "WEBRip-720p") == "regrab"  # deliberately unchanged


def test_undersized_always_rescans_regardless_of_grade():
    assert recommend_action("undersized", "Bluray-2160p") == "rescan"
    assert recommend_action("undersized", "HDTV-1080p") == "rescan"
    assert recommend_action("normal", "HDTV-1080p") == ""


# ── Re-grab attempt ledger ────────────────────────────────────────────────

_DAY = 86400.0
_NOW = 1_760_000_000.0
_BUDGET = {"max_attempts": 3, "retry_days": 7}


def test_absent_entry_is_a_first_attempt():
    assert should_attempt(None, 4_000_000_000, _NOW, **_BUDGET) == (True, "first")
    assert should_attempt({}, 4_000_000_000, _NOW, **_BUDGET) == (True, "first")


def test_cooldown_then_retry_then_abandon():
    entry = record_attempt(None, 4_000_000_000, _NOW)
    assert entry["attempts"] == 1
    # same run / same week: too soon
    assert should_attempt(entry, 4_000_000_000, _NOW + 60, **_BUDGET) == (False, "cooling")
    # window elapsed, budget left
    assert should_attempt(entry, 4_000_000_000, _NOW + 8 * _DAY, **_BUDGET) == (True, "retry")
    # burn the budget
    cur, t = None, _NOW
    for _ in range(3):
        cur = record_attempt(cur, 4_000_000_000, t)
        t += 8 * _DAY
    assert cur["attempts"] == 3
    assert should_attempt(cur, 4_000_000_000, t, **_BUDGET) == (False, "abandoned")


def test_a_size_change_resets_the_budget():
    """The file that was failing to move has moved, so its history no longer describes it."""
    spent = {"attempts": 3, "last_at": _NOW, "size_bytes": 4_000_000_000}
    assert should_attempt(spent, 1_200_000_000, _NOW + 60, **_BUDGET) == (True, "changed")
    assert record_attempt(spent, 1_200_000_000, _NOW)["attempts"] == 1


def test_unknown_size_must_not_read_as_changed():
    """P-C. Absent/unparseable is UNKNOWN, not 'different' — reading it as a change would reset
    the budget on every run and restore the exact churn the ledger exists to stop."""
    spent = {"attempts": 3, "last_at": _NOW, "size_bytes": 4_000_000_000}
    assert should_attempt(spent, None, _NOW + 9 * _DAY, **_BUDGET) == (False, "abandoned")
    no_prev = {"attempts": 3, "last_at": _NOW, "size_bytes": None}
    assert should_attempt(no_prev, 4_000_000_000, _NOW + 9 * _DAY, **_BUDGET) == (False, "abandoned")


def test_corrupt_entries_degrade_instead_of_raising():
    for junk in ({"attempts": "x"}, {"last_at": "y", "attempts": 1}, {"size_bytes": "z"},
                 {"size_bytes": object()}):
        ok, why = should_attempt(junk, 4_000_000_000, _NOW, **_BUDGET)
        assert isinstance(ok, bool) and isinstance(why, str)
        assert record_attempt(junk, 4_000_000_000, _NOW)["attempts"] >= 1


def test_zero_max_attempts_disables_the_bound():
    spent = {"attempts": 99, "last_at": 0, "size_bytes": 1}
    assert should_attempt(spent, 1, _NOW, max_attempts=0, retry_days=0)[0] is True


def test_prune_drops_files_that_stopped_being_anomalous():
    store = {"10": {"attempts": 3}, "20": {"attempts": 1}}
    assert prune_attempts(store, [20]) == {"20": {"attempts": 1}}
    assert prune_attempts(store, ["10", 20]) == store      # int/str ids both match
    assert prune_attempts(store, []) == {}
    assert prune_attempts(None, [1]) == {}


def test_the_churn_converges():
    """The measured failure was the same 38 episodes searched on consecutive runs forever.
    With the ledger a stuck file is searched 3 times and then left alone."""
    entry, t, searches = None, _NOW, 0
    for _ in range(12):
        ok, _why = should_attempt(entry, 4_100_000_000, t, **_BUDGET)
        if ok:
            searches += 1
            entry = record_attempt(entry, 4_100_000_000, t)
        t += 8 * _DAY
    assert searches == 3


def test_size_bytes_is_on_the_row_for_the_ledger():
    """The ledger's change-detection needs an exact size, not the 2dp display GB."""
    rows = find_size_anomalies(
        pd.DataFrame([_row("Bloat", "Bluray-720p", 45, 30.0, resolution=720)]),
        id_cols=("title",), runtime_col="runtime_minutes")
    assert rows and rows[0]["size_bytes"] == int(30.0 * 1024 ** 3)
