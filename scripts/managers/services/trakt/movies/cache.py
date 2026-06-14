"""
TraktMovieCacheManager
=======================
Per-TMDb-ID disk cache for Trakt movie data (people, metadata).
Stores gzip-compressed JSON with a configurable TTL.

Layout:
    cache/trakt/movies/{tmdb_id}.json.gz

Default TTL: 7 days (604_800 seconds).
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
import time
from pathlib import Path

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.daemons.daemon_paths import CACHE_TTL_S, MOVIE_BUCKETS
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktMovieCacheManager(BaseManager, ComponentManagerMixin):
    DEFAULT_TTL = CACHE_TTL_S          # 7 days
    # Absolute, single-sourced path shared with enrich_daemon.py so the daemon's
    # output is visible to the runtime scorer (previously CWD-relative → diverged).
    DEFAULT_DIR = MOVIE_BUCKETS["people"]

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "TraktMoviesManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent       = kwargs.get("manager")
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
        self.ttl     = int(kwargs.get("ttl", self.DEFAULT_TTL))
        self.base_dir = Path(kwargs.get("cache_dir", self.DEFAULT_DIR))

        if not self.dry_run:
            self.base_dir.mkdir(parents=True, exist_ok=True)

        # One directory per daemon bucket so the runtime can read cast/crew (people),
        # the Trakt audience rating (ratings), and the summary — the movie twin of
        # TraktShowCacheManager._dirs. "people" stays at base_dir (back-compat with get()).
        self._dirs: dict[str, Path] = {
            "people":  self.base_dir,
            "ratings": Path(MOVIE_BUCKETS.get("ratings", self.base_dir.parent / "movie_ratings")),
            "summary": Path(MOVIE_BUCKETS.get("summary", self.base_dir.parent / "movie_summary")),
            "related": Path(MOVIE_BUCKETS.get("related", self.base_dir.parent / "movie_related")),
        }
        self.logger.log_debug(f"[TraktMovieCache] dir={self.base_dir}, ttl={self.ttl}s")

    def _read_bucket(self, bucket: str, tmdb_id: int) -> dict | None:
        path = self._dirs[bucket] / f"{tmdb_id}.json.gz"
        try:
            st = path.stat()
        except OSError:
            return None
        # 0-byte = poison (killed write / file-sync dehydration), never a valid gz —
        # treat as a clean miss so a phantom-empty file is never read as enrichment.
        if st.st_size == 0 or (time.time() - st.st_mtime) > self.ttl:
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.logger.log_debug(f"[TraktMovieCache] Read error for {bucket}/{tmdb_id}: {e}")
            return None

    def get_people(self, tmdb_id: int) -> dict:
        """``{cast, crew}`` credits for a movie, or ``{}`` — mirrors TraktShowCacheManager
        so the enrichment broadcast call-site is identical for movies and shows."""
        return self._read_bucket("people", int(tmdb_id)) or {} if tmdb_id else {}

    def get_ratings(self, tmdb_id: int) -> dict:
        """Trakt movie ratings ``{rating, votes, distribution}``, or ``{}``."""
        return self._read_bucket("ratings", int(tmdb_id)) or {} if tmdb_id else {}

    def get_summary(self, tmdb_id: int) -> dict:
        """Trakt movie summary (extended=full) dict, or ``{}``."""
        return self._read_bucket("summary", int(tmdb_id)) or {} if tmdb_id else {}

    # ── Internal ──────────────────────────────────────────────────────────────────

    def _path(self, tmdb_id: int) -> Path:
        return self.base_dir / f"{tmdb_id}.json.gz"

    # ── Public ────────────────────────────────────────────────────────────────────

    def is_fresh(self, tmdb_id: int) -> bool:
        """True if a non-expired, non-empty entry exists for tmdb_id (a 0-byte
        poison file is NOT fresh — it must re-fetch, not be served as a hit)."""
        try:
            st = self._path(tmdb_id).stat()
        except OSError:
            return False
        return st.st_size > 0 and (time.time() - st.st_mtime) <= self.ttl

    @timeit("get")
    def get(self, tmdb_id: int) -> dict | None:
        """Return cached data, or None if missing / expired."""
        if not self.is_fresh(tmdb_id):
            return None
        try:
            with gzip.open(self._path(tmdb_id), "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.logger.log_debug(f"[TraktMovieCache] Read error for {tmdb_id}: {e}")
            return None

    @timeit("get_fresh")
    def get_fresh(self, tmdb_id: int) -> "tuple[bool, dict | None]":
        """Single-pass freshness check + read: returns ``(was_fresh, data)``.

        ``was_fresh`` mirrors :meth:`is_fresh` exactly (file exists AND within ttl);
        ``data`` is the parsed dict, or None if missing / expired / unreadable. So a
        fresh-but-corrupt entry yields ``(True, None)`` — matching ``is_fresh()=True``
        while ``get()=None`` — which lets a caller derive both signals from one read
        instead of an :meth:`is_fresh` stat followed by a redundant :meth:`get`."""
        path = self._path(tmdb_id)
        try:
            st = path.stat()
        except OSError:
            return False, None
        # Mirror is_fresh: a 0-byte poison file is NOT fresh (re-fetch, don't serve).
        if st.st_size == 0 or (time.time() - st.st_mtime) > self.ttl:
            return False, None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return True, json.load(f)
        except Exception as e:
            self.logger.log_debug(f"[TraktMovieCache] Read error for {tmdb_id}: {e}")
            return True, None

    @timeit("set")
    def set(self, tmdb_id: int, data: dict) -> bool:
        """Write data to disk.  No-ops in dry_run.

        Atomic: writes to a temp file in the same dir then os.replace, so a hard
        kill (e.g. the daemon being terminated) can never leave a truncated gz
        that a later reader would choke on.
        """
        if self.dry_run:
            return False
        path = self._path(tmdb_id)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".trakt_", suffix=".tmp")
            with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp, path)
            return True
        except Exception as e:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            self.logger.log_warning(f"[TraktMovieCache] Write error for {tmdb_id}: {e}")
            return False

    def invalidate(self, tmdb_id: int):
        self._path(tmdb_id).unlink(missing_ok=True)

    def stats(self) -> dict:
        files = list(self.base_dir.glob("*.json.gz")) if self.base_dir.exists() else []
        now   = time.time()
        fresh = sum(1 for f in files if (now - f.stat().st_mtime) <= self.ttl)
        return {"total": len(files), "fresh": fresh, "stale": len(files) - fresh}
