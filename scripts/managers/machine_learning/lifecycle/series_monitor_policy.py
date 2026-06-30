"""lifecycle/series_monitor_policy.py — Sonarr monitor-by-watchability decision (pure).
==============================================================================
The pure decision slice of the Sonarr series monitor policy — the Sonarr twin of the Radarr
owned-monitor / stale-prune lifecycle, but MONITOR-ONLY: it never deletes a series or its files, it
only flips Sonarr's ``monitored`` flag so the low-affinity tail goes dormant (Sonarr stops grabbing
it) and climbers come back. The scoring (watchability), the Sonarr ``series/editor`` PUT and the
global_cache dwell clock are I/O kept in the manager; only the side-effect-free routing lives here.

HYSTERESIS (anti-flap, the same shape as the 4K demote): promote at ``promote_threshold`` (default
35), demote only below ``demote_floor`` (default 20). The [demote_floor, promote_threshold) band is
STICKY — a series there keeps its current monitored state, so a score wobbling near the line never
flaps. An optional per-series DWELL (``dwell_days``) requires the score to stay below the floor for N
days before unmonitoring, absorbing a transient dip.

HARD GUARDS (never unmonitor): a keep-tagged series (``keep_series`` / ``keep_season`` — the user
pinned it) or one the household has WATCHED. A series with no score yet (un-graded) DEFERS — never
act on missing data (the affinity is understated until the enrich daemon fills it).

PURE — stdlib only; no HTTP, no global_cache, no service imports.

Public API:
  * series_monitor_action(*, monitored, score, has_score, keep_tagged, watched,
        promote_threshold, demote_floor, age_days, dwell_days) -> str
        -> 'monitor' | 'unmonitor' | 'hold' | 'defer'.
"""
from __future__ import annotations


def series_monitor_action(*, monitored, score, has_score, keep_tagged, watched,
                          promote_threshold, demote_floor, age_days, dwell_days) -> str:
    """Route one series by watchability:

      * 'defer'     — no score yet (un-graded): never act on missing affinity data.
      * a MONITORED series:
          - keep-tagged OR watched           -> 'hold'      (hard guard: never unmonitor)
          - score >= demote_floor            -> 'hold'      (in the sticky band or above)
          - score <  demote_floor:
              age_days >= dwell_days          -> 'unmonitor' (sustained low affinity -> dormant)
              else                            -> 'hold'      (still clocking toward the dwell)
      * an UNMONITORED series:
          - score >= promote_threshold        -> 'monitor'   (climbed back / earns monitoring)
          - else                              -> 'hold'      (stay dormant; sticky band)

    Note the asymmetry is deliberate: demote below the FLOOR, (re)monitor only at/above the
    THRESHOLD, so a series must cross the whole band to change state — no flapping. The keep/watched
    guard only blocks UNmonitoring; it never force-monitors a series the user chose to leave dormant.
    """
    if not has_score:
        return "defer"
    if monitored:
        if keep_tagged or watched:
            return "hold"                      # pinned / watched → never dormant it
        if score >= demote_floor:
            return "hold"                      # sticky band or above → keep monitored
        return "unmonitor" if age_days >= dwell_days else "hold"
    # unmonitored
    return "monitor" if score >= promote_threshold else "hold"
