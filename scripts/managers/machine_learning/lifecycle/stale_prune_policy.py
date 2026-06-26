"""lifecycle/stale_prune_policy.py — owned-movie stale prune (pure).
==============================================================================
The pure decision slices of ``radarr/repair/anomaly.demote_stale_monitored`` (ML
Step 8). Owned movies whose watchability stays below a floor are unmonitored after
one dwell, then their file is deleted after a longer dwell (the deletion is tracked
and restorable). The scoring (Trakt credits + ``score_movie``), the per-movie
clock in global_cache, and the movie/editor PUT / moviefile DELETE are I/O — so
only the side-effect-free policy lives here: the pressure→dwell expedite curve, the
clock-age parse, and the per-movie action.

PURE — stdlib only; no HTTP, no global_cache, no service imports.

Public API:
  * expedite_dwell(free_gb, T, U, delete_days, min_delete_days) -> (eff_delete_days, pressure_active)
        the space-pressure gate: with a floor set, shorten the delete dwell as free
        space falls from U toward T; with no floor it's the legacy always-active dwell.
  * clock_age(since_iso, now) -> (since_iso, age_days)
        parse the per-movie "below-floor since" clock; a malformed value resets to now.
  * restore_cooldown_active(deletion_iso, now, min_age_days) -> bool
        whether a deleted title is still inside its re-grab cooldown (block restore until
        min_age_days have elapsed since deletion, independent of score). Default-off
        (<=0 -> never blocks); shared by the Radarr + Sonarr restore passes.
  * prune_score_gate(score, has_credits, floor) -> 'defer' | 'error' | 'recovered' | 'below_floor'
        the pre-dwell gate from the watchability score.
  * prune_below_floor_action(*, age_days, delete_enabled, delete_active, has_fid,
        eff_delete_days, pressure_active, unmonitor_days, monitored) -> 'delete' | 'unmonitor' | 'age'
        what to do with a sustained below-floor movie this run.
  * franchise_delete_exempt(*, collection_tmdb_id, sibling_tmdb_ids, watched_tmdb_ids,
        movie_tmdb_id, threshold, enabled) -> bool
        spare a below-floor movie from DELETION when its collection is substantially watched.
  * budget_delete_cohort(candidates, *, need_gb, enabled) -> list
        rank the dwell-passed delete cohort (worst score, biggest file first) and keep only
        enough to reclaim need_gb — so a deep stale backlog isn't wiped in one pass.
"""
from __future__ import annotations

from datetime import datetime, timezone


def expedite_dwell(free_gb, T, U, delete_days, min_delete_days):
    """Effective delete dwell + whether the prune acts this run.

    With a floor configured (T > 0) and a known free space, the prune only ACTS in
    the pressure band (free < U), and the delete dwell shortens linearly from
    ``delete_days`` (at/above U) down to ``min_delete_days`` (at/below T). With no
    floor (T <= 0) or unknown free space, it's the legacy always-active full dwell."""
    eff = delete_days
    if T > 0 and free_gb != float("inf"):
        pressure = free_gb < U
        if min_delete_days < delete_days:
            p = 0.0 if free_gb >= U else (1.0 if free_gb <= T else (U - free_gb) / (U - T))
            eff = max(min_delete_days, min(delete_days,
                int(round(min_delete_days + (1.0 - p) * (delete_days - min_delete_days)))))
    else:
        pressure = True
    return eff, pressure


def clock_age(since_iso, now):
    """Parse a 'continuously below floor since' ISO timestamp and return
    ``(since_iso, age_days)``. A naive timestamp is treated as UTC; a malformed one
    resets the clock to ``now`` (and the returned since_iso reflects that reset)."""
    try:
        since_dt = datetime.fromisoformat(since_iso)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        since_dt, since_iso = now, now.isoformat()
    return since_iso, (now - since_dt).days


