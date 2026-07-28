"""lifecycle/grace_policy.py — grace-period marking (pure).
==============================================================================
The pure decision slices of the grace-period marking shared by
``radarr/cache/movie_files.apply_grace_period`` and
``sonarr/cache/episode_files._apply_grace_period`` (ML Step 8). A watched file is
kept ``available_until = last_watched + grace`` and ``marked_for_deletion`` once the
window passes — UNLESS a guard protects it. The per-row df reads/writes and the
guard precomputes (pilot/franchise file-id sets, keep policy, latest-season) stay in
the service; only the side-effect-free decisions live here: the per-row precedence
and the grace-window computation.

NOTE: grace marking is the one Step-8 method that writes the dry-run ``plan_summary``
oracle (marked_for_deletion / available_until), so this extraction is byte-identical.

PURE — pandas + stdlib only; no HTTP, no global_cache, no df writes.

Public API:
  * grace_mark(anchor, grace_td, now) -> (available_until_iso | None, marked | None)
        the window: parse the anchor watch time, return (iso, now >= until); (None,
        None) when the anchor can't be parsed (the caller leaves the row unchanged).
  * movie_grace_decision(...) -> 'clear' | 'skip' | 'mark'   (Radarr)
  * episode_grace_decision(...) -> 'clear' | 'skip' | 'mark' (Sonarr)
        the guard precedence; 'clear' forces marked_for_deletion=False, 'skip' leaves
        the row untouched, 'mark' runs grace_mark.
"""
from __future__ import annotations

import pandas as pd


def grace_window_multiplier(percentile, ramp) -> float:
    """Multiplier on a row's grace window from its ``watchability_percentile`` —
    favourites keep their file longer, forgettables shorter.

    DEFAULT (``ramp`` falsy or ``enabled`` not set) -> EXACTLY ``1.0`` so
    ``grace_td * 1.0 == grace_td`` and the marking is byte-identical. Configured ->
    ``low_mult``..``high_mult`` interpolated across percentile 0..100 (clamped); a
    null / NaN / non-numeric / absent percentile -> 1.0 (neutral)."""
    if not (ramp and ramp.get("enabled")):
        return 1.0
    try:
        p = float(percentile)
    except (TypeError, ValueError):
        return 1.0
    if pd.isna(p):
        return 1.0
    lo = float(ramp.get("low_mult", 1.0))
    hi = float(ramp.get("high_mult", 1.0))
    p = max(0.0, min(100.0, p))
    return lo + (hi - lo) * (p / 100.0)


def grace_mark(anchor, grace_td, now):
    """Grace window for a row whose countdown starts at ``anchor`` (a watch
    timestamp). Returns ``(available_until_iso, now >= until)``; ``(None, None)`` if
    the anchor can't be parsed, so the caller leaves available_until/marked as-is
    (mirroring the original try/except: pass)."""
    try:
        until = pd.to_datetime(anchor, utc=True) + grace_td
        if pd.isna(until):              # anchor=None yields NaT (no raise) on newer pandas
            return None, None
        return until.isoformat(), (now >= until)
    except Exception:
        return None, None


def movie_grace_decision(*, is_franchise_entry, fid_franchise_protected,
                         keep_protected, is_watched, has_last_watched) -> str:
    """Radarr per-row precedence. 'clear' (never mark): a franchise entry, a
    franchise-protected file, a keep_forever/keep_movie/universe policy, or a row
    that is NOT WATCHED. 'skip' (leave as-is): watched but no last-watched
    timestamp. Else 'mark'.

    ``not is_watched`` RETURNS 'clear', NOT 'skip'. Grace marking is the only thing
    that sets ``marked_for_deletion``, so an unwatched row carrying that flag is an
    impossible state — and 'skip' would preserve it forever. That was inert while
    ``is_watched`` could only ever go False→True; under the global watched bar
    (``lifecycle.watched_definition``) a row whose only plays were samples flips
    True→False, and 'skip' would have left the previous run's mark standing and
    deleted a file the current definition says was never watched. On rows that were
    never marked this is a no-op (False → False)."""
    if is_franchise_entry:
        return "clear"
    if fid_franchise_protected:
        return "clear"
    if keep_protected:
        return "clear"
    if not is_watched:
        return "clear"
    if not has_last_watched:
        return "skip"
    return "mark"


def episode_grace_decision(*, is_pilot, is_next, is_watched, has_last_watched,
                           fid_protected, keep_series, keep_season_current,
                           recent_aired, household_blocked, viewer_protected=False) -> str:
    """Sonarr per-row precedence. 'clear' (never mark): a pilot/next-episode row, a
    row that is NOT WATCHED, an episode inside a viewer's retention interval, a
    protected pilot file, a keep_series series, the current keep_season, a
    recently-aired episode, or a household member who hasn't finished. 'skip' (leave
    as-is): watched but no last-watched timestamp. Else 'mark'.

    Order matches the service guards exactly: pilot/next first (they're cleared even
    when unwatched), then the watched/last-watched legs, then the remaining clears.

    ``not is_watched`` RETURNS 'clear', NOT 'skip' — see the twin note in
    :func:`movie_grace_decision`. Under the global watched bar an episode whose only
    plays were sub-threshold samples flips True→False, and 'skip' would have left a
    previous run's ``marked_for_deletion`` standing. Five live rows are in exactly
    that state (Fallout S01E06, See S01E06, DuckTales S01E01, Blue Bloods S01E11/E12);
    without this they would be deleted on the first pass after the change, on a mark
    the current definition does not support. A no-op on rows that were never marked.

    ``viewer_protected`` is the PER-VIEWER RETENTION guard
    (``lifecycle.viewer_retention``): the episode sits inside some account's
    [position − backward_buffer, position + pace × horizon] interval. It is
    evaluated first among the post-skip clears because it is the guard an operator
    most often wants to see attributed in the log ("held for Aiden / Raina"); the
    ordering is cosmetic — every branch below returns the same 'clear'. Default
    False keeps every existing caller byte-identical."""
    if is_pilot or is_next:
        return "clear"
    if not is_watched:
        return "clear"
    if not has_last_watched:
        return "skip"
    if viewer_protected:
        return "clear"
    if fid_protected:
        return "clear"
    if keep_series:
        return "clear"
    if keep_season_current:
        return "clear"
    if recent_aired:
        return "clear"
    if household_blocked:
        return "clear"
    return "mark"
