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
  * build_protected_file_reasons(df, now, pilot_file_ids, *, recent_air_days)
      -> dict[str, frozenset]   (GLD-ACQ-22: per-guard breakdown; the flat set above is
      the union of these values, derived from the SAME masks so attribution can never
      disagree with the guard)
  * build_pilot_file_ids(df) -> frozenset   (parquet-backed: real + de-facto pilots)
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.space.downgrade_planner import UNIVERSE_PROTECT_MIN


def build_protected_file_ids(df, now, pilot_file_ids, *, recent_air_days) -> "frozenset":
    """Return the frozenset of ``episode_file_id`` values that must NEVER be deleted
    because ANY episode row backed by that file hits a protective guard. Thin union over
    :func:`build_protected_file_reasons` — ONE mask source (GLD-ACQ-22), so the flat set
    and the per-guard attribution cannot disagree."""
    reasons = build_protected_file_reasons(
        df, now, pilot_file_ids, recent_air_days=recent_air_days)
    return frozenset().union(*reasons.values()) if reasons else frozenset()


def build_protected_file_reasons(
    df, now, pilot_file_ids, *, recent_air_days
) -> "dict[str, frozenset]":
    """GLD-ACQ-22 — ``{guard_name: frozenset(episode_file_id)}`` for every protective
    guard, from the same masks the flat set unions. Guard names: ``pilot``,
    ``keep_series``, ``keep_season``, ``recent_air``, ``household``, ``retention``,
    ``watchlist``, ``universe``. A fid may appear under several guards (overlap is
    real and reported as-is); absent columns simply yield empty sets.

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
                      AND falsy AND an active watcher is still approaching the
                      episode (row ``retention_hold``; GLD-ACQ-18 — active
                      watchers only, decision 2026-08-06).
      * retention   — file ids on rows inside SOME viewer's retention interval
                      (``retention_hold``; see lifecycle.viewer_retention). This is
                      the whole-file half of the per-viewer rule: a multi-episode
                      file backing one held episode must not be destroyed by a
                      sibling row that fell outside every interval.
      * watchlist   — file ids on rows whose SERIES is watchlisted by a still-active
                      household member (``watchlist_hold``; see
                      episode_files._apply_watchlist_shield). Self-releasing on
                      watchlister dormancy, so it can never hold disk forever.
      * universe    — file ids of any series whose recency-decayed borrowed franchise/
                      universe credit reaches ``UNIVERSE_PROTECT_MIN`` (a HOT saga resists
                      DELETION just as ``plan_series_downgrades`` makes it resist a step-down;
                      byte-identical when the column is absent / cold, i.e. 0.0 < the floor).
    """
    reasons: "dict[str, set]" = {
        "pilot": set(), "keep_series": set(), "keep_season": set(),
        "recent_air": set(), "household": set(), "retention": set(),
        "watchlist": set(), "universe": set(),
    }
    if "episode_file_id" not in df.columns:
        return {k: frozenset(v) for k, v in reasons.items()}

    def _add_fids(name: str, mask: "pd.Series | None") -> None:
        if mask is None or not mask.any():
            return
        for f in df.loc[mask, "episode_file_id"].dropna():
            reasons[name].add(int(f))

    # ── Pilots (real + de-facto) — passed in by the service ──────────────────
    for f in (pilot_file_ids or ()):
        if pd.notna(f):
            reasons["pilot"].add(int(f))

    # ── Keep-policy guards ───────────────────────────────────────────────────
    if "keep_policy" in df.columns:
        _add_fids("keep_series", df["keep_policy"] == "keep_series")

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
                    "keep_season",
                    keep_season_mask & (_sid_num == sid) & (_sn_num >= latest),
                )

    # ── Recently-aired guard ─────────────────────────────────────────────────
    if "air_date_utc" in df.columns:
        air = pd.to_datetime(df["air_date_utc"], utc=True, errors="coerce")
        # (now - air) is a Timedelta Series; .dt.days floors to whole days,
        # matching the per-row guard's `(now - air).days`.
        days_since = (now - air).dt.days
        _add_fids("recent_air", air.notna() & (days_since < recent_air_days))

    # ── Household watch guard ────────────────────────────────────────────────
    # ACTIVE WATCHERS ONLY (GLD-ACQ-18, decision 2026-08-06): "not all household
    # watched" holds a file ONLY while a member who is actively watching the series
    # will come upon the episode reasonably soon. That is precisely retention_hold's
    # window (per-account [position − back, position + pace × horizon]; dormant
    # accounts get no forward reach), so this guard INTERSECTS with it rather than
    # growing a second definition of "approaching" — the watched-bar lesson (§8 P-C,
    # six divergent definitions) applied prospectively. The column keeps its
    # all-members meaning and is still computed every sync; only guard USAGE narrows.
    # The old form (present & falsy alone) was near-unsatisfiable with six members —
    # 571 all-watched of 12,637 rows — and froze 4,812 fids, which is what emptied
    # the leapfrog recycle's pool for every actively-watched series (GLD-ACQ-21's
    # first reason lines). By construction this branch now adds no fid the retention
    # guard below hasn't already added; it stays for legibility and for the delete
    # pass's named per-row skip. NaN ahw = legacy/no-household → not a guard; absent
    # retention column ⇒ no active-watcher data ⇒ no household hold.
    if "all_household_watched" in df.columns and "retention_hold" in df.columns:
        ahw = df["all_household_watched"]
        _rh_hh = df["retention_hold"]
        _add_fids("household", ahw.notna() & ~ahw.astype(bool)
                  & _rh_hh.notna() & _rh_hh.astype(bool))

    # ── Per-viewer retention guard ───────────────────────────────────────────
    # ``retention_hold`` is the per-run verdict stamped by
    # ``episode_files._apply_viewer_retention``: True ⇒ the episode is inside some
    # ACCOUNT's [position − backward_buffer, position + pace × horizon] window.
    # Collapsed to file ids here so a multi-episode file backing a held episode is
    # protected whole — the same footgun the pilot/keep guards above close.
    # Byte-identical when the column is absent (a pre-retention parquet) or all
    # falsy (the rule disabled).
    if "retention_hold" in df.columns:
        _rh = df["retention_hold"]
        _add_fids("retention", _rh.notna() & _rh.astype(bool))

    # ── Watchlist intent guard (GROUP A5) ────────────────────────────────────
    # ``watchlist_hold`` is the per-run verdict stamped by
    # ``episode_files._apply_watchlist_shield``: True ⇒ somebody in this household put the
    # SERIES on a watchlist and that member is still an active viewer. Robert's decision:
    # a watchlisted title is shielded from deletion, not merely scored higher — A5's points
    # alone cannot lift a weak-taste series over the delete ceiling, and deleting something
    # the household explicitly asked for is the one deletion that is never defensible.
    # Collapsed to file ids for the same whole-file reason as every guard above. The hold
    # SELF-RELEASES once the watchlister goes dormant (see intent_hold_active), so this can
    # never hold disk forever. Byte-identical when the column is absent (a pre-shield
    # parquet) or all falsy (the term disabled / nothing watchlisted).
    if "watchlist_hold" in df.columns:
        _wh = df["watchlist_hold"]
        _add_fids("watchlist", _wh.notna() & _wh.astype(bool))

    # ── Hot franchise/universe credit guard ──────────────────────────────────
    # A hot saga (recency-decayed borrowed credit, broadcast per-series onto every
    # episode row by _apply_universe_credit) resists DELETION just as
    # plan_series_downgrades makes it resist a quality step-down — deletion must not be
    # more aggressive than the downgrade it already survives. Any series whose credit
    # reaches the floor protects all of its on-disk files; as the saga goes stale the
    # credit decays below the floor and the files become deletable again. Byte-identical
    # when the column is absent or cold (0.0 everywhere < UNIVERSE_PROTECT_MIN).
    if "universe_credit" in df.columns:
        _uc = pd.to_numeric(df["universe_credit"], errors="coerce")
        _add_fids("universe", _uc.notna() & (_uc >= UNIVERSE_PROTECT_MIN))

    return {k: frozenset(v) for k, v in reasons.items()}


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
