"""lifecycle/watched_definition.py — the ONE definition of "watched" (pure).
==============================================================================
WHAT THIS ANSWERS
-----------------
"Did the household WATCH this, or did they sample it?" — for a single Tautulli
play. Every parquet watch column (``is_watched``, ``watch_count``) is now built
by counting only the plays this module admits, so Glidearr agrees with what Plex
and Tautulli show the household instead of inventing a second definition.

THE BUG THIS EXISTS TO FIX
--------------------------
``is_watched`` was ``watch_count > 0``, and ``watch_count`` was incremented once
per Tautulli history row **regardless of completion** — in BOTH producers
(``sonarr/cache/episode_files._fetch_tautulli_episode_history`` and
``radarr/cache/movie_files._fetch_watch_map``). A 30-second sample therefore:

  * marked the file watched,
  * started the 3-hour grace clock that queues it for deletion,
  * bought it an engagement floor of 50 in ``watch_likelihood`` (≈ WEB-1080p),
  * and counted as a rewatch towards A3 once it happened twice.

Measured on the live cache: 50 of 511 episode plays (9.8%) and 141 of 389 movie
plays (36.2%) were below the bar.

THE RULE (and its precedence)
-----------------------------
1. ``watched_status`` — TAUTULLI'S OWN per-row verdict (``1`` watched /
   ``0.5`` partial / ``0`` unwatched). PREFERRED whenever present: it already
   reflects whatever completion threshold the operator configured in Tautulli
   (85% out of the box, shared with Plex). Honouring it is the whole point — it
   is the operator's verdict, not ours.
2. ``percent_complete >= threshold_pct`` — the FALLBACK for rows without the
   field: history cached before ``watched_status`` was admitted to the Tautulli
   projection whitelist (``services/tautulli/watch_history._CACHED_HISTORY_FIELDS``),
   and any Tautulli old enough not to emit it. Those rows DO carry
   ``percent_complete``, so the transition is silent — no historical row reads as
   unwatched merely because the cache has not cycled.
3. Neither present → ``True``. "A play is a play" is the fail-open answer: this
   predicate gates DELETE guards (a viewer's protected interval, the grace
   clock), and a guard must never shrink on missing data.

WHAT IS **NOT** THRESHOLDED — AND WHY
-------------------------------------
``percent_complete`` and ``last_watched_at`` stay THRESHOLD-FREE. They are raw
playback FACTS ("how far did the furthest play get", "when was this last
played"), not verdicts, and two things depend on that:

  * ``watch_likelihood.explain_likelihood`` grades a sub-threshold play as
    *started* (20–90%) or *abandoned* (<20%) off ``percent_complete``. If that
    signal were thresholded too, a title abandoned at 15% would fall through to
    the UNTOUCHED branch (``untouched_base`` 25 + score) and abandoning a show
    could RAISE its quality target — the exact inverse of the intent.
  * Every guard that must mean "watched" already conjoins ``is_watched``
    (see ``lifecycle.grace_policy``), while the handful of consumers that read
    ``last_watched_at`` ALONE use it PROTECTIVELY (don't delete something played
    recently; don't call a series cold). Leaving it raw keeps this change from
    becoming a stealth widening of deletion.

``last_watched_at`` also carries the "was this tried at all?" bit for the one
case ``percent_complete`` cannot express: a play Tautulli reported at 0%. Two
such rows exist on the live cache (The Big Bang Theory S02E03, The Seven Deadly
Sins S01E01); without that bit they would land on UNTOUCHED and score *higher*
after being abandoned. ``explain_likelihood`` reads it for exactly that.

CONFIG
------
``watched_threshold.percent`` (default 85) — the system-level knob. It was born
as ``episode_retention.watched_percent`` when the bar applied to the retention
rule only; that name is now wrong (it governs the movie library too), so the old
key remains a BACK-COMPATIBLE ALIAS and the new one wins. ``resolve_retention_config``
resolves through here as well, so the retention rule and the global bar can never
disagree.

PURE — stdlib only; no pandas, no HTTP, no config manager, no service imports.

Public API:
  * DEFAULT_WATCHED_PERCENT               — 85.0
  * resolve_watched_percent(config)       — new key → legacy alias → default
  * watched_by_tautulli(watched_status, percent_complete, *, threshold_pct)
  * play_is_watched(entry, *, threshold_pct)   — the same call on a history row
"""
from __future__ import annotations

