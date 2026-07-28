"""labels/test_snapshot_signal_columns.py — the snapshot's sig_ column vocabulary.

Written because ``sig_D4_transcode_risk`` was missing from BOTH live 2026-07 partitions
and read as a flattener bug. It is not one: the row builders flatten the ALREADY-PERSISTED
``watchability_breakdown``, and the last live run predated the change that introduced the
key. These tests pin the two properties that make that diagnosis checkable rather than a
story — the flattener emits EVERY breakdown key, and the writer UNIONS columns across
partitions rather than reindexing new ones away — plus the presence of both new signals.
"""
from __future__ import annotations

import json

import pandas as pd

from scripts.managers.machine_learning.labels.snapshots import (
    append_snapshot,
    build_movie_snapshot_rows,
    build_show_snapshot_rows,
    flatten_breakdown,
    load_snapshots,
)

_BREAKDOWN = {
    "A1_keep_policy": 15.0, "A2_completion": 0.0, "A3_rewatch": 0.0, "A4_user_rating": 0.0,
    "A5_intent": 4.8,
    "B1_actor_affinity": 1.0, "C4_person_affinity": 0.0,
    "D1_device_capability": 0.0, "D2_transcode_avoidance": 0.0,
    "D3_platform_ceiling": 0.0, "D4_transcode_risk": -3.25,
    "F1_critic_consensus": 12.0, "G1_language": 0.0,
    "_total_raw": 29.55, "_total_final": 30,
}


def test_flattener_emits_every_breakdown_key_including_the_two_new_ones():
    out = flatten_breakdown(json.dumps(_BREAKDOWN))
    assert out["sig_A5_intent"] == 4.8
    assert out["sig_D4_transcode_risk"] == -3.25
    # …and nothing is whitelisted away: every non-meta key becomes a column.
    assert set(out) == {f"sig_{k}" for k in _BREAKDOWN if not k.startswith("_")}


def test_movie_and_show_rows_both_carry_the_new_columns():
    mdf = pd.DataFrame([{"tmdb_id": 7, "title": "M", "watchability_score": 30,
                         "watchability_breakdown": json.dumps(_BREAKDOWN),
                         "is_watched": False, "watch_count": 0}])
    row = build_movie_snapshot_rows(mdf, "standard")[0]
    assert row["sig_A5_intent"] == 4.8 and row["sig_D4_transcode_risk"] == -3.25

    sdf = pd.DataFrame([{"series_id": 3, "series_title": "S", "watchability_score": 30,
                         "watchability_breakdown": json.dumps(_BREAKDOWN),
                         "is_watched": False, "watch_count": 0}])
    srow = build_show_snapshot_rows(sdf, "standard", tvdb_by_series={3: 99})[0]
    assert srow["sig_A5_intent"] == 4.8 and srow["sig_D4_transcode_risk"] == -3.25


def test_a_new_signal_needs_no_migration_the_writer_unions_columns(tmp_path):
    """THE D4 DIAGNOSIS, made checkable: append a partition written under the OLD
    vocabulary, then one under the new. The old rows must keep their values and the new
    columns must simply appear (NULL on the old rows) — no reindex, no truncation."""
    old_bd = {k: v for k, v in _BREAKDOWN.items()
              if k not in ("A5_intent", "D4_transcode_risk")}
    old = pd.DataFrame([{"tmdb_id": 1, "title": "Old", "watchability_score": 20,
                         "watchability_breakdown": json.dumps(old_bd),
                         "is_watched": False, "watch_count": 0}])
    new = pd.DataFrame([{"tmdb_id": 2, "title": "New", "watchability_score": 30,
                         "watchability_breakdown": json.dumps(_BREAKDOWN),
                         "is_watched": False, "watch_count": 0}])
    append_snapshot(tmp_path, "radarr", "standard",
                    build_movie_snapshot_rows(old, "standard", snapshot_ts="2026-07-01T00:00:00+00:00"))
    append_snapshot(tmp_path, "radarr", "standard",
                    build_movie_snapshot_rows(new, "standard", snapshot_ts="2026-07-02T00:00:00+00:00"))

    df = load_snapshots(tmp_path, services=("radarr",))
    assert len(df) == 2
    assert "sig_A5_intent" in df.columns and "sig_D4_transcode_risk" in df.columns
    got = df.set_index("entity_id")
    assert got.at["2", "sig_D4_transcode_risk"] == -3.25
    assert pd.isna(got.at["1", "sig_D4_transcode_risk"])       # genuinely absent, not 0-filled
    assert got.at["1", "sig_A1_keep_policy"] == 15.0           # old row untouched


def test_the_challenger_reads_a_missing_signal_as_zero_so_no_backfill_is_needed():
    """A row scored under a revision where the signal did not exist contributed exactly 0
    of it — which is what build_feature_matrix already coerces NULL to. Existing partitions
    therefore need nothing."""
    from scripts.managers.machine_learning.challenger.gbt_shadow import (
        build_feature_matrix, feature_columns,
    )
    df = pd.DataFrame([{"sig_A1_keep_policy": 15.0, "sig_A5_intent": None,
                        "sig_D4_transcode_risk": float("nan"), "size_bytes": 0}])
    cols = feature_columns(df)
    assert "sig_A5_intent" in cols and "sig_D4_transcode_risk" in cols
    X = build_feature_matrix(df, cols)
    assert X[0][cols.index("sig_A5_intent")] == 0.0
    assert X[0][cols.index("sig_D4_transcode_risk")] == 0.0
