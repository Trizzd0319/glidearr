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
