"""Tests for the 'wildly out of size profile' detector (sizing/anomaly.py)."""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.sizing.anomaly import (
    config_for,
    find_size_anomalies,
    implied_tier,
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