import math

# Tautulli and Plex both ship an 85% completion threshold. Keeping the fallback on
# the same number means the percentage leg and the `watched_status` leg agree on a
# default install, so a cache that has not yet cycled `watched_status` in produces
# the same verdicts as one that has.
DEFAULT_WATCHED_PERCENT = 85.0

# The system-level home for the knob, and the key it was born under.
WATCHED_PERCENT_KEY = ("watched_threshold", "percent")
WATCHED_PERCENT_LEGACY_KEY = ("episode_retention", "watched_percent")


def _as_float(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(out) else out


def resolve_watched_percent(config) -> float:
    """The completion bar (0–100) a play must clear when Tautulli's own
    ``watched_status`` is absent.

    Precedence: ``watched_threshold.percent`` → ``episode_retention.watched_percent``
    (the legacy alias, kept so an existing config keeps working) → 85. Clamped to
    0–100; an unparseable value falls through to the next source rather than to 0,
    because a bar of 0 would silently restore the very bug this replaces."""
    cfg = config or {}
    for block, key in (WATCHED_PERCENT_KEY, WATCHED_PERCENT_LEGACY_KEY):
        blk = cfg.get(block)
        if not isinstance(blk, dict):
            continue
        val = _as_float(blk.get(key), None)
        if val is not None:
            return min(100.0, max(0.0, val))
    return DEFAULT_WATCHED_PERCENT


def watched_by_tautulli(watched_status, percent_complete, *, threshold_pct) -> bool:
    """Whether Tautulli considers this play a WATCH, not a sample.

    ``watched_status`` is Tautulli's own per-row verdict and is preferred whenever
    present: it already reflects the operator's configured completion threshold, so
    honouring it keeps Glidearr consistent with what Plex and Tautulli show the
    household — no second, disagreeing definition of "watched". Tautulli emits
    ``1`` (watched), ``0.5`` (partially watched) and ``0`` (unwatched); only ``1``
    counts.

    ``percent_complete >= threshold_pct`` is the FALLBACK for rows without the
    field — history cached before ``watched_status`` was admitted to the Tautulli
    projection whitelist, and any Tautulli old enough not to emit it. Because the
    fallback reads a field those rows DO carry, the transition is silent: no
    historical row reads as unwatched merely because the cache has not cycled."""
    if watched_status is not None:
        val = _as_float(watched_status, None)
        if val is not None:
            return val >= 1.0
        s = str(watched_status).strip().lower()
        if s in ("true", "watched"):
            return True
        if s in ("false", "unwatched", "partial", ""):
            return False
    pct = _as_float(percent_complete, None)
    if pct is None:
        # No verdict and no percentage: fall back to "a play is a play" rather than
        # silently un-watching a row we know nothing about. A DELETE guard must
        # never shrink a viewer's protected interval on missing data.
        return True
    return pct >= float(threshold_pct)


def play_is_watched(entry, *, threshold_pct) -> bool:
    """:func:`watched_by_tautulli` applied to a raw Tautulli history row.

    The one call both producers make per history row, so the movie path and the
    show path cannot drift apart the way they did when each inlined
    ``watch_count += 1``. A non-dict (or a row missing both fields) is handled by
    the same fail-open rule."""
    if not isinstance(entry, dict):
        return True
    return watched_by_tautulli(entry.get("watched_status"),
                               entry.get("percent_complete"),
                               threshold_pct=threshold_pct)
