"""
RadarrRepairOrphansManager
============================
Detects and repairs orphaned / stale movie records in Radarr.

Stale records:  Radarr believes a movie is missing (wanted/missing) —
                either the file was deleted externally or an import failed.
                Repair: RescanMovie to re-detect existing files, then
                RefreshMovie for ones Radarr still can't find.

Untracked files: Folders on disk not linked to any Radarr movie entry.
                Repair: DownloadedMoviesScan to trigger import.
"""

from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrRepairOrphansManager(BaseManager, ComponentManagerMixin):
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

    # ── Helpers ──────────────────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    def _get_movies(self, instance: str) -> list[dict]:
        """Prefer global_cache to avoid redundant API calls."""
        if self.global_cache:
            cached = self.global_cache.get(f"radarr.movies.{instance}.full")
            if cached:
                return cached
        return self.radarr_api._make_request(instance, "movie", fallback=[]) or []

    # ── Stale Radarr records ─────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_stale_records")
    def find_stale_records(self, instance: str) -> list[dict]:
        """
        Find Radarr movie records that are monitored but have no file.

        Paginates through ALL pages of /wanted/missing so large libraries
        are fully covered (previous impl was capped at pageSize=500).

        Returns list of {movie_id, title, year, path, tmdb_id}
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            self.logger.log_warning("radarr_api not available")
            return []

        results: list[dict] = []
        page      = 1
        page_size = 250

        while True:
            data = self.radarr_api._make_request(
                instance,
                f"wanted/missing?pageSize={page_size}&page={page}",
                fallback={},
            ) or {}

            records = data.get("records") or []
            if not records:
                break

            for m in records:
                if m.get("monitored"):
                    results.append({
                        "movie_id": m.get("id"),
                        "title":    m.get("title"),
                        "year":     m.get("year"),
                        "path":     m.get("path"),
                        "tmdb_id":  m.get("tmdbId"),
                    })

            # Check if we've reached the last page
            total_records = data.get("totalRecords", 0)
            if page * page_size >= total_records:
                break
            page += 1

        self.logger.log_info(
            f"[Orphans] Stale Radarr records in '{instance}': {len(results)} movie(s)"
        )
        return results

    # ── Repair: stale records ─────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("repair_stale_records")
    def repair_stale_records(self, instance: str) -> dict:
        """
        Attempt to repair stale records in two passes:

        Pass 1 — RescanMovie (batch):
            Re-scans the folder for each stale movie. If the file exists on
            disk but Radarr lost track of it, this re-imports it silently.

        Pass 2 — RefreshMovie (batch):
            For anything still missing after rescan, refresh metadata from
            TMDb. Clears stuck states, updates availability flags, and lets
            the anomaly triage decide whether to search or unmonitor.

        Both passes use batched commands to avoid hammering Radarr.
        """
        instance = self._resolve_instance(instance)
        stats = {
            "checked":         0,
            "rescan_queued":   0,
            "refresh_queued":  0,
            "failed":          0,
        }

        if self.radarr_api is None:
            return stats

        stale = self.find_stale_records(instance)
        if not stale:
            return stats

        stale_ids = [m["movie_id"] for m in stale if m.get("movie_id")]
        stats["checked"] = len(stale_ids)

        BATCH = 100

        # ── Pass 1: RescanMovie ───────────────────────────────────────────────────
        for i in range(0, len(stale_ids), BATCH):
            batch = stale_ids[i : i + BATCH]
            if self.dry_run:
                self.logger.log_info(
                    f"  [dry_run] Would RescanMovie for {len(batch)} movie(s) "
                    f"(batch {i // BATCH + 1})"
                )
                stats["rescan_queued"] += len(batch)
                continue
            # _make_request returns fallback (None) on failure rather than
            # raising. SQLITE_BUSY retry/backoff + per-instance write serialisation
            # are handled centrally in _make_request, so no inter-batch sleep is
            # needed (it only waits when actually contended).
            resp = self.radarr_api._make_request(
                instance, "command", method="POST",
                payload={"name": "RescanMovie", "movieIds": batch},
                fallback=None,
            )
            if resp is None:
                self.logger.log_warning(
                    f"  ⚠️ RescanMovie batch failed for {len(batch)} movie(s)"
                )
                stats["failed"] += len(batch)
            else:
                stats["rescan_queued"] += len(batch)
                self.logger.log_info(
                    f"  🔍 RescanMovie queued: {len(batch)} movie(s) "
                    f"(batch {i // BATCH + 1}/{(len(stale_ids) - 1) // BATCH + 1})"
                )

        # ── Pass 2: RefreshMovie ─────────────────────────────────────────────────
        # Refreshing clears stale metadata, updates isAvailable/status flags,
        # and ensures the anomaly triage works with current data next run.
        for i in range(0, len(stale_ids), BATCH):
            batch = stale_ids[i : i + BATCH]
            if self.dry_run:
                self.logger.log_info(
                    f"  [dry_run] Would RefreshMovie for {len(batch)} movie(s) "
                    f"(batch {i // BATCH + 1})"
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
                self.logger.log_info(
                    f"  🔄 RefreshMovie queued: {len(batch)} movie(s) "
                    f"(batch {i // BATCH + 1}/{(len(stale_ids) - 1) // BATCH + 1})"
                )

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_info(
            f"[Orphans] {prefix}Stale record repair for '{instance}': "
            f"{stats['checked']} checked | {stats['rescan_queued']} rescanned | "
            f"{stats['refresh_queued']} refreshed | {stats['failed']} failed"
        )
        return stats

    # ── Orphaned movie files via disk scan ────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_untracked_files")
    def find_untracked_files(self, instance: str) -> list[dict]:
        """
        Detect movie folders on disk that Radarr has not imported.
        Uses unmappedFolders from each root folder's metadata.

        Returns list of {root_folder, folder_path, folder_name}
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            return []

        try:
            root_folders = self.radarr_api._make_request(
                instance, "rootfolder", fallback=[]
            ) or []
        except Exception as e:
            self.logger.log_warning(f"[Orphans] Could not fetch root folders: {e}")
            return []

        untracked: list[dict] = []
        for rf in root_folders:
            path     = rf.get("path", "")
            unmapped = rf.get("unmappedFolders") or []
            for folder in unmapped:
                untracked.append({
                    "root_folder": path,
                    "folder_path": folder.get("path"),
                    "folder_name": folder.get("name"),
                })

        self.logger.log_info(
            f"[Orphans] Untracked folder(s) in '{instance}': {len(untracked)}"
        )
        return untracked

    # ── Repair: import untracked files ───────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("repair_import_untracked")
    def repair_import_untracked(self, instance: str) -> dict:
        """
        Trigger DownloadedMoviesScan for every untracked folder so Radarr
        attempts to import them automatically.
        """
        instance = self._resolve_instance(instance)
        stats    = {"checked": 0, "triggered": 0, "failed": 0}

        if self.radarr_api is None:
            return stats

        untracked = self.find_untracked_files(instance)
        if not untracked:
            return stats

        # A 1.8k-folder library otherwise dumped one grid row per untracked folder (a
        # multi-thousand-line wall). Walk with a tqdm bar (stderr) instead; the post-loop
        # summary already reports checked|triggered|failed, so the per-row grid was pure
        # noise. Per-folder failures are still logged (exception-only, not spammy).
        from scripts.support.utilities.progress.tqdm_wrapper import tqdm
        for folder in tqdm(untracked, total=len(untracked),
                           desc=f"📁 Untracked import [{instance}]", unit="folder"):
            stats["checked"] += 1
            path = folder.get("folder_path") or folder.get("root_folder")
            if not path:
                continue

            if self.dry_run:
                stats["triggered"] += 1
                continue

            try:
                self.radarr_api._make_request(
                    instance, "command", method="POST",
                    payload={"name": "DownloadedMoviesScan", "path": path},
                )
                stats["triggered"] += 1
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ Import scan failed for '{path}': {e}"
                )
                stats["failed"] += 1

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_info(
            f"[Orphans] {prefix}Untracked import for '{instance}': "
            f"{stats['checked']} checked | {stats['triggered']} triggered | "
            f"{stats['failed']} failed"
        )
        return stats

    # ── Repair: trigger import scan (legacy single-path) ────────────────────────

    @LoggerManager().log_function_entry
    @timeit("repair_trigger_import")
    def repair_trigger_import(self, instance: str, folder_path: str) -> dict:
        """Trigger a manual import scan for a specific folder path."""
        instance = self._resolve_instance(instance)
        stats    = {"triggered": 0, "failed": 0}

        if self.radarr_api is None:
            return stats

        if self.dry_run:
            self.logger.log_info(
                f"[dry_run] Would trigger DownloadedMoviesScan for '{folder_path}'"
            )
            stats["triggered"] = 1
            return stats

        try:
            self.radarr_api._make_request(
                instance, "command", method="POST",
                payload={"name": "DownloadedMoviesScan", "path": folder_path},
            )
            self.logger.log_info(
                f"[Orphans] Triggered DownloadedMoviesScan for '{folder_path}'"
            )
            stats["triggered"] = 1
        except Exception as e:
            self.logger.log_warning(
                f"[Orphans] Import trigger failed for '{folder_path}': {e}"
            )
            stats["failed"] = 1

        return stats

    # ── Repair: full library rescan ──────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("repair_rescan_all")
    def repair_rescan_all(self, instance: str) -> dict:
        """Trigger a full library rescan (all movies, no filter)."""
        instance = self._resolve_instance(instance)
        stats    = {"triggered": 0, "failed": 0}

        if self.radarr_api is None:
            return stats

        if self.dry_run:
            self.logger.log_info(
                f"[dry_run] Would trigger RescanMovie (all) for '{instance}'"
            )
            stats["triggered"] = 1
            return stats

        try:
            self.radarr_api._make_request(
                instance, "command", method="POST",
                payload={"name": "RescanMovie"},
            )
            self.logger.log_info(
                f"[Orphans] Triggered full library rescan for '{instance}'"
            )
            stats["triggered"] = 1
        except Exception as e:
            self.logger.log_warning(f"[Orphans] Rescan trigger failed: {e}")
            stats["failed"] = 1

        return stats

    # ── Full orphan scan + repair ────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_orphan_scan")
    def run(self, instance: str) -> dict:
        instance = self._resolve_instance(instance)
        return {
            "stale_records":    self.repair_stale_records(instance),
            "untracked_import": self.repair_import_untracked(instance),
        }
