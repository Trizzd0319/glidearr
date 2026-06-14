"""
ledger/decision_ledger.py — stamp a planned action onto a Parquet row (pure).
================================================================================
Relocated from the per-service ``_stamp_plan`` / ``_stamp_universe_plan`` copies
(ML Step 6). Writes the decision-ledger columns — ``planned_action`` /
``plan_reason`` / ``plan_reclaim_gb`` — that ``ledger/plan_summary`` later rolls up
into the dry-run "what I'd do" table (the parity oracle). PURE — operates on a
DataFrame cell; no HTTP, no global_cache. The service ensures the columns exist /
persists the Parquet.

Public API:
  * stamp(df, idx, action, reason, reclaim_gb) -> None
        reclaim_gb is +GiB freed (delete/downgrade) or -GiB consumed (upgrade).
  * stamp_universe_plan(df, idx, action, target_profile) -> None
        the universe quality-change variant: derives the SIGNED space impact from
        runtime x the target profile's top quality vs the current file size.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.sizing.size_model import estimate_gb_for_profile


def stamp(df, idx, action: str, reason: str, reclaim_gb) -> None:
    """Record a planned action on row ``idx`` (preview-safe; persisted in dry_run).
    ``reclaim_gb`` is +GiB freed (delete/downgrade) or -GiB consumed (upgrade)."""
    df.at[idx, "planned_action"] = action
    df.at[idx, "plan_reason"]    = reason
    try:
        df.at[idx, "plan_reclaim_gb"] = round(float(reclaim_gb), 2) if reclaim_gb is not None else None
    except (TypeError, ValueError):
        df.at[idx, "plan_reclaim_gb"] = None


def stamp_universe_plan(df, idx, action: str, target_profile: dict) -> None:
    """Stamp the ledger for a universe quality change with the SIGNED space impact:
    a downgrade FREES (+(old-new)), an upgrade CONSUMES (-(new-old)), estimated from
    ``runtime_minutes`` x the target profile's top quality vs the current file size.
    Ledger-only — callers must NOT persist quality_profile_id speculatively in dry_run."""
    try:
        _rt = df.at[idx, "runtime_minutes"] if "runtime_minutes" in df.columns else None
        rt_min = float(_rt) if (_rt is not None and pd.notna(_rt)) else 0.0
        est_target = estimate_gb_for_profile(target_profile, rt_min, 1) if rt_min > 0 else 0.0
        _sz = df.at[idx, "size_bytes"] if "size_bytes" in df.columns else None
        cur_gb = (float(_sz) / (1024 ** 3)) if (_sz is not None and pd.notna(_sz)) else 0.0
        if action == "downgrade":
            reclaim = round(max(0.0, cur_gb - est_target), 2)     # + space freed
        else:
            reclaim = -round(max(0.0, est_target - cur_gb), 2)    # - space consumed
        df.at[idx, "planned_action"]  = action
        df.at[idx, "plan_reason"]     = f"universe {action}"
        df.at[idx, "plan_reclaim_gb"] = reclaim
    except Exception:
        pass
