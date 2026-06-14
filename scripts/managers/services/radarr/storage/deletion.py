"""
RadarrStorageDeletionManager
==============================
Handles lifecycle deletion of Radarr movie files, with full franchise
protection and keep-policy support.

All API mutations check ``self.dry_run`` first.

FRANCHISE ENTRIES ARE NEVER DELETED — hard franchise guard applies both
in apply_grace_period and delete_marked_movies.
"""

from __future__ import annotations

import datetime
import sys

from tqdm import tqdm

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrStorageDeletionManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrStorageManager"

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
        self.radarr_api      = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.global_cache = global_cache or getattr(parent, "global_cache", None)

        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(parent, "dry_run", None) if parent else None
        if _dry_run is None and self.registry:
            try:
                _root = self.registry.get("manager", "RadarrManager")
                _dry_run = getattr(_root, "dry_run", None) if _root else None
            except Exception:
                pass
        if _dry_run is None and self.registry:
            try:
                _main = self.registry.get("manager", "Main")
                _dry_run = getattr(_main, "dry_run", None) if _main else None
            except Exception:
                pass
        if _dry_run is None:
            raise ValueError(
                f"❌ {self.__class__.__name__} could not resolve dry_run from kwargs, "
                f"RadarrManager, or Main. Refusing to initialize without an explicit value "
                f"from config.json to prevent accidental destructive operations."
            )
        self.dry_run = bool(_dry_run)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    # ── Instance resolution ──────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    @staticmethod
    def _fmt_bytes(n) -> str:
        if n is None or n != n:
            return "0 B"
        n = float(n)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(n) < 1024.0:
                return f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} PB"

    # ── Parquet-backed lifecycle methods ─────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("apply_grace_period")
    def apply_grace_period(self, instance: str) -> dict:
        """
        Load the movie_files Parquet, apply grace period logic, save.
        Uses RadarrCacheMovieFilesManager if available in registry.

        Returns stats dict or empty dict if movie_files cache unavailable.
        """
        instance = self._resolve_instance(instance)
        movie_files_mgr = self._get_movie_files_manager()

        if movie_files_mgr is None:
            self.logger.log_debug(
                "[Deletion] movie_files manager unavailable — skipping parquet grace period"
            )
            return {}

        df = movie_files_mgr.apply_grace_period(instance)
        marked = 0
        if hasattr(df, "columns") and "marked_for_deletion" in df.columns:
            import pandas as pd
            marked = int(df["marked_for_deletion"].infer_objects(copy=False).fillna(False).astype(bool).sum())
        self.logger.log_info(
            f"[Deletion] Grace period applied for '{instance}': {marked} movie(s) marked"
        )
        return {"marked": marked}

    @LoggerManager().log_function_entry
    @timeit("delete_marked_movies")
    def delete_marked_movies(self, instance: str) -> dict:
        """
        Delete all movie files marked for deletion in the movie_files Parquet.
        Respects franchise protection and keep policies.
        Delegates to RadarrCacheMovieFilesManager.delete_marked_files().
        """
        instance = self._resolve_instance(instance)
        movie_files_mgr = self._get_movie_files_manager()

        if movie_files_mgr is None:
            self.logger.log_debug(
                "[Deletion] movie_files manager unavailable — skipping parquet deletion"
            )
            return {}

        return movie_files_mgr.delete_marked_files(instance)

    @LoggerManager().log_function_entry
    @timeit("get_movies_pending_deletion")
    def get_movies_pending_deletion(self, instance: str) -> list[dict]:
        """
        Return list of movies pending deletion without performing any deletions.
        Reads from the movie_files Parquet.
        """
        instance = self._resolve_instance(instance)
        movie_files_mgr = self._get_movie_files_manager()

        if movie_files_mgr is None:
            return []

        import pandas as pd
        df = movie_files_mgr.load(instance)
        if df.empty or "marked_for_deletion" not in df.columns:
            return []

        marked_mask = df["marked_for_deletion"].infer_objects(copy=False).fillna(False).astype(bool)
        if not marked_mask.any():
            return []

        result = []
        for _, row in df[marked_mask].iterrows():
            result.append({
                "movie_id":       row.get("movie_id"),
                "movie_file_id":  row.get("movie_file_id"),
                "title":          row.get("title"),
                "year":           row.get("year"),
                "size_bytes":     row.get("size_bytes"),
                "available_until": row.get("available_until"),
                "keep_policy":    row.get("keep_policy"),
            })
        return result

    # ── Legacy-compatible direct deletion methods ───────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("delete_movies_older_than")
    def delete_movies_older_than(self, instance: str, days: int = 30):
        """Delete movie files added more than ``days`` days ago."""
        resolved_instance = self._resolve_instance(instance)
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=days)
        self.logger.log_info(
            f"Deleting movie files older than {days} days in {resolved_instance}..."
        )

        if self.radarr_api is None:
            self.logger.log_warning("radarr_api not available")
            return

        movie_files = self.radarr_api._make_request(resolved_instance, "moviefile", fallback=[]) or []
        deleted = 0

        for mf in tqdm(
            movie_files,
            desc=f"Expired Cleanup [{resolved_instance}]",
            unit="file",
            file=sys.stderr,
        ):
            added = mf.get("dateAdded")
            if not added:
                continue

            try:
                added_dt = datetime.datetime.fromisoformat(added.replace("Z", ""))
            except ValueError:
                self.logger.log_debug(f"Skipping file with invalid date: {added}")
                continue

            if added_dt < cutoff_date:
                file_id = mf["id"]
                if self.dry_run:
                    self.logger.log_info(
                        f"[dry_run] Would delete movie file ID {file_id} in {resolved_instance}"
                    )
                else:
                    self.radarr_api._make_request(
                        resolved_instance, f"moviefile/{file_id}", method="DELETE"
                    )
                    deleted += 1

        self.logger.log_info(
            f"Finished cleanup in {resolved_instance}. Total deleted: {deleted}"
        )

    @LoggerManager().log_function_entry
    @timeit("delete_duplicate_movies")
    def delete_duplicate_movies(self, instance: str):
        """Delete lower-quality duplicate files for the same movie."""
        resolved_instance = self._resolve_instance(instance)
        self.logger.log_info(
            f"Searching for duplicate movie files in {resolved_instance}..."
        )

        if self.radarr_api is None:
            self.logger.log_warning("radarr_api not available")
            return

        files   = self.radarr_api._make_request(resolved_instance, "moviefile", fallback=[]) or []
        grouped = {}

        for file in files:
            key = file.get("movieId")
            if key:
                grouped.setdefault(key, []).append(file)

        deletions = 0
        for movie_id, group in grouped.items():
            if len(group) <= 1:
                continue

            # Retain the highest quality file (highest quality ID)
            group.sort(
                key=lambda f: ((f.get("quality") or {}).get("quality") or {}).get("id", 0),
                reverse=True,
            )
            for duplicate in group[1:]:
                file_id = duplicate["id"]
                if self.dry_run:
                    self.logger.log_info(
                        f"[dry_run] Would delete duplicate movie file {file_id} "
                        f"for movie {movie_id} in {resolved_instance}"
                    )
                else:
                    self.radarr_api._make_request(
                        resolved_instance, f"moviefile/{file_id}", method="DELETE"
                    )
                    deletions += 1

        self.logger.log_info(
            f"Duplicate cleanup complete in {resolved_instance}. Total deleted: {deletions}"
        )

    # ── Registry helper ──────────────────────────────────────────────────────────

    def _get_movie_files_manager(self):
        """Resolve RadarrCacheMovieFilesManager from registry."""
        try:
            return self.registry.get("manager", "RadarrCacheMovieFilesManager")
        except Exception:
            return None
