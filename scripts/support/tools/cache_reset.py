"""
cache_reset.py — selective wipe of the rebuildable caches for a clean test rebuild.
================================================================================
CLEARS the sonarr / radarr / trakt-library / Plex / Tautulli / operational caches (everything that
re-syncs cheaply from the *arr / Plex / Tautulli APIs on the next run) while PRESERVING the EXPENSIVE
internet-fetched data:

  • the enrichment DAEMON's output — per-title Trakt metadata (summaries, ratings, related graphs,
    studios, translations, cast/crew) under ``trakt/`` (one API call per title; thousands of titles
    = a multi-day re-fetch), and
  • the collection / saga / universe info from the internet — ``universe/`` (chronolists timelines),
    ``people_matrix/`` (cast/crew affinity), ``discovery/`` (anniversary shelves), ``mdblist/`` /
    ``mal/`` lists, ``franchise_catalog_state.json``, and the learned universe/saga membership under
    ``plex/playlists/`` (kometa_franchises / saga_member_titles / universe_source — survives a Kometa
    rebuild).

DEFAULT-PRESERVE: anything not POSITIVELY recognised as rebuildable is KEPT, so a cache namespace
added in the future can never be silently wiped. The classification is authoritative — see the
PRESERVE/CLEAR sets below — and ``run_cache_reset(apply=False)`` (the default) previews the plan with
sizes, deleting nothing, so an operator can review before committing with ``apply=True``.

Driven by ``python scripts/main.py --reset-caches [--apply]``.
"""
from __future__ import annotations

import shutil
from pathlib import Path

# ── Internet-fetched enrichment buckets under trakt/ (enrich_daemon writes one API call per title) ──
# These are the only trakt SUBDIRECTORIES worth keeping; the user-account dirs (history + the
# per-username ratings/watchlist dir) are the rebuildable ones. All trakt FILES (enrich_daemon.pid /
# .stop, main_run.active, enrichment_cursor.json, daemon_radarr_movies.json, daemon_sonarr_series.json,
# lock temps) are daemon STATE → preserved so a running daemon is never disturbed (see plan rule below).
_TRAKT_ENRICH_BUCKETS = frozenset({
    "movie_summary", "movie_ratings", "movie_related", "movie_aliases", "movie_studios",
    "movie_translations", "movie_lists", "movies",
    "show_summary", "show_ratings", "show_related", "shows",
})

# ── Top-level cache entries preserved WHOLESALE: collection / saga / universe + people enrichment ──
_PRESERVE_TOPLEVEL_DIRS = frozenset({
    "universe",        # saga/universe (chronolists timelines, saga_credit_preview)
    "people_matrix",   # cast/crew affinity matrix (Trakt-sourced)
    "discovery",       # this-week anniversary shelves + saved discovery (internet/plex.tv)
    "mdblist",         # MDBList internet lists (rate-limited)
    "mal",             # MyAnimeList seasonal catalog + user anime lists
})
_PRESERVE_TOPLEVEL_FILES = frozenset({
    "franchise_catalog_state.json",   # learned franchise membership (Wikidata/Wikipedia)
})
# Within plex/, the universe/saga membership + playlist plans live under playlists/ → preserve it;
# the rest of plex/ (users / watchlist / episodes / movies / debug) is library state that re-syncs.
_PLEX_PRESERVE_CHILDREN = frozenset({"playlists"})

# ── Top-level dirs cleared WHOLESALE (rebuild cheaply from the *arr / Plex / Tautulli APIs) ──
_CLEAR_TOPLEVEL_DIRS = frozenset({
    "sonarr", "radarr", "tautulli", "pilot_search",
    "acquisition", "lifecycle", "notifications", "system", "size_model",
})


def _is_clear_toplevel_file(name: str) -> bool:
    """Top-level radarr library snapshots (radarr.movies/monitoring/quality/tags/cf_sync.*.json) are
    cleared; any OTHER unrecognised top-level file is preserved (default-safe)."""
    return name.startswith("radarr.") and name.endswith(".json")


