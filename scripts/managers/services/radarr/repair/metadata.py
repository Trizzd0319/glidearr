"""
RadarrRepairMetadataManager
============================
Detects and repairs metadata issues in Radarr:
- Movies missing tmdbId or imdbId  → refresh + attempt ID repair
- Titles containing problematic special characters
- Year mismatches between Radarr and the movie file name/release data

``RefreshMovie`` commands are asynchronous — Radarr processes them in the
background over minutes to hours.  To avoid re-queuing the same movies
every run, the manager writes the refreshed IDs to global_cache under
``radarr/metadata/pending_refresh/{instance}`` with a 24-hour TTL.
Movies already in that set are skipped until the cache expires.
"""

from __future__ import annotations

import re

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

# Characters that are genuinely problematic in movie titles for filesystem/API use.
# Colon (:) and forward-slash (/) are intentionally excluded — they appear in normal
# movie titles ("Mission: Impossible", "AC/DC") and Radarr already strips them from paths.
_PROBLEMATIC_CHARS_RE = re.compile(r'[\\*?"<>|]')

# How long to remember that a RefreshMovie was queued (seconds).
# Radarr typically finishes a library refresh within a few hours;
# 24 h gives plenty of headroom before we try again.
_PENDING_TTL_S = 86_400   # 24 hours


class RadarrRepairMetadataManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrRepairManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(
        self,
        logger=None,
        config=None,
        global_cache=None,
        validator=None,
        registry=None,
        **kwargs,
    ):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _pending_cache_key(self, instance: str) -> str:
        return f"radarr/metadata/pending_refresh/{instance}"

    def _get_pending_ids(self, instance: str) -> set[int]:
        """Return the set of movie IDs already queued for RefreshMovie."""
        if not self.global_cache:
            return set()
        raw = self.global_cache.get(self._pending_cache_key(instance))
        if isinstance(raw, list):
            return set(raw)
        return set()

    def _add_pending_ids(self, instance: str, ids: list[int]):
        """Persist newly-queued movie IDs with a 24-hour TTL."""
        if not self.global_cache or not ids:
            return
        existing = self._get_pending_ids(instance)
        merged   = sorted(existing | set(ids))
        try:
            self.global_cache.set(
                self._pending_cache_key(instance),
                merged,
                expiration_time=_PENDING_TTL_S,
            )
        except TypeError:
            # Fallback if the cache implementation doesn't accept expiration_time
            self.global_cache.set(self._pending_cache_key(instance), merged)

    def _clear_resolved_ids(self, instance: str, resolved_ids: list[int]):
        """Remove IDs from the pending set once they've been resolved."""
        if not self.global_cache or not resolved_ids:
            return
        existing = self._get_pending_ids(instance)
        updated  = sorted(existing - set(resolved_ids))
        self.global_cache.set(self._pending_cache_key(instance), updated)

    # ── Instance resolution ──────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    def _get_movies(self, instance: str) -> list[dict]:
        """Return full movie list — prefer global_cache to avoid a redundant API call."""
        if self.global_cache:
            cached = self.global_cache.get(f"radarr.movies.{instance}.full")
            if cached:
                return cached
        return self.radarr_api._make_request(instance, "movie", fallback=[]) or []

    # ── Missing IDs ──────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_missing_external_ids")
    def find_missing_external_ids(self, instance: str) -> list[dict]:
        """
        Find movies that are missing tmdbId or imdbId.

        Returns list of {movie_id, title, year, tmdb_id, missing_ids: list[str]}
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            self.logger.log_warning("radarr_api not available")
            return []

        movies  = self._get_movies(instance)
        results = []
        for m in movies:
            missing = []
            if not m.get("tmdbId"):
                missing.append("tmdbId")
            if not m.get("imdbId"):
                missing.append("imdbId")
            if missing:
                results.append({
                    "movie_id":    m.get("id"),
                    "title":       m.get("title"),
                    "year":        m.get("year"),
                    "tmdb_id":     m.get("tmdbId"),
                    "missing_ids": missing,
                })

        self.logger.log_info(
            f"[Metadata] Missing external IDs in '{instance}': {len(results)} movie(s)"
        )
        return results

    # ── Repair: missing IDs ──────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("repair_missing_ids")
    def repair_missing_ids(self, instance: str) -> dict:
        """
        Attempt to resolve missing tmdbId / imdbId for each affected movie.

        Strategy per movie:
        ┌─────────────────────────────────────────────────────────────────┐
        │ Missing only imdbId (has tmdbId)                                │
        │   → Re-fetch the individual movie record from Radarr.           │
        │     Radarr stores the imdbId in the full GET /movie/{id}        │
        │     response even when it was absent from the bulk list.        │
        │     If still absent, trigger RefreshMovie so Radarr re-polls    │
        │     TMDb and back-fills the field.                              │
        ├─────────────────────────────────────────────────────────────────┤
        │ Missing tmdbId (unmatched entry)                                │
        │   → Trigger RefreshMovie so Radarr re-attempts TMDb matching.  │
        │     These are hard to fix programmatically — a human may need   │
        │     to manually match in the Radarr UI for stubborn cases.      │
        └─────────────────────────────────────────────────────────────────┘

        Batches RefreshMovie commands to avoid hammering Radarr.
        Returns stats dict.
        """
        instance = self._resolve_instance(instance)
        stats = {
            "checked":         0,
            "imdb_resolved":   0,   # imdbId filled from individual fetch
            "refresh_queued":  0,   # RefreshMovie triggered
            "still_missing":   0,   # could not resolve
            "failed":          0,
        }

        if self.radarr_api is None:
            return stats

        missing_movies = self.find_missing_external_ids(instance)
        if not missing_movies:
            return stats

        # Skip movies already pending a refresh from a previous run.
        # RefreshMovie is async — Radarr may still be processing it.
        # We give it 24 hours before retrying.
        pending_ids = self._get_pending_ids(instance)
        if pending_ids:
            before = len(missing_movies)
            missing_movies = [m for m in missing_movies if m["movie_id"] not in pending_ids]
            skipped = before - len(missing_movies)
            if skipped:
                self.logger.log_info(
                    f"[Metadata] Skipping {skipped} movie(s) already pending refresh "
                    f"(< 24 h since last queue) — {len(missing_movies)} remain"
                )
        if not missing_movies:
            self.logger.log_info(
                f"[Metadata] All missing-ID movies are already pending refresh — nothing to do."
            )
            return stats

        # Split into two buckets
        needs_imdb_only: list[dict] = []   # have tmdbId, just missing imdbId
        needs_full_refresh: list[dict] = []  # missing tmdbId

        for m in missing_movies:
            stats["checked"] += 1
            if "tmdbId" in m["missing_ids"]:
                needs_full_refresh.append(m)
            else:
                needs_imdb_only.append(m)

        # ── Bucket 1: missing only imdbId ────────────────────────────────────────
        # Re-fetch each movie individually — the bulk /movie endpoint sometimes
        # omits imdbId while GET /movie/{id} returns it in full detail.
        resolved_ids: list[int] = []
        still_needs_refresh: list[dict] = []
        _resolved_rows: list[list] = []

        for m in needs_imdb_only:
            mid   = m["movie_id"]
            title = m["title"]
            try:
                full = self.radarr_api._make_request(instance, f"movie/{mid}", fallback=None)
                if full and isinstance(full, dict) and full.get("imdbId"):
                    _resolved_rows.append([str(title)[:28], str(full["imdbId"])])
                    stats["imdb_resolved"] += 1
                    resolved_ids.append(mid)
                else:
                    # Still missing — trigger a metadata refresh
                    still_needs_refresh.append(m)
            except Exception as e:
                self.logger.log_debug(f"  ⚠️ Could not fetch '{title}' (id={mid}): {e}")
                still_needs_refresh.append(m)

        _imdb_title = "[dry_run] imdbId resolved from full record" if self.dry_run else "imdbId resolved from full record"
        self.logger.log_grid(["Title", "imdb"], _resolved_rows, title=_imdb_title, cap=28)

        if resolved_ids:
            self._clear_resolved_ids(instance, resolved_ids)

        # ── Bucket 2: batch RefreshMovie for remaining ────────────────────────────
        all_refresh = still_needs_refresh + needs_full_refresh
        if all_refresh:
            refresh_ids = [m["movie_id"] for m in all_refresh]

            # Batch in groups of 50 to avoid timeouts on large libraries
            BATCH_SIZE = 50
            queued_this_run: list[int] = []
            for i in range(0, len(refresh_ids), BATCH_SIZE):
                batch = refresh_ids[i : i + BATCH_SIZE]
                if self.dry_run:
                    self.logger.log_info(
                        f"  [dry_run] Would RefreshMovie for {len(batch)} movie(s) "
                        f"(batch {i // BATCH_SIZE + 1})"
                    )
                    stats["refresh_queued"] += len(batch)
                    continue
                # _make_request returns fallback (None) on failure rather than
                # raising. SQLITE_BUSY retry/backoff + per-instance write
                # serialisation are handled centrally, so no inter-batch sleep is
                # needed (it only waits when actually contended).
                resp = self.radarr_api._make_request(
                    instance,
                    "command",
                    method="POST",
                    payload={"name": "RefreshMovie", "movieIds": batch},
                    fallback=None,
                )
                if resp is None:
                    self.logger.log_warning(
                        f"  ⚠️ RefreshMovie batch failed for {len(batch)} movie(s)"
                    )
                    stats["failed"] += len(batch)
                else:
                    stats["refresh_queued"] += len(batch)
                    queued_this_run.extend(batch)
                    self.logger.log_info(
                        f"  🔄 RefreshMovie queued for {len(batch)} movie(s) "
                        f"(batch {i // BATCH_SIZE + 1}/{(len(refresh_ids) - 1) // BATCH_SIZE + 1})"
                    )

            # Persist queued IDs so next run skips them
            if queued_this_run:
                self._add_pending_ids(instance, queued_this_run)

        still_missing_count = len(needs_full_refresh) - (stats["refresh_queued"] - len(still_needs_refresh))
        stats["still_missing"] = max(0, still_missing_count)

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_info(
            f"[Metadata] {prefix}Missing-ID repair for '{instance}': "
            f"{stats['checked']} checked | {stats['imdb_resolved']} imdbId resolved | "
            f"{stats['refresh_queued']} refresh queued | {stats['failed']} failed"
        )
        return stats

    # ── Repair: problematic titles ────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("repair_problematic_titles")
    def repair_problematic_titles(self, instance: str) -> dict:
        """
        Trigger RefreshMovie for every movie with a problematic title so
        Radarr re-fetches the canonical title from TMDb.

        In most cases the bad characters came from a manual import or an
        old TMDb entry — a refresh pulls the current clean title.
        Batches in groups of 50 with a 1s pause.
        """
        instance = self._resolve_instance(instance)
        stats = {"checked": 0, "refresh_queued": 0, "failed": 0}

        if self.radarr_api is None:
            return stats

        problematic = self.find_problematic_titles(instance)
        if not problematic:
            return stats

        # Skip movies already pending refresh
        pending_ids = self._get_pending_ids(instance)
        if pending_ids:
            before      = len(problematic)
            problematic = [m for m in problematic if m.get("movie_id") not in pending_ids]
            skipped     = before - len(problematic)
            if skipped:
                self.logger.log_info(
                    f"[Metadata] Skipping {skipped} problematic-title movie(s) already "
                    f"pending refresh (< 24 h) — {len(problematic)} remain"
                )
        if not problematic:
            self.logger.log_info(
                "[Metadata] All problematic-title movies are already pending refresh."
            )
            return stats

        ids = [m["movie_id"] for m in problematic if m.get("movie_id")]
        stats["checked"] = len(ids)

        BATCH = 50
        queued_this_run: list[int] = []
        for i in range(0, len(ids), BATCH):
            batch = ids[i : i + BATCH]
            if self.dry_run:
                self.logger.log_info(
                    f"  [dry_run] Would RefreshMovie for {len(batch)} problematic-title movie(s)"
                )
                stats["refresh_queued"] += len(batch)
                continue
            resp = self.radarr_api._make_request(
                instance, "command", method="POST",
                payload={"name": "RefreshMovie", "movieIds": batch},
                fallback=None,
            )
            if resp is None:
                self.logger.log_warning(
                    f"  ⚠️ RefreshMovie batch failed for {len(batch)} movie(s)"
                )
                stats["failed"] += len(batch)
            else:
                stats["refresh_queued"] += len(batch)
                queued_this_run.extend(batch)
                self.logger.log_info(
                    f"  🔄 RefreshMovie queued for {len(batch)} problematic-title movie(s) "
                    f"(batch {i // BATCH + 1}/{(len(ids) - 1) // BATCH + 1})"
                )

        if queued_this_run:
            self._add_pending_ids(instance, queued_this_run)

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_info(
            f"[Metadata] {prefix}Problematic-title repair for '{instance}': "
            f"{stats['checked']} checked | {stats['refresh_queued']} refresh queued | "
            f"{stats['failed']} failed"
        )
        return stats

    # ── Problematic titles ───────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_problematic_titles")
    def find_problematic_titles(self, instance: str) -> list[dict]:
        """
        Find movies whose titles contain characters that cause filesystem
        or downstream API issues (backslash, colon, asterisk, etc.).

        Returns list of {movie_id, title, year, problematic_chars}
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            return []

        movies  = self._get_movies(instance)
        results = []
        for m in movies:
            title = m.get("title") or ""
            found = _PROBLEMATIC_CHARS_RE.findall(title)
            if found:
                results.append({
                    "movie_id":          m.get("id"),
                    "title":             title,
                    "year":              m.get("year"),
                    "problematic_chars": list(set(found)),
                })

        self.logger.log_info(
            f"[Metadata] Problematic titles in '{instance}': {len(results)} movie(s)"
        )
        return results

    # ── Year mismatches ──────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_year_mismatches")
    def find_year_mismatches(self, instance: str, tolerance: int = 1) -> list[dict]:
        """
        Detect movies where the Radarr year differs from the year embedded
        in the movie file's relative path (e.g. "Title (2005)/file.mkv").

        ``tolerance``: allow +/- this many years before flagging.

        Returns list of {movie_id, title, radarr_year, path_year, path}
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            return []

        movies  = self._get_movies(instance)
        results = []
        _year_re = re.compile(r"\((\d{4})\)")

        for m in movies:
            radarr_year = m.get("year")
            if not radarr_year:
                continue
            mf   = m.get("movieFile") or {}
            path = mf.get("relativePath") or mf.get("path") or ""
            match = _year_re.search(path)
            if not match:
                continue
            path_year = int(match.group(1))
            if abs(path_year - int(radarr_year)) > tolerance:
                results.append({
                    "movie_id":    m.get("id"),
                    "title":       m.get("title"),
                    "radarr_year": radarr_year,
                    "path_year":   path_year,
                    "path":        path,
                })

        self.logger.log_info(
            f"[Metadata] Year mismatches in '{instance}': {len(results)} movie(s)"
        )
        return results

    # ── Repair: trigger metadata refresh (legacy) ────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("repair_refresh_metadata")
    def repair_refresh_metadata(
        self,
        instance: str,
        movie_ids: list[int] | None = None,
    ) -> dict:
        """
        Trigger Radarr's RefreshMovie command for a specific list of movie IDs.
        For missing-ID repair use repair_missing_ids() which has smarter logic.
        """
        instance = self._resolve_instance(instance)
        stats = {"checked": 0, "triggered": 0, "failed": 0}

        if self.radarr_api is None:
            return stats

        if movie_ids is None:
            missing_id_movies = self.find_missing_external_ids(instance)
            movie_ids = [m["movie_id"] for m in missing_id_movies]

        _refresh_rows: list[list] = []
        for mid in movie_ids:
            stats["checked"] += 1
            if self.dry_run:
                _refresh_rows.append([str(mid)])
                stats["triggered"] += 1
                continue
            try:
                self.radarr_api._make_request(
                    instance,
                    "command",
                    method="POST",
                    payload={"name": "RefreshMovie", "movieIds": [mid]},
                )
                stats["triggered"] += 1
            except Exception as e:
                self.logger.log_warning(
                    f"[Metadata] Refresh failed for movie id={mid}: {e}"
                )
                stats["failed"] += 1

        self.logger.log_grid(["Id"], _refresh_rows, title="[dry_run] Would refresh metadata", cap=12)

        return stats

    # ── Full metadata scan ───────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_metadata_scan")
    def run(self, instance: str) -> dict:
        instance = self._resolve_instance(instance)
        return {
            "missing_ids_repair":       self.repair_missing_ids(instance),
            "problematic_titles_repair": self.repair_problematic_titles(instance),
            "year_mismatches":           self.find_year_mismatches(instance),
        }
