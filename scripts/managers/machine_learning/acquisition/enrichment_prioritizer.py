"""acquisition/enrichment_prioritizer.py — Trakt enrichment selection (pure).
==============================================================================
The decision core of ``trakt/movies/people.enrich_movies`` (ML Step 8). Each run
only a bounded slice of the library is enriched with Trakt credits: everything the
household has watched (always), everything already cached (free disk read), plus a
round-robin chunk of the rest so the full library cycles through without ever
blasting ~1,800 live calls at once and tripping a 429. This module owns the PURE
selection — who is "priority", how the round-robin cursor advances, and the
per-row enrich/defer/skip precedence. The service keeps the I/O (cache freshness
reads, the global_cache cursor get/set, the live ``get_people`` fetch, logging).

PURE — stdlib only; no HTTP, no global_cache, no disk. The cache-freshness signal
(``already_cached``) is an I/O input the service injects per row.

Public API:
  * priority_set(movies, *, watched_ids, watched_titles_norm) -> set[int]
        tmdbIds the household has watched — by Trakt id OR normalised title.
  * chunk_pool(candidates, *, exclude_ids) -> list[int]
        the sorted tmdbId pool a cursor round-robins over (priority ids removed).
  * chunk_window(pool, *, last_id, size) -> ChunkWindow
        advance the round-robin cursor one ``size`` slice past ``last_id`` (wrapping
        at the end); returns the window + the cursor dict to persist (None if empty).
  * enrich_action(*, has_file, is_priority, already_cached, selected_for_fetch, has_file_only)
        the per-row precedence -> 'skip_no_file' | 'enrich' | 'defer'.
  * relevance_rank(rows) -> list
        order (tmdb_id, popularity, critic) rows most-relevant-first (the owned chunk
        walks relevance order, not tmdbId order).
  * relevance_window(ordered_ids, *, done_ids, size) -> (window, new_done)
        the next `size` not-yet-done ids this cycle, with a self-resetting done-set
        cursor (replaces the id-bisect cursor for the owned pool).
"""
from __future__ import annotations

import bisect
from typing import NamedTuple


class ChunkWindow(NamedTuple):
    start: int                  # cursor start index into the pool (post-wrap)
    end: int                    # exclusive end index
    ids: set                    # the tmdbIds in this run's slice
    cursor: "dict | None"       # cursor state to persist, or None when the pool is empty


def priority_set(movies, *, watched_ids, watched_titles_norm) -> set:
    """tmdbIds of movies the household has watched — matched by Trakt id OR by
    normalised (lower/stripped) title. ``movies`` should already be filtered to
    rows carrying a tmdbId; the ``m.get`` guards keep it total regardless."""
    return {
        m["tmdbId"] for m in movies
        if m.get("tmdbId")
        and (
            m["tmdbId"] in watched_ids
            or (m.get("title") or "").lower().strip() in watched_titles_norm
        )
    }


def chunk_pool(candidates, *, exclude_ids) -> list:
    """The ascending tmdbId pool the round-robin cursor walks: the candidates'
    ids minus ``exclude_ids`` (the priority set, which is always enriched and so
    must never consume a chunk slot), sorted so the cursor advances deterministically."""
    return sorted(
        m["tmdbId"] for m in candidates
        if m.get("tmdbId") and m["tmdbId"] not in exclude_ids
    )


def chunk_window(pool, *, last_id, size) -> ChunkWindow:
    """Advance the round-robin cursor one ``size`` slice past ``last_id``.

    ``last_id`` is the highest tmdbId enriched last run; ``bisect_right`` resumes
    just after it, wrapping to the start once the pool is exhausted. Returns the
    slice (start/end indices + the id set) plus the cursor dict to persist — or a
    ``None`` cursor when the pool is empty (nothing to persist)."""
    start = bisect.bisect_right(pool, last_id)
    if start >= len(pool):
        start = 0
    end = min(start + size, len(pool))
    ids = set(pool[start:end])
    cursor = None
    if pool:
        cursor = {
            "last_tmdb_id": pool[end - 1] if end > start else last_id,
            "position": end,
            "chunk_size": size,
            "total": len(pool),
        }
    return ChunkWindow(start, end, ids, cursor)


def enrich_action(*, has_file, is_priority, already_cached, selected_for_fetch, has_file_only) -> str:
    """Per-row precedence (the caller has already handled the no-tmdbId case):
      * 'skip_no_file' — has_file_only run and this is an unowned, non-priority,
        uncached row → acquisition owns it, not enrichment.
      * 'enrich'       — already cached (free attach) OR selected for a live fetch
        this run (the watched-tier budget, the owned chunk, or the unowned chunk).
      * 'defer'        — eligible but not selected this run; a later run gets it.

    NOTE: ``is_priority`` no longer forces an enrich. The watched tier is now
    budget-capped upstream — cached watched rows still attach via ``already_cached``,
    but an *uncached* watched row only enriches when the service picks it into
    ``selected_for_fetch`` (so a large watch-import can't blast N live calls at once
    and trip a 429). ``is_priority`` survives solely so a watched-but-unowned row
    escapes the ``skip_no_file`` guard."""
    if has_file_only and not has_file and not is_priority and not already_cached:
        return "skip_no_file"
    if already_cached or selected_for_fetch:
        return "enrich"
    return "defer"


def relevance_rank(rows) -> list:
    """Order ``(tmdb_id, popularity, critic)`` rows most-relevant-first so the owned
    enrichment chunk reaches the titles that matter sooner than tmdbId order would.
    Key: popularity desc, then critic desc, then tmdb_id asc (fully deterministic).
    A missing (None / non-numeric) popularity or critic sorts last within its level."""
    def _num(x):
        return x if isinstance(x, (int, float)) and not isinstance(x, bool) else float("-inf")
    return [r[0] for r in sorted(rows, key=lambda r: (-_num(r[1]), -_num(r[2]), r[0]))]


def relevance_window(ordered_ids, *, done_ids, size):
    """Pick the next ``size`` ids from the relevance-ordered pool that haven't been
    enriched yet this cycle, and return ``(window, new_done)``.

    ``done_ids`` is the set already covered this cycle; it's first pruned to the
    current pool (movies that left the library don't keep the cycle from completing
    or bloat the persisted set). When everything has been covered the cycle resets
    and refills from the top THIS run — the round-robin never idles."""
    pool = set(ordered_ids)
    done = set(done_ids) & pool                      # drop stale ids no longer present
    remaining = [i for i in ordered_ids if i not in done]
    if not remaining:                                # cycle complete -> restart now
        done = set()
        remaining = list(ordered_ids)
    window = set(remaining[:size])
    return window, (done | window)
