"""
mdblist/age_cache.py — shared Common Sense Media age cache (read / write / batch-fetch).
================================================================================
The movie classifier + ``router_movie`` READ this cache; the enrichment daemon and the
one-shot ``enrich_csm_ages`` tool POPULATE it from MDBList. Single JSON keyed by tmdbId:

    support/cache/mdblist/age_ratings.json = { "<tmdbId>": <age int> | null }

A value of ``null`` means "MDBList looked it up and Common Sense has no rating" — cached
so it's not re-queried; the cache itself is therefore the resume state (skip ids already
present). Transient failures are NOT cached, so they retry on the next pass.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from scripts.managers.services.mdblist.client import movie_ratings, show_ratings

# scripts/managers/services/mdblist/age_cache.py → parents[3] == scripts/
_SCRIPTS = Path(__file__).resolve().parents[3]
AGE_CACHE_PATH = _SCRIPTS / "support" / "cache" / "mdblist" / "age_ratings.json"
# TV ages live in a SEPARATE file keyed by show-space tmdbId — movie and show tmdbIds
# share the same integer space, so they must never share a {tmdbId: age} dict.
TV_AGE_CACHE_PATH = _SCRIPTS / "support" / "cache" / "mdblist" / "age_ratings_tv.json"
BUDGET_FLOOR = 500          # leave this many MDBList daily requests in reserve


def load(path: Path = AGE_CACHE_PATH) -> dict:
    """Return the {tmdbId: age|null} cache (empty dict on miss/corrupt)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def age_for(tmdb_id, *, path: Path = AGE_CACHE_PATH, cache: "dict | None" = None) -> "int | None":
    """Common Sense recommended age for a tmdbId, or None. Pure cache read — no network.

    Returns an int ONLY when a real CSM age is cached; None for both the
    'looked-up-no-CSM' (stored null) and 'not cached yet' (missing key) cases — this is
    the same ``isinstance(v, int)`` contract router_movie._csm_age already uses, so a
    null/missing entry lets the classifier fall back to its genre/cert/studio heuristics.

    ``cache`` lets a caller reuse an already-loaded dict so each candidate lookup doesn't
    re-read the file; omit it for a one-off lookup (loads ``path`` itself). Pass
    ``path=TV_AGE_CACHE_PATH`` for shows (the movie and show caches are separate files
    because their tmdbIds share an integer space)."""
    c = cache if cache is not None else load(path)
    if not tmdb_id:
        return None
    v = c.get(str(tmdb_id))
    return v if isinstance(v, int) else None


def save(cache: dict, path: Path = AGE_CACHE_PATH) -> None:
    """Atomic write (temp + os.replace) so a hard kill never leaves a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".age_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def fetch_into(apikey: str, tmdb_ids, cache: dict, *, max_calls: int,
               throttle: float = 0.08, stop=None, lookup=movie_ratings) -> tuple[int, int]:
    """Look up CSM age for up to ``max_calls`` ids from ``tmdb_ids`` NOT already cached,
    mutating ``cache`` in place. Returns ``(looked, covered)`` — looked = lookups made,
    covered = how many had a real CSM age. ``stop`` is an optional ``() -> bool`` to abort
    early (e.g. the daemon's stop sentinel). Transient failures are skipped (not cached).
    ``lookup`` is the MDBList client call — ``movie_ratings`` (default) for the movie cache
    or ``show_ratings`` for the TV cache; both share the ``{"ok", "age_rating", ...}`` shape."""
    looked = covered = 0
    for tmdb in tmdb_ids:
        if looked >= max_calls:
            break
        if stop and stop():
            break
        key = str(tmdb)
        if key in cache:
            continue
        r = lookup(apikey, tmdb)
        if not r["ok"]:
            if r.get("status") == 429:
                time.sleep(30)
            continue
        cache[key] = r["age_rating"]            # int age, or None = looked-up-no-CSM
        looked += 1
        if r["age_rating"] is not None:
            covered += 1
        time.sleep(throttle)
    return looked, covered


def budget(apikey: str) -> tuple[int, int]:
    """(used, limit) for the MDBList daily request budget; (0, 25000) on failure."""
    import requests
    try:
        d = requests.get("https://api.mdblist.com/user", params={"apikey": apikey}, timeout=15).json()
        return int(d.get("api_requests_count") or 0), int(d.get("api_requests") or 25000)
    except Exception:
        return 0, 25000
