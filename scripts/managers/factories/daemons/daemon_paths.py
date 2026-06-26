"""
daemon_paths.py — single source of truth shared by the enrichment daemon and its
supervisor.
================================================================================
Both ``scripts/support/daemons/enrich_daemon.py`` (the background worker) and
``supervisor.py`` (which spawns / stops it from main.py) import their paths and
tuning constants from here, so they can never disagree about WHERE the pid file,
stop sentinel, cursor, log, or cache buckets live.

``MOVIE_BUCKETS["people"]`` is ALSO imported by
``scripts/managers/services/trakt/movies/cache.py`` so the runtime
``TraktMovieCacheManager`` and the daemon read/write the exact same absolute
``…/support/cache/trakt/movies`` directory (they previously diverged — the
manager used a CWD-relative path).

Pure path/constant module — only imports ``pathlib`` so importing it is always
safe and cycle-free.
"""
from __future__ import annotations

from pathlib import Path

# This file: scripts/managers/factories/daemons/daemon_paths.py → parents[3] == scripts/
SCRIPTS_DIR   = Path(__file__).resolve().parents[3]
REPO_ROOT     = SCRIPTS_DIR.parent                       # dir that holds the `scripts` package
SUPPORT_DIR   = SCRIPTS_DIR / "support"
CONFIG_PATH   = SUPPORT_DIR / "config" / "config.json"
CACHE_TRAKT   = SUPPORT_DIR / "cache" / "trakt"
DAEMON_SCRIPT = SUPPORT_DIR / "daemons" / "enrich_daemon.py"

PID_PATH      = CACHE_TRAKT / "enrich_daemon.pid"
STOP_SENTINEL = CACHE_TRAKT / "enrich_daemon.stop"
CURSOR_PATH   = CACHE_TRAKT / "enrichment_cursor.json"
LOG_PATH      = SUPPORT_DIR / "logs" / "enrich_daemon.log"

# main.py writes this (its pid + a timestamp) for the duration of a run; the
# daemon pauses fetching while it's present so it doesn't compete for the shared
# Trakt rate-limit window. Crash-safety: the daemon ignores the sentinel if the
# pid is dead or it is older than MAIN_ACTIVE_MAX_AGE_S, so a crashed main can
# never pause the daemon forever.
MAIN_ACTIVE_SENTINEL  = CACHE_TRAKT / "main_run.active"
MAIN_ACTIVE_MAX_AGE_S = 1_800     # 30-min backstop against a stale sentinel
MAIN_ACTIVE_POLL_S    = 5         # how often a paused daemon re-checks

# Per-data-type movie cache buckets (file = {tmdb_id}.json.gz inside each).
# "people" is the EXISTING bucket the watchability scorer already reads — keep it
# at …/trakt/movies. The rest are new siblings, one directory per Trakt endpoint.
MOVIE_BUCKETS: dict[str, Path] = {
    "people":       CACHE_TRAKT / "movies",
    "summary":      CACHE_TRAKT / "movie_summary",
    "ratings":      CACHE_TRAKT / "movie_ratings",
    "related":      CACHE_TRAKT / "movie_related",
    "aliases":      CACHE_TRAKT / "movie_aliases",
    "studios":      CACHE_TRAKT / "movie_studios",
    "translations": CACHE_TRAKT / "movie_translations",
    "lists":        CACHE_TRAKT / "movie_lists",
}

# Shows are enriched for the TV watchability scorer: summary (genres) for
# genre affinity (incl. cross-medium movie next-watch), credits (people) for
# Group-B cast/crew affinity, audience ratings for the Group-F critic blend, and
# related for the Group-C3 related-graph. One directory per bucket, file =
# {tvdbId}.json.gz inside each. TraktShowCacheManager reads these exact paths.
SHOW_BUCKETS: dict[str, Path] = {
    "summary": CACHE_TRAKT / "show_summary",
    "people":  CACHE_TRAKT / "shows",
    "ratings": CACHE_TRAKT / "show_ratings",
    "related": CACHE_TRAKT / "show_related",
}

# Person↔media co-occurrence graph (machine_learning/people_matrix), built from the
# people buckets above. TWO artifacts cached SEPARATELY on purpose: the forward map
# is LIBRARY-derived (stable — only changes as the daemon enriches new titles); the
# person-affinity weights are WATCHED-SET-derived (volatile — change every run as the
# household watches more). Conflating them would force a full matrix rebuild on every
# watched-set change.
PEOPLE_MATRIX_PATH:   Path = CACHE_TRAKT / "people_matrix.json.gz"      # forward map {(medium,ext_id):{role:[pid]}}
PEOPLE_AFFINITY_PATH: Path = CACHE_TRAKT / "people_affinity.json.gz"    # household {tmdb_person_id: weight}
PEOPLE_NAMES_PATH:    Path = CACHE_TRAKT / "people_names.json.gz"       # {tmdb_person_id: name}; infra id→name lookup, not read by the scorers

