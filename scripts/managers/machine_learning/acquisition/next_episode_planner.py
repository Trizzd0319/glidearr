"""acquisition/next_episode_planner.py — next-up prefetch derivations (pure).
==============================================================================
The pure decision/derivation slices of ``sonarr/cache/episode_files._compute_next_episodes``
(ML Step 8). That method walks forward from each series' last-watched episode,
accumulating a runtime budget and flagging upcoming episodes ``next_episode=True``
for acquisition. The walk itself is heavily I/O- and state-interleaved (per-series
Sonarr fetches, ``_resolve_episode_file``, df mutations and row appends mid-loop),
so — as with ``jit_planner`` — only the side-effect-free slices live here; the
service keeps the stateful walk + the fetch/monitor/search APPLY.

PURE — pandas + stdlib only; no HTTP, no global_cache, no df writes.

Public API:
  * last_watched_per_series(df) -> DataFrame
        one row per series at its highest watched (season, episode) — the resume
        point the walk starts from. Empty in → empty out (caller short-circuits).
  * build_runtime_lookup(df) -> dict[(sid, season, episode), float]
        per-episode runtime_seconds (positive only), so the walk budgets without
        extra API calls.
  * episode_cap(series_runtime_s, *, short_episode_s, max_ep, graduated=None) -> float
        the per-series episode cap — the legacy short/normal cliff by default, or an
        opt-in graduated count cap that scales inversely with episode length.
  * order_series_by_recency(last_by_series) -> DataFrame
        the resume frame sorted most-recently-watched first (opt-in walk ordering).
  * is_cold_series(last_watched_at, now, *, cold_days, has_upcoming) -> bool
        whether to skip a series' prefetch — cold by date AND not airing soon (the
        mid-season-break exemption is mandatory).
  * series_budget_multiplier(percentile, ramp) -> float
        per-series prefetch-budget multiplier from watchability_percentile; exactly
        1.0 (byte-identical) when the ramp is unconfigured.
"""
from __future__ import annotations

import math

import pandas as pd

# ── recommended defaults (active-by-default) ──────────────────────────────────
# The ON values the service falls back to when config.json omits the key (absent
# key → recommended ON). A user disables a single feature by writing an explicit
# {} or {"enabled": False} for it (present-but-off short-circuits each feature's
# runtime fallback: graduated → legacy cliff, recency → never skip, ramp → 1.0×).
# Keep these IN SYNC with the onboarding schema skeleton (schema.py acquisition).
DEFAULT_GRADUATED_CAP = {"enabled": True, "reference_minutes": 45, "base_cap": 6, "hard_cap": 24}
DEFAULT_RECENCY_GATE  = {"enabled": True, "cold_days": 90}
DEFAULT_BUDGET_RAMP   = {"enabled": True, "low_mult": 0.5, "high_mult": 1.5}


def last_watched_per_series(df):
    """The resume point per series: the row at the highest watched (season, episode).

    The sort key ``season*10_000 + episode`` orders episodes without per-season
    overflow; ``groupby.last()`` after sorting picks each series' furthest-watched
    row (carrying the columns the walk reads — series_title, keep_policy,
    last_watched_at, certification). Returns the (empty) watched frame unchanged
    when nothing is watched, so the caller can short-circuit on ``.empty``."""
    watched_mask = df["is_watched"].infer_objects(copy=False).fillna(False).astype(bool)
    watched = df[watched_mask].copy()
    if watched.empty:
        return watched
    watched["_sk"] = (
        pd.to_numeric(watched["season_number"],  errors="coerce").fillna(0).astype(int) * 10_000
        + pd.to_numeric(watched["episode_number"], errors="coerce").fillna(0).astype(int)
    )
    return (
        watched.sort_values("_sk")
        .groupby("series_id", sort=False)
        .last()
        .reset_index()
    )


def build_runtime_lookup(df) -> dict:
    """``(series_id, season, episode) -> runtime_seconds`` for every Parquet row with
    a positive runtime, letting the walk budget upcoming episodes without extra API
    calls. Empty dict when the ``runtime_seconds`` column is absent."""
    lookup: dict = {}
    if "runtime_seconds" not in df.columns:
        return lookup
    rt_num = pd.to_numeric(df["runtime_seconds"], errors="coerce")
    for idx in df.index:
        s  = df.at[idx, "series_id"]
        sn = df.at[idx, "season_number"]
        en = df.at[idx, "episode_number"]
        rt = rt_num.at[idx]
        if pd.notna(s) and pd.notna(sn) and pd.notna(en) and pd.notna(rt) and rt > 0:
            lookup[(int(s), int(sn), int(en))] = float(rt)
    return lookup