def restore_cooldown_active(deletion_iso, now, min_age_days) -> bool:
    """Whether a deleted title is still inside its re-grab cooldown — restore is
    BLOCKED until ``min_age_days`` have elapsed since the deletion timestamp,
    independent of how far its watchability score has recovered. Stops delete/
    re-grab thrash when a title's score hovers right at the shared demote/restore
    floor (deleted one run, restored the next, repeatedly).

    DEFAULT (``min_age_days`` <= 0, None, or unparseable) -> False (never blocks), so
    the restore pass stays byte-identical to the pure score gate. A naive
    ``deletion_iso`` is treated as UTC; a blank / malformed timestamp -> False
    (fail-open — restore rather than wedge an entry forever on a bad clock, mirroring
    ``clock_age``'s reset-on-garbage). The boundary is inclusive of the cooldown end:
    at exactly ``min_age_days`` elapsed the title is restorable (active iff
    ``age_days < min_age_days``). Post-deletion counterpart to the below-floor clock
    in :func:`clock_age`; shared by the Radarr (``restore_recovered_deletions``) and
    Sonarr (``restore_recovered_episode_deletions``) restore passes."""
    try:
        days = int(min_age_days)
    except (TypeError, ValueError):
        return False
    if days <= 0:
        return False
    try:
        del_dt = datetime.fromisoformat(deletion_iso)
        if del_dt.tzinfo is None:
            del_dt = del_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return (now - del_dt).days < days


def prune_score_gate(score, has_credits, floor) -> str:
    """Pre-dwell gate from the watchability score:
      * 'defer'       — credits not fetched yet; affinity unknown, take no action.
      * 'error'       — scoring error sentinel (score < 0); skip this run.
      * 'recovered'   — score back at/above the floor; drop the clock (reset dwell).
      * 'below_floor' — sustained low; proceed to the dwell/age decision.
    """
    if not has_credits:
        return "defer"
    if score < 0:
        return "error"
    if score >= floor:
        return "recovered"
    return "below_floor"


def prune_below_floor_action(*, age_days, delete_enabled, delete_active, has_fid,
                             eff_delete_days, pressure_active, unmonitor_days, monitored) -> str:
    """What to do with a sustained below-floor movie this run:
      * 'delete'    — deletion enabled+active, dwell reached, and a file exists.
      * 'unmonitor' — under pressure, unmonitor dwell reached, still monitored.
      * 'age'       — keep the clock advancing (comfortable space, or dwell not yet met).
    """
    if delete_enabled and delete_active and age_days >= eff_delete_days and has_fid:
        return "delete"
    if pressure_active and age_days >= unmonitor_days and monitored:
        return "unmonitor"
    return "age"


def franchise_delete_exempt(*, collection_tmdb_id, sibling_tmdb_ids, watched_tmdb_ids,
                            movie_tmdb_id, threshold, enabled) -> bool:
    """Spare a below-floor movie from DELETION (not from unmonitor/age) when it belongs to a
    collection/franchise the household has substantially watched — a low-scoring entry in a
    set you're actively working through (e.g. mid-franchise sequel) shouldn't be culled out
    from under you. The signal is the RAW watched fraction of its siblings (watched_others /
    others), NOT an affinity score.

    DEFAULT (``enabled`` False, no collection, or ``threshold`` not positive) -> False — no
    exemption, byte-identical. Exempt iff fraction(siblings watched) >= threshold."""
    if not enabled or not collection_tmdb_id or threshold is None or threshold <= 0:
        return False
    others = set(sibling_tmdb_ids) - ({movie_tmdb_id} if movie_tmdb_id else set())
    if not others:
        return False
    watched = len(others & set(watched_tmdb_ids))
    return (watched / len(others)) >= threshold


def budget_delete_cohort(candidates, *, need_gb, enabled) -> list:
    """Pick which of the dwell-passed delete ``candidates`` to actually delete this run.
    Each candidate is a dict carrying at least ``score`` and ``size_gb``.

    DEFAULT (``enabled`` False, or ``need_gb`` None / <= 0 / infinite — e.g. free space
    unknown) -> ALL candidates in their original order, byte-identical to deleting the whole
    cohort. Enabled with a finite positive ``need_gb`` -> rank worst-score-then-biggest-file
    first and accumulate until the projected reclaim reaches ``need_gb``, so only enough is
    deleted to relieve pressure back to the band top U; the rest keep ageing for next pass."""
    cands = list(candidates)
    if not enabled or need_gb is None or need_gb <= 0 or need_gb == float("inf"):
        return cands
    ordered = sorted(cands, key=lambda c: (c.get("score", 0), -float(c.get("size_gb") or 0.0)))
    selected, reclaimed = [], 0.0
    for c in ordered:
        if reclaimed >= need_gb:
            break
        selected.append(c)
        reclaimed += float(c.get("size_gb") or 0.0)
    return selected