# Default per-movie scope. translations is ON by default for the globally-shared
# deployment — it caches localized title/tagline/overview text for non-English
# users. NOTE: no consumer reads the translations bucket yet, so today it only
# WARMS the cache (at +1 Trakt call/movie) for a future localized renderer.
# lists (paginated/expensive) stays opt-in.
DEFAULT_SCOPE: list[str] = ["summary", "people", "ratings", "related", "aliases", "studios", "translations"]
# "summary" (shows/{id}?extended=full) adds GENRES — needed for TV genre affinity and the
# cross-medium movie next-watch signal; "related" enriches shows/{id}/related for the
# Group-C3 related-graph term (mirrors the movie scope). Negative-caching (NO_DATA) keeps
# them cheap once warm; each adds at most one endpoint-call per show per TTL.
SHOW_SCOPE:    list[str] = ["summary", "people", "ratings", "related"]

# ── Pilot-search daemon ─────────────────────────────────────────────────────────
# A SECOND, independent daemon (scripts/support/daemons/pilot_search_daemon.py) that
# drains Sonarr pilot interactive-search jobs out of the main run process. ``run_pilot_search``
# hands a large batch (> threshold stubs) to this daemon via a JSON job file instead of the
# in-process NON-daemon worker thread — that thread blocks interpreter exit, so a 9k-stub
# spree would hang the whole run until every indexer search finished. Small batches still run
# in-process. Its files live in their OWN directory (not the Trakt cache) so the two daemons
# never collide on pid / stop / log.
PILOT_DAEMON_SCRIPT   = SUPPORT_DIR / "daemons" / "pilot_search_daemon.py"
PILOT_CACHE_DIR       = SUPPORT_DIR / "cache" / "pilot_search"
PILOT_QUEUE_DIR       = PILOT_CACHE_DIR / "queue"        # <instance>.json — newest enqueue wins (overwrite)
PILOT_PROCESSING_DIR  = PILOT_CACHE_DIR / "processing"   # claimed jobs; orphans are re-queued on daemon start
PILOT_PID_PATH        = PILOT_CACHE_DIR / "pilot_search_daemon.pid"
PILOT_STOP_SENTINEL   = PILOT_CACHE_DIR / "pilot_search_daemon.stop"
PILOT_LOG_PATH        = SUPPORT_DIR / "logs" / "pilot_search_daemon.log"
PILOT_POLL_INTERVAL_S = 2.0      # how often the idle daemon re-checks the queue / stop sentinel
PILOT_IDLE_EXIT_S     = 1_800    # exit after this long with no work, so an idle daemon never lingers
                                 # forever; the supervisor re-spawns it the next time a batch is enqueued
PILOT_SEARCH_WORKERS  = 6        # parallel JIT step-down workers (mirrors JIT_SEARCH_MAX_WORKERS)
PILOT_INTERACTIVE_WORKERS = 3    # parallel interactive (release?episodeId=) searches — LOWER than the
                                 # JIT workers on purpose: a live interactive search hits the indexer
                                 # synchronously, and 6-at-once over a big batch makes a single indexer
                                 # (e.g. NZBgeek) time out / rate-limit → false 'no_results'. Tunable via
                                 # pilot_interactive.search_workers.
PILOT_SPILL_THRESHOLD = 10       # batches LARGER than this spill to the daemon; <= stay in-process
PILOT_SEARCH_BATCH    = 100      # EpisodeSearch grab-triggers are fired in chunks of this many episodeIds
                                 # (Sonarr's command takes a list) so a 9k-stub spree posts ~91 commands
                                 # to Sonarr's task queue instead of 9k — the profile of each series is
                                 # still set individually first, so each grab honours its own tier


# ── Rate / timing tuning ──────────────────────────────────────────────────────
CACHE_TTL_S    = 604_800     # 7 days — matches TraktMovieCacheManager
RATE_WINDOW_S  = 300         # Trakt's 5-minute rate window
# Trakt's hard limit is 1000 calls / 5 min. We spend at most this many ENDPOINT
# calls per cycle (each movie costs len(scope) calls, not 1) then sleep a window.
SAFE_THROUGHPUT_CALLS         = 650
# The user's conservative mental floor (documented; not the operative value).
CONSERVATIVE_THROUGHPUT_CALLS = 500
SLEEP_SECONDS   = 306         # just over one rate window between cycles
POLL_INTERVAL_S = 1.5         # stop-sentinel poll granularity during sleep/work
GRACE_STOP_S    = 10          # supervisor waits this long for a clean stop before hard-kill
