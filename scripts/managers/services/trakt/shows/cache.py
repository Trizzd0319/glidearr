"""
TraktShowCacheManager
=====================
Per-tvdbId disk cache reader for Trakt SHOW data written by the enrich daemon.
The series twin of ``TraktMovieCacheManager`` — gzip-compressed JSON, keyed by
Sonarr's ``tvdbId`` (the daemon fills ``shows/{tvdbId}/people`` and
``shows/{tvdbId}/ratings`` straight from Trakt).

Layout (single-sourced from daemon_paths.SHOW_BUCKETS so daemon + runtime never
diverge):
    cache/trakt/shows/{tvdbId}.json.gz         — {cast, crew} credits
    cache/trakt/show_ratings/{tvdbId}.json.gz  — {rating, votes, distribution}

Default TTL: 7 days (matches the daemon's CACHE_TTL_S).
"""
from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.daemons.daemon_paths import CACHE_TTL_S, SHOW_BUCKETS
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktShowCacheManager(BaseManager, ComponentManagerMixin):
    DEFAULT_TTL = CACHE_TTL_S          # 7 days

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktShowsManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent       = kwargs.get("manager")
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
        self.ttl     = int(kwargs.get("ttl", self.DEFAULT_TTL))
        # One directory per bucket; "ratings" may not exist on older daemon
        # builds — default it next to the people bucket so reads degrade to a
        # clean cache-miss rather than a crash.
        self._dirs: dict[str, Path] = {
            "people":  Path(SHOW_BUCKETS["people"]),
            "ratings": Path(SHOW_BUCKETS.get("ratings", SHOW_BUCKETS["people"].parent / "show_ratings")),
            "related": Path(SHOW_BUCKETS.get("related", SHOW_BUCKETS["people"].parent / "show_related")),
            # "summary" (shows/{id}?extended=full) carries genres+overview — a newer
            # daemon bucket; default it beside the others so older builds cache-miss cleanly.
            "summary": Path(SHOW_BUCKETS.get("summary", SHOW_BUCKETS["people"].parent / "show_summary")),
        }
        self.logger.log_debug(
            f"[TraktShowCache] people={self._dirs['people']} "
            f"ratings={self._dirs['ratings']} related={self._dirs['related']} ttl={self.ttl}s"
        )

    # ── Internal ──────────────────────────────────────────────────────────────────

    def _path(self, bucket: str, tvdb_id: int) -> Path:
        return self._dirs[bucket] / f"{tvdb_id}.json.gz"

    def _read(self, bucket: str, tvdb_id: int) -> dict | None:
        path = self._path(bucket, tvdb_id)
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
            self.logger.log_debug(f"[TraktShowCache] Read error for {bucket}/{tvdb_id}: {e}")
            return None

    # ── Public ────────────────────────────────────────────────────────────────────

    #: buckets the show scorer reads — the default fingerprint set.
    SCORER_BUCKETS = ("people", "ratings", "related")

    def fingerprint(self, tvdb_id: int, buckets: "tuple | None" = None) -> tuple:
        """Cheap identity of what this show's cached payloads WOULD return, without
        decompressing them: ``(size, mtime_ns, fresh)`` per bucket from a single
        ``stat()`` each.

        Exists so cache-key builders (the show-score memo) can decide "unchanged"
        for ~12k series at stat() cost instead of ~36k gzip+JSON reads. The
        freshness flag is included because ``_read`` serves None once a payload
        ages past the TTL — mtime alone would miss that transition. Any daemon
        rewrite changes size/mtime, so a stale fingerprint cannot outlive a
        refresh."""
        out = []
        now = time.time()
        for b in (buckets or self.SCORER_BUCKETS):
            try:
                st = self._path(b, tvdb_id).stat()
                out.append((st.st_size, st.st_mtime_ns,
                            st.st_size > 0 and (now - st.st_mtime) <= self.ttl))
            except OSError:
                out.append(None)
        return tuple(out)

    def fingerprint_index(self, buckets: "tuple | None" = None) -> dict:
        """``{bucket: {tvdb_id: (size, mtime_ns, fresh)}}`` — the same tuples
        ``fingerprint()`` returns, but gathered with ONE ``os.scandir`` per bucket.

        A per-series ``fingerprint()`` costs 3 stat() syscalls; across a 12k-series
        library that is ~36k stats, which dominates the score pass on Windows
        (Defender inspects each). Three directory scans replace them, after which a
        lookup is a dict hit. Missing bucket dirs yield empty maps, so callers fall
        back to a clean 'no cached payload' fingerprint."""
        now = time.time()
        suffix = ".json.gz"
        out: dict = {}
        for b in (buckets or self.SCORER_BUCKETS):
            d: dict = {}
            try:
                with os.scandir(self._dirs[b]) as it:
                    for e in it:
                        if not e.name.endswith(suffix):
                            continue
                        try:
                            tid = int(e.name[: -len(suffix)])
                        except ValueError:
                            continue
                        try:
                            st = e.stat()
                        except OSError:
                            continue
                        d[tid] = (st.st_size, st.st_mtime_ns,
                                  st.st_size > 0 and (now - st.st_mtime) <= self.ttl)
            except OSError:
                pass
            out[b] = d
        return out

    def is_fresh(self, tvdb_id: int, bucket: str = "people") -> bool:
        # A 0-byte poison file is NOT fresh — it must re-fetch, not be served as a hit.
        try:
            st = self._path(bucket, tvdb_id).stat()
        except OSError:
            return False
        return st.st_size > 0 and (time.time() - st.st_mtime) <= self.ttl

    @timeit("get_people")
    def get_people(self, tvdb_id: int) -> dict:
        """Return ``{cast, crew}`` credits for a show, or ``{}`` if not cached.

        Mirrors ``TraktMoviePeopleManager.get_people`` so the scorer's Group-B
        affinity call-site is identical for movies and shows.
        """
        if not tvdb_id:
            return {}
        return self._read("people", int(tvdb_id)) or {}

    @timeit("get_ratings")
    def get_ratings(self, tvdb_id: int) -> dict:
        """Return the Trakt show ratings dict ``{rating, votes, distribution}``,
        or ``{}`` if not cached."""
        if not tvdb_id:
            return {}
        return self._read("ratings", int(tvdb_id)) or {}

    @timeit("get_summary")
    def get_summary(self, tvdb_id: int) -> dict:
        """Return the Trakt show summary (extended=full) dict — carries ``genres`` +
        ``overview`` + ids — or ``{}`` if not cached. Feeds the parquet ``genres``
        column (cross-medium affinity) as a fallback to Sonarr's own genres."""
        if not tvdb_id:
            return {}
        return self._read("summary", int(tvdb_id)) or {}

    @timeit("get_related")
    def get_related(self, tvdb_id: int) -> list:
        """Return this show's Trakt-related neighbours (a list of show objects,
        each ``{"ids": {"tvdb": ..., ...}, "title": ..., "year": ...}``), or ``[]``
        if not cached. Feeds the Group-C3 related-graph affinity term. The daemon
        negative-caches an empty ``{}`` for shows with no related data, so coerce a
        non-list payload to ``[]``."""
        if not tvdb_id:
            return []
        data = self._read("related", int(tvdb_id))
        return data if isinstance(data, list) else []

    def stats(self) -> dict:
        out: dict = {}
        now = time.time()
        for bucket, d in self._dirs.items():
            files = list(d.glob("*.json.gz")) if d.exists() else []
            fresh = sum(1 for f in files if (now - f.stat().st_mtime) <= self.ttl)
            out[bucket] = {"total": len(files), "fresh": fresh, "stale": len(files) - fresh}
        return out
