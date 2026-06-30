"""lifecycle/monitor_policy.py — monitored-missing triage (pure).
==============================================================================
The pure decision slices of ``radarr/repair/anomaly.triage_monitored_missing`` (ML
Step 8). That method scores every monitored-but-missing movie and routes it to
search / adjust-down+search / unmonitor; the scoring (Trakt credits fetch +
``score_movie``) and the bulk movie/editor PUTs are I/O, so only the side-effect-free
decisions live here: whether a release is available to search for, and which action
a given score earns.

PURE — stdlib only; no HTTP, no global_cache, no service imports.

Public API:
  * release_available(movie, now) -> bool
        whether a home-media release has passed (Radarr ``isAvailable`` OR a
        physical/digital release date in the past OR status 'released') — the gate
        on searching at all.
  * triage_action(*, score, has_keep_tag, credits_fetched, cur_profile_id,
                  hd720p_id, watch_threshold, unmonitor_below) -> str
        the routing -> 'keep_skip' | 'defer' | 'unmonitor' | 'adjust_and_search'
        | 'search'.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _date_passed(val, now) -> bool:
    """True if an ISO date string ``val`` parses to a moment at/​before ``now``
    (naive timestamps are treated as UTC). Blank/non-string/unparseable -> False."""
    if not val:
        return False
    if not isinstance(val, str):
        # A truthy non-str (e.g. an int epoch) would AttributeError on .replace;
        # Radarr returns ISO strings today, but harden against the off-shape.
        return False
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= now
    except (ValueError, TypeError):
        return False


def release_available(movie, now) -> bool:
    """Whether a home-media release has passed so a search is worthwhile. Trusts
    Radarr's ``isAvailable`` (it respects minimumAvailability), then falls back to a
    physical/digital release date in the past, then a 'released' status."""
    if movie.get("isAvailable", False):
        return True
    return (
        _date_passed(movie.get("physicalRelease"), now)
        or _date_passed(movie.get("digitalRelease"), now)
        or movie.get("status") == "released"
    )


def triage_action(*, score, has_keep_tag, credits_fetched, cur_profile_id,
                  hd720p_id, watch_threshold, unmonitor_below,
                  household_watched=False, is_uhd_instance=False, warrants_uhd=False) -> str:
    """Route a monitored-but-missing movie by its watchability score:

      * 'keep_skip'         — below the unmonitor floor but keep/universe-tagged: a
        user override outranks the score; never unmonitor.
      * 'defer'             — below the floor but credits aren't fetched yet, so the
        score is unreliable; wait for enrichment rather than unmonitor a favourite.
      * 'unmonitor'         — below the floor: unlikely to be watched.
      * 'adjust_and_search' — marginal (below the search threshold) and on the wrong
        profile: drop the quality bar to HD-720p, then search.
      * 'search'            — good watchability (or no HD-720p target): search now.

    ``household_watched`` is a hard override: a movie the household HAS watched (its
    tmdb is in the watched-set) lost its file, so it is always RE-ACQUIRED (search /
    adjust-and-search) and never unmonitored/deferred/keep-skipped — a watched movie
    scoring low for lack of ratings must not be silently dropped. (The owned-movie
    stale prune already hard-guards the watched-set; this closes the same gap for the
    monitored-missing triage.)

    ``is_uhd_instance`` flips the routing for a DEDICATED 4K/UHD instance (one that
    holds ONLY titles warranting 4K). A monitored-missing title there must NEVER be
    grabbed at a sub-4K quality — that would land a 720p/1080p file in the 4K library;
    its lower-resolution baseline is the standard instance's job. So on the 4K instance
    the 'adjust_and_search' (drop-to-720p) branch is unreachable and the routing is:

      * warrants 4K (``warrants_uhd`` — keep/universe-tagged, household-watched, or a
        score at/above the UHD threshold) → 'search' at the 4K profile (never adjusted),
      * else, score unreliable (credits not fetched) → 'defer' (don't drop a household
        favourite on understated data — the same protection the standard path gives),
      * else → 'unmonitor' (no sub-4K grab into the 4K library)."""
    if is_uhd_instance:
        if warrants_uhd or household_watched:
            return "search"
        if not credits_fetched:
            return "defer"
        return "unmonitor"
    if not household_watched:
        if score < unmonitor_below and has_keep_tag:
            return "keep_skip"
        if score < unmonitor_below and not credits_fetched:
            return "defer"
        if score < unmonitor_below:
            return "unmonitor"
    if score < watch_threshold and hd720p_id and cur_profile_id != hd720p_id:
        return "adjust_and_search"
    return "search"
