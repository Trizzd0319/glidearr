"""acquisition/pilot_stepping.py — pilot quality-ladder stepping (pure).
==============================================================================
The pure decision slices of ``sonarr/cache/episode_files.run_pilot_search`` (ML Step
8). There are TWO pilot strategies, selected by the service's ``pilot_best_tier_first``
flag:

  * LEGACY (default-off flag): FLOOR-FIRST + STEP-UP. A stub pilot (S01E01 with no file)
    is searched at the floor profile first; each run whose prior attempt at the current
    profile still found nothing climbs ONE tier up the resolution ladder toward the
    widest ("Any"), then keeps re-searching there. Cores: ``rank_pilot_profiles`` +
    ``next_pilot_profile`` (+ ``pilot_search_due`` / ``pilot_backoff_interval`` cadence).
  * BEST-TIER-FIRST (flag on): the pilot earns the HIGHEST tier whose estimated grab still
    keeps the disk reserve (SPACE-gated, NOT watch-likelihood-gated), then DIVERTS DOWN the
    ladder for availability. Cores: ``choose_pilot_profile`` + ``pilot_step_down_pids``.
    The re-probe cadence funcs (``pilot_search_due`` / ``pilot_backoff_interval``) are shared
    by both strategies — they decide WHEN to (re)search, not WHICH tier.

The method is heavily I/O-interleaved (qualityprofile fetch, per-stub series GET/PUT,
batched EpisodeSearch, df writes), so — as with jit_planner — only the side-effect-free
slices live here; the service keeps the fetch + the stateful search/apply loop.

PURE — pandas + the sizing brain only; no HTTP, no global_cache, no df writes.

Public API:
  * profile_max_resolution(profile) -> int
        highest allowed resolution in a profile (incl. nested grouped items) — the
        key that ranks profiles floor-first.
  * rank_pilot_profiles(raw_profiles) -> list
        profiles sorted ascending by max resolution (rank 0 = floor, rank -1 = widest).
  * pilot_search_due(last_searched, now, interval) -> bool
        whether a stub is due for a (re)search (never searched / interval elapsed).
  * pilot_backoff_interval(base_interval, attempts_done, *, backoff) -> timedelta
        the effective re-search interval: exponential backoff in attempts, then a long
        re-probe cooldown once exhausted (None/off -> base_interval, byte-identical).
  * next_pilot_profile(*, attempts_done, current_pid, current_rank, last_pid, ranked,
                       max_rank=None)
        the LEGACY ladder step -> (new_pid, action) where action is
        'floor' | 'step_up' | 'at_ceiling' | 'hold'. ``max_rank`` caps how high a
        low-likelihood stub may climb (None -> uncapped, byte-identical).
  * choose_pilot_profile(best_first, *, projected_free, reserve_gb, runtime_min, measured)
        BEST-TIER-FIRST pick — the highest-resolution profile whose estimated grab keeps the
        reserve, with NO likelihood cap (a pilot earns max tier on space alone). None when even
        the lowest profile would breach the reserve (the SERVICE owns forced-floor vs skip).
  * next_pilot_profile_descend(*, start_rank, current_pid, current_rank, last_pid, ranked)
        the best-tier-first ladder step -> (new_pid, action) with action
        'target' | 'step_down' | 'at_floor'. Targets the best-that-fits-space ceiling
        (``start_rank``), and descends ONE rung per empty run (availability divert), never above
        the ceiling and never out of the search (a pilot is never abandoned).
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.sizing.size_model import estimate_gb_for_profile


def profile_max_resolution(profile) -> int:
    """Highest allowed resolution in a Sonarr quality profile, including nested
    grouped items — the key that ranks profiles from most-permissive (lowest) to
    widest. 0 when nothing is allowed / no resolution is set."""
    best = 0
    for item in (profile.get("items") or []):
        if not item.get("allowed"):
            continue
        res = (item.get("quality") or {}).get("resolution", 0)
        if isinstance(res, (int, float)):
            best = max(best, int(res))
        for sub in (item.get("items") or []):
            if sub.get("allowed"):
                sr = (sub.get("quality") or {}).get("resolution", 0)
                if isinstance(sr, (int, float)):
                    best = max(best, int(sr))
    return best


def rank_pilot_profiles(raw_profiles) -> list:
    """Profiles sorted ascending by max resolution: rank 0 = the floor (most
    permissive — grabs almost anything), rank -1 = the widest ('Any')."""
    return sorted(raw_profiles, key=profile_max_resolution)


def pilot_search_due(last_searched, now, interval) -> bool:
    """Whether a stub pilot is due for a (re)search: True when it has never been
    searched, or its last attempt was at least ``interval`` ago. A blank / NaT /
    unparseable timestamp counts as due (we'd rather re-search than stall)."""
    if not last_searched or pd.isna(last_searched):
        return True
    try:
        return (now - pd.to_datetime(last_searched, utc=True)) >= interval
    except Exception:
        return True


def pilot_backoff_interval(base_interval, attempts_done, *, backoff=None):
    """Effective re-search interval for a stub given how many times it has already been
    searched. DEFAULT (``backoff`` falsy / ``enabled`` not set) -> ``base_interval``
    unchanged, so the due check is byte-identical.

    Enabled, the interval grows exponentially with ``attempts_done`` —
    ``base_interval * (base ** min(attempts_done, cap_attempts))`` — so a stub that keeps
    coming up empty is retried less and less often instead of every interval (a library of
    permanently-unavailable pilots stops hammering the indexers each cycle). At
    ``attempts_done == 0`` the exponent is 0 so the first pass still uses exactly
    ``base_interval``.

    Once ``attempts_done >= exhausted_after`` the stub is treated as EXHAUSTED and put on a
    long ``reprobe_multiplier`` cooldown — but the interval stays FINITE, so a release that
    finally shows up is still picked up on the next probe (re-probeable), never permanently
    abandoned. ``base``/``reprobe_multiplier`` are clamped to >= 1.0 so backoff only ever
    lengthens the interval, never shortens it."""
    if not (backoff and backoff.get("enabled")):
        return base_interval
    try:
        att = max(0, int(attempts_done))
    except (TypeError, ValueError):
        att = 0
    exhausted_after = backoff.get("exhausted_after")
    if exhausted_after is not None:
        try:
            if att >= int(exhausted_after):
                mult = float(backoff.get("reprobe_multiplier", 1.0) or 1.0)
                return base_interval * max(1.0, mult)
        except (TypeError, ValueError):
            pass
    try:
        base = float(backoff.get("base", 2.0) or 2.0)
    except (TypeError, ValueError):
        base = 2.0
    try:
        cap = max(0, int(backoff.get("cap_attempts", 6)))
    except (TypeError, ValueError):
        cap = 6
    return base_interval * (max(1.0, base) ** min(att, cap))


def next_pilot_profile(*, attempts_done, current_pid, current_rank, last_pid, ranked,
                       max_rank=None):
    """The quality-ladder step for a stub pilot's next search. Returns
    ``(new_pid, action)``:

      * 'floor'      — first attempt (attempts_done == 0): target the floor (rank 0,
        most permissive). The service applies it only when new_pid != current_pid.
      * 'step_up'    — a prior attempt at the CURRENT profile found nothing
        (last_pid == current_pid): climb one tier (rank+1) toward the widest.
      * 'at_ceiling' — already at the widest profile (rank+1 out of range), or the next
        tier would exceed ``max_rank``: hold and keep re-searching here.
      * 'hold'       — the profile changed outside our stepping (last_pid !=
        current_pid): leave it and just search at the current profile.

    ``max_rank`` (default None -> uncapped, byte-identical) caps the climb so a
    low-watch-likelihood stub stops at the resolution its propensity earns instead of
    escalating a never-watched series all the way to the widest 'Any' net."""
    if attempts_done == 0:
        return ranked[0]["id"], "floor"
    if last_pid is not None and last_pid == current_pid:
        new_rank = current_rank + 1
        if max_rank is not None and new_rank > max_rank:
            return current_pid, "at_ceiling"
        if new_rank < len(ranked):
            return ranked[new_rank]["id"], "step_up"
        return current_pid, "at_ceiling"
    return current_pid, "hold"


def choose_pilot_profile(best_first, *, projected_free, reserve_gb, runtime_min, measured):
    """Best-tier-first pick for a stub pilot: the highest-resolution profile in ``best_first``
    (ordered high-res first) whose estimated single-episode grab keeps ``projected_free`` at or
    above ``reserve_gb``. Returns None when even the lowest profile would breach the reserve.

    Mirrors :func:`space.jit_planner.choose_jit_profile` BUT with NO watch-likelihood / pressure
    cap — a pilot earns the highest tier purely on available space. The None case is returned
    HONESTLY (symmetric with choose_jit_profile); the SERVICE decides whether to force the floor
    (always grab the pilot) or skip until space frees. Pure: size_model only."""
    for prof in best_first:
        if projected_free - estimate_gb_for_profile(prof, runtime_min, 1, measured) >= reserve_gb:
            return prof
    return None


def next_pilot_profile_descend(*, start_rank, current_pid, current_rank, last_pid, ranked):
    """Best-tier-first / step-DOWN counterpart of :func:`next_pilot_profile`. ``start_rank`` is the
    best-that-fits-space rank the service computed (from :func:`choose_pilot_profile`) — the
    quality CEILING for this run. ``ranked`` is floor-first (rank 0 = most permissive). Returns
    ``(new_pid, action)``:

      * 'target'    — first attempt OR the profile changed externally: search at the ceiling
        (``start_rank``, the highest tier space allows).
      * 'step_down' — a prior attempt at the CURRENT profile found nothing (``last_pid ==
        current_pid``): descend ONE rung toward the floor (divert to a lower tier), never above the
        ceiling.
      * 'at_floor'  — already at rank 0 (the most permissive floor): hold and keep re-searching, so
        a pilot is NEVER abandoned.

    A pilot thus targets the highest tier space allows and, only when that tier keeps coming up
    empty, diverts down one rung per run — never climbing above the space ceiling and never dropping
    out of the search entirely. Pure: stdlib only."""
    n = len(ranked)
    if n == 0:
        return current_pid, "at_floor"
    start_rank = max(0, min(int(start_rank), n - 1))
    if last_pid is not None and last_pid == current_pid:
        new_rank = current_rank - 1
        if new_rank < 0:
            return ranked[0]["id"], "at_floor"
        return ranked[min(new_rank, start_rank)]["id"], "step_down"
    return ranked[start_rank]["id"], "target"