def episode_cap(series_runtime_s, *, short_episode_s, max_ep, graduated=None) -> float:
    """Per-series episode cap on the prefetch walk.

    DEFAULT (graduated falsy / disabled) — the legacy binary cliff: a short-episode
    series (runtime below ``short_episode_s``) is uncapped (``inf``); a normal-length
    series caps at ``max_ep``. This is the byte-identical baseline.

    GRADUATED (``graduated={"enabled": True, ...}``) — replaces the cliff with a smooth
    count cap that scales inversely with episode length: roughly ``base_cap`` episodes
    for a reference-length episode, more for shorter ones, clamped to ``hard_cap``. This
    removes the cliff's runaway (a 599 s estimate no longer grabs the whole library) and
    its discontinuity (599 s → inf vs 601 s → 6). The runtime budget in the walk's
    while-guard still bounds total runtime; this only bounds the episode COUNT.
    Knobs (with defaults): reference_minutes=45, base_cap=max_ep, hard_cap=24."""
    if not (graduated and graduated.get("enabled")):
        return max_ep if series_runtime_s >= short_episode_s else float("inf")
    rt = series_runtime_s if series_runtime_s and series_runtime_s > 0 else short_episode_s
    ref_s = float(graduated.get("reference_minutes", 45)) * 60.0
    base  = int(graduated.get("base_cap", max_ep))
    hard  = max(int(graduated.get("hard_cap", 24)), base)  # >= a long season; never below base
    cap   = round(ref_s / rt * base)
    return float(max(base, min(hard, cap)))


def order_series_by_recency(last_by_series):
    """Sort the resume frame most-recently-watched first (unparseable/missing dates
    last), stably and deterministically, so under the walk's wall-clock / prewarm
    caps the hottest series prefetch before cold ones get a chance to consume the
    budget. Pure — returns a new frame; a no-op (copy) when the date column is absent."""
    if "last_watched_at" not in last_by_series.columns:
        return last_by_series.copy()
    out = last_by_series.copy()
    out["_lw"] = pd.to_datetime(out["last_watched_at"], utc=True, errors="coerce")
    out = out.sort_values("_lw", ascending=False, na_position="last", kind="stable")
    return out.drop(columns=["_lw"]).reset_index(drop=True)


def is_cold_series(last_watched_at, now, *, cold_days, has_upcoming) -> bool:
    """True ⇒ skip this series' prefetch walk. A series is cold when it was last
    watched more than ``cold_days`` ago AND has no imminent airing.

    ``has_upcoming`` is the MANDATORY mid-season-break exemption: a show with an
    episode airing soon (Sonarr ``nextAiring`` set) is never cold, even if the last
    watch was long ago — the household is waiting for the season to continue.
    ``cold_days=None`` (the default/off state) and a missing/unparseable
    ``last_watched_at`` both return False (never skip)."""
    if has_upcoming or cold_days is None:
        return False
    dt = pd.to_datetime(last_watched_at, utc=True, errors="coerce")
    if pd.isna(dt):
        return False
    return (now - dt).days > cold_days


def series_budget_multiplier(percentile, ramp) -> float:
    """Multiplier on a series' prefetch runtime budget from its watchability_percentile.

    DEFAULT (``ramp`` falsy) — returns EXACTLY ``1.0`` for every input, so the budget is
    untouched and the baseline is byte-identical (``budget * 1.0 == budget`` in IEEE-754,
    so the walk's ``accumulated < budget`` boundary never shifts). A configured ramp
    linearly interpolates ``low_mult``..``high_mult`` across percentile 0..100 (clamped),
    routing more buffer to the series the household is most likely to watch. A null / NaN
    / non-numeric / absent percentile returns 1.0 — the percentile is stale-by-one-run
    (refresh_scores runs after the sync that prefetches) and absent on a first sync, so a
    neutral multiplier is the safe fallback.

    OFF when ``ramp`` is falsy ({}) OR ``ramp["enabled"]`` is not truthy — the same
    ``{enabled: bool}`` contract graduated_cap and recency_gate use."""
    if not (ramp and ramp.get("enabled")):
        return 1.0
    if percentile is None:
        return 1.0
    try:
        p = float(percentile)
    except (TypeError, ValueError):
        return 1.0
    if math.isnan(p):
        return 1.0
    lo = float(ramp.get("low_mult", 1.0))
    hi = float(ramp.get("high_mult", 1.0))
    p = max(0.0, min(100.0, p))
    return lo + (hi - lo) * (p / 100.0)
