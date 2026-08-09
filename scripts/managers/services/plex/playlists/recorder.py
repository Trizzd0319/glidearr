"""plex/playlists/recorder.py — write what a shelf SURFACED into the durable ledger.

One implementation, shared by every surface. The alternative was an
``append_events`` call grown independently inside each builder, which is how
``updated_tags.add("keep")`` ended up in four places (`GLD-ACQ-31`) and how two
definitions of "which collections exist" drifted apart (`GLD-PLY-18`).

WHAT IT RECORDS. A recommendation EVENT per published item: stable identity,
surface, profile, and the moment it was shown. That row is what later lets
``playlists/discovery.py`` answer "they watched Iron Giant off the Anniversary
shelf and finished it" instead of "a play happened".

IDENTITY COMES FROM THE PLAN, NOT FROM A LOOKUP. The builders now carry
``tmdb_id`` / ``tvdb_id`` / ``tvdb_join_key`` on their plan items
(`GLD-PLY-24`), captured at selection time. Re-deriving identity here from a
ratingKey would reintroduce exactly the bug that change fixed: a Plex re-scan
RETIRES ratingKeys - 11/117 on one series when measured - so the lookup either
fails or, worse, succeeds against a different title that inherited the number.
A row recorded against the wrong entity is undetectable downstream.

ARMED-ONLY, and this is a real decision rather than caution. A dry run PREVIEWS
a shelf; it does not show it to anybody. Recording a placement nobody saw would
manufacture misses - the item enters its measurement window, the window closes
with no play, and the surface's completion rate drops for a recommendation that
was never made.

IDEMPOTENT BY CONSTRUCTION. ``append_events`` dedups on
``(recommended_date, surface, profile, entity_id)``, so several runs on one day
record the pick ONCE. That is what makes a shelf held open across its whole
window (rather than churned daily) measurable at all.

NEVER RAISES. A ledger write failing must cost the measurement, never the
playlist. Every path returns counts and logs.
"""
from __future__ import annotations

from scripts.managers.machine_learning.labels import recommendations as R

#: Surfaces recorded by this module.
#:
#: Hidden Gems is ABSENT on purpose - it already records through its own publish
#: path, and a second writer for the same surface would double every row and
#: quietly corrupt the hit rate it has been accumulating.
#:
#: Anniversary's movies and shows share ONE surface. They are the same editorial
#: act ("this week in history") split across two Plex objects only because a smart
#: playlist can point at a single section; splitting them in the ledger would halve
#: every sample for no analytical gain.
SURFACE_ANNIVERSARY = "anniversary"
SURFACE_TONIGHT = "tonight"


def record_surface(*, base_dir, picks, profile, surface, logger=None,
                   window_days: int = 30, media_default: str = "movie") -> dict:
    """Append one event per pick. Returns ``{recorded, skipped, deduped}``.

    ``picks`` are plan items as the builder cached them. ``base_dir`` is the
    cache root; ``None`` disables recording entirely (returns zeros), which is
    what a manager without a cache should do rather than guess at a path.
    """
    stats = {"recorded": 0, "skipped": 0, "deduped": 0}
    if base_dir is None or not picks:
        return stats
    try:
        rows, skipped = R.build_events_ex(
            picks, profile=str(profile), surface=str(surface),
            media=media_default, window_days=int(window_days))
    except Exception as exc:
        _warn(logger, f"[Recorder] '{surface}' build failed for '{profile}': "
                      f"{type(exc).__name__}: {exc}")
        return stats
    stats["skipped"] = skipped
    if not rows:
        # Every pick unidentifiable is a REPORTABLE state, not a quiet zero: it
        # is precisely how 97 shows a night vanished before GLD-PLY-24.
        if skipped:
            _warn(logger, f"[Recorder] '{surface}' for '{profile}': all {skipped} "
                          f"pick(s) lacked a stable id - nothing recorded.")
        return stats
    try:
        written = R.append_events(base_dir, rows, surface=str(surface))
    except Exception as exc:
        _warn(logger, f"[Recorder] '{surface}' append failed for '{profile}': "
                      f"{type(exc).__name__}: {exc}")
        return stats
    stats["recorded"] = int(written or 0)
    stats["deduped"] = len(rows) - stats["recorded"]
    return stats


def base_dir_of(cache):
    """The cache root a ledger writes under, or None when unavailable.

    None rather than a guessed path: writing a parquet tree into the wrong
    directory is worse than not writing one, because it looks like success and
    the real ledger stays empty.
    """
    kb = getattr(cache, "key_builder", None) if cache is not None else None
    return getattr(kb, "base_dir", None) if kb is not None else None


def _warn(logger, msg):
    if logger is not None and hasattr(logger, "log_warning"):
        logger.log_warning(msg)
