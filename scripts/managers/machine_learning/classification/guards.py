"""
classification/guards.py — whole-file delete guards (pure).
================================================================================
Relocated from ``sonarr/cache/episode_files._build_protected_file_ids`` (ML Step
5b). CRITICAL: this is the whole-file-delete footgun guard (see MEMORY). PURE — a
pandas-only computation over the episode_files frame; no HTTP, no global_cache.
The service resolves the two inputs it owns (the pilot file-id set via
``_build_pilot_file_ids`` and the ``RECENT_AIR_DAYS`` constant) and delegates here.

Public API:
  * build_protected_file_ids(df, now, pilot_file_ids, *, recent_air_days) -> frozenset
  * build_pilot_file_ids(df) -> frozenset   (parquet-backed: real + de-facto pilots)
"""
from __future__ import annotations

import pandas as pd


def build_protected_file_ids(df, now, pilot_file_ids, *, recent_air_days) -> "frozenset":
    """Return the frozenset of ``episode_file_id`` values that must NEVER be deleted
    because ANY episode row backed by that file hits a protective guard.

    Whole-file protection: a single physical Sonarr file can back several episode
    rows (multi-episode files share one ``episodeFileId``). The per-row delete
    guards only inspect the current row, so a watched/grace-expired episode could
    DELETE the file and silently destroy a guarded SIBLING. This collapses every
    guard down to the set of file ids touched, so if any row sharing a file id is
    guarded the whole file id is protected.

    Guards (mirroring ``_apply_grace_period`` + the per-row delete guards):
      * pilot       — ``pilot_file_ids`` (real + de-facto pilots; built by service).
      * keep_series — every file id on a ``keep_series`` row.
      * keep_season — file ids on rows in the latest non-special season of a
                      ``keep_season`` series.
      * recent-air  — file ids on rows that aired within ``recent_air_days``.
      * household   — file ids on rows where ``all_household_watched`` is present
                      AND falsy (a household member still hasn't watched).
    """
    if "episode_file_id" not in df.columns:
        return frozenset()

    protected: set = set()

    def _add_fids(mask: "pd.Series | None") -> None:
        if mask is None or not mask.any():
            return
        for f in df.loc[mask, "episode_file_id"].dropna():
            protected.add(int(f))

    # ── Pilots (real + de-facto) — passed in by the service ──────────────────
    for f in (pilot_file_ids or ()):
        if pd.notna(f):
            protected.add(int(f))

    # ── Keep-policy guards ───────────────────────────────────────────────────
    if "keep_policy" in df.columns:
        _add_fids(df["keep_policy"] == "keep_series")

        keep_season_mask = df["keep_policy"] == "keep_season"
        if keep_season_mask.any():
            _sid_num = pd.to_numeric(df["series_id"], errors="coerce")
            _sn_num  = pd.to_numeric(df["season_number"], errors="coerce")
            keep_season_sids = set(
                _sid_num[keep_season_mask].dropna().astype(int).unique()
            )
            for sid in keep_season_sids:
                non_special = _sn_num[(_sid_num == sid) & (_sn_num > 0)].dropna()
                if non_special.empty:
                    continue
                latest = int(non_special.max())
                _add_fids(
                    keep_season_mask & (_sid_num == sid) & (_sn_num >= latest)
                )

    # ── Recently-aired guard ─────────────────────────────────────────────────
    if "air_date_utc" in df.columns:
        air = pd.to_datetime(df["air_date_utc"], utc=True, errors="coerce")
        # (now - air) is a Timedelta Series; .dt.days floors to whole days,
        # matching the per-row guard's `(now - air).days`.
        days_since = (now - air).dt.days
        _add_fids(air.notna() & (days_since < recent_air_days))

    # ── Household watch guard ────────────────────────────────────────────────
    if "all_household_watched" in df.columns:
        ahw = df["all_household_watched"]
        # present (not NaN) AND falsy → a household member still hasn't watched;
        # NaN = legacy row / no household config → not a guard.
        _add_fids(ahw.notna() & ~ahw.astype(bool))

    return frozenset(protected)


def build_pilot_file_ids(df) -> "frozenset":
    """Return the frozenset of ``episode_file_id`` values that back a real OR
    de-facto pilot and must never be deleted. Pure pandas; fed into
    ``build_protected_file_ids`` as the pilot guard.

    Two categories:
      1. Real pilot rows — ``is_pilot`` True AND ``episode_file_id`` not NaN.
      2. De-facto pilots — for a series with only a stub pilot (``is_pilot`` True,
         ``episode_file_id`` None) or no pilot row, the earliest WATCHED non-pilot
         episode's file id (bridges the gap before the pilot batch resolves a real
         pilot file).
    """
    pilot_file_ids: set = set()

    if "is_pilot" not in df.columns or "episode_file_id" not in df.columns:
        return frozenset()

    _pilot_mask = (
        df["is_pilot"].infer_objects(copy=False).fillna(False).astype(bool)
    )

    # 1. Real pilot file IDs
    real_pilot_mask = _pilot_mask & df["episode_file_id"].notna()
    pilot_file_ids.update(df.loc[real_pilot_mask, "episode_file_id"].dropna())
    real_pilot_sids: "set[int]" = set(
        df.loc[real_pilot_mask, "series_id"].dropna().astype(int)
    )

    # 2. De-facto pilot for series without a resolved pilot file
    if "is_watched" not in df.columns:
        return frozenset(pilot_file_ids)

    watched_mask = (
        df["is_watched"].infer_objects(copy=False).fillna(False).astype(bool)
    )
    candidate_mask = watched_mask & ~_pilot_mask
    if not candidate_mask.any():
        return frozenset(pilot_file_ids)

    candidates = df[candidate_mask]
    _sid = pd.to_numeric(candidates["series_id"],   errors="coerce").fillna(-1).astype(int)
    _sn  = pd.to_numeric(candidates["season_number"], errors="coerce").fillna(9_999)
    _en  = pd.to_numeric(candidates["episode_number"], errors="coerce").fillna(9_999)
    sort_key = _sn * 10_000 + _en

    for sid_val in _sid.unique():
        if sid_val < 0 or int(sid_val) in real_pilot_sids:
            continue
        cand_idx = candidates.index[_sid == sid_val]
        if len(cand_idx) == 0:
            continue
        earliest_idx = sort_key.loc[cand_idx].idxmin()
        fid = df.at[earliest_idx, "episode_file_id"]
        if pd.notna(fid):
            pilot_file_ids.add(fid)

    return frozenset(pilot_file_ids)