def plan_cache_reset(root: Path) -> dict:
    """Classify every cache entry under ``root`` into clear / preserve / unknown (a Path list each).
    DEFAULT-PRESERVE: an unrecognised entry is added to BOTH ``unknown`` (so the caller can surface
    it) AND ``preserve`` (so it is never deleted). ``trakt/`` and ``plex/`` are classified one level
    deep; everything else at the top level."""
    clear: list[Path] = []
    preserve: list[Path] = []
    unknown: list[Path] = []
    if not root.exists():
        return {"clear": clear, "preserve": preserve, "unknown": unknown}

    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        name = entry.name
        if name in _PRESERVE_TOPLEVEL_DIRS or name in _PRESERVE_TOPLEVEL_FILES:
            preserve.append(entry)
        elif name == "trakt" and entry.is_dir():
            for sub in sorted(entry.iterdir(), key=lambda p: p.name):
                if sub.is_dir():
                    # bucket dirs (enrichment) → keep; history + per-username dir → clear.
                    (preserve if sub.name in _TRAKT_ENRICH_BUCKETS else clear).append(sub)
                else:
                    preserve.append(sub)          # every trakt FILE is enrich-daemon state → keep
        elif name == "plex" and entry.is_dir():
            for sub in sorted(entry.iterdir(), key=lambda p: p.name):
                (preserve if sub.name in _PLEX_PRESERVE_CHILDREN else clear).append(sub)
        elif name in _CLEAR_TOPLEVEL_DIRS and entry.is_dir():
            clear.append(entry)
        elif entry.is_file() and _is_clear_toplevel_file(name):
            clear.append(entry)
        else:
            unknown.append(entry)                 # new/unrecognised namespace → PRESERVE, but flag it
            preserve.append(entry)
    return {"clear": clear, "preserve": preserve, "unknown": unknown}


def _size_bytes(p: Path) -> int:
    try:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def _fmt_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{n} B"


def run_cache_reset(*, apply: bool, logger=None, root: Path | None = None,
                    stop_daemons: bool = True) -> dict:
    """Preview (``apply=False``, default) or perform (``apply=True``) the selective cache wipe.
    Returns ``{cleared, preserved, unknown, reclaimed_bytes, applied}``. Before deleting, the
    pilot-search daemon is stopped (it actively writes CLEAR data — pilot_search/ + sonarr/jit — so a
    clear under a live daemon would race); the enrichment daemon is left alone (it writes only
    PRESERVE data)."""
    if root is None:
        from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
        root = CacheKeyBuilder().base_dir
    root = Path(root)

    def _log(msg):
        if logger is not None and hasattr(logger, "log_info"):
            logger.log_info(msg)
        else:
            print(msg)

    plan = plan_cache_reset(root)
    clear, preserve, unknown = plan["clear"], plan["preserve"], plan["unknown"]
    tag = "APPLY" if apply else "DRY-RUN (preview only — nothing deleted; re-run with --apply)"

    # Stop the pilot-search daemon first so it can't re-create pilot_search/ mid-clear.
    if apply and stop_daemons:
        try:
            from scripts.managers.factories.daemons.supervisor import PilotSearchDaemonSupervisor
            if PilotSearchDaemonSupervisor(logger=logger).is_running():
                _log("[CacheReset] stopping the pilot-search daemon before clearing…")
                PilotSearchDaemonSupervisor(logger=logger).stop()
        except Exception as e:
            _log(f"[CacheReset] could not stop the pilot-search daemon ({e}); continuing.")

    _log(f"[CacheReset] {tag}  cache root: {root}")
    clear_sizes = [(p, _size_bytes(p)) for p in clear]
    clear_sizes.sort(key=lambda t: t[1], reverse=True)
    reclaimed = 0
    for p, sz in clear_sizes:
        rel = p.relative_to(root)
        if apply:
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
                _log(f"[CacheReset]   cleared  {rel}  ({_fmt_size(sz)})")
            except OSError as e:
                _log(f"[CacheReset]   FAILED   {rel}: {e}")
                continue
        else:
            _log(f"[CacheReset]   would clear  {rel}  ({_fmt_size(sz)})")
        reclaimed += sz

    preserve_total = sum(_size_bytes(p) for p in preserve)
    _log(f"[CacheReset] PRESERVED {len(preserve)} entr(y/ies) (~{_fmt_size(preserve_total)}): "
         f"enrichment + collection/saga/universe kept intact.")
    if unknown:
        _log(f"[CacheReset] NOTE: {len(unknown)} unrecognised namespace(s) were PRESERVED by default "
             f"(review if a new rebuildable cache was added): "
             f"{', '.join(p.name for p in unknown)}")
    verb = "Cleared" if apply else "Would clear"
    _log(f"[CacheReset] {verb} {len(clear)} entr(y/ies), reclaiming ~{_fmt_size(reclaimed)}. "
         f"{'Done — next `python scripts/main.py` rebuilds the sonarr/radarr/trakt caches.' if apply else 'Re-run with --apply to delete.'}")
    return {"cleared": [str(p.relative_to(root)) for p in clear],
            "preserved": [str(p.relative_to(root)) for p in preserve],
            "unknown": [str(p.relative_to(root)) for p in unknown],
            "reclaimed_bytes": reclaimed, "applied": apply}
