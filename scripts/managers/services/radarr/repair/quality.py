"""
RadarrRepairQualityManager
============================
Detects quality issues in Radarr:
- Movies with quality_cutoff_not_met (eligible for upgrade)
- Duplicate movie files for the same tmdbId
- Movies below the minimum resolution threshold for an instance
"""

from __future__ import annotations

from collections import defaultdict

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrRepairQualityManager(BaseManager, ComponentManagerMixin):
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

    # ── Instance resolution ──────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    # ── Cutoff not met ───────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_cutoff_not_met")
    def find_cutoff_not_met(self, instance: str) -> list[dict]:
        """
        Find movies where the quality cutoff has not been met.
        These are candidates for upgrade searches.

        Returns list of {movie_id, title, year, quality_name, quality_profile_id}
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            self.logger.log_warning("radarr_api not available")
            return []

        movies  = self.radarr_api._make_request(instance, "movie", fallback=[]) or []
        results = []
        for m in movies:
            if not m.get("hasFile"):
                continue
            mf = m.get("movieFile") or {}
            if mf.get("qualityCutoffNotMet"):
                qq = ((mf.get("quality") or {}).get("quality") or {})
                results.append({
                    "movie_id":          m.get("id"),
                    "title":             m.get("title"),
                    "year":              m.get("year"),
                    "quality_name":      qq.get("name"),
                    "quality_profile_id": m.get("qualityProfileId"),
                })

        self.logger.log_info(
            f"[Quality] Cutoff not met in '{instance}': {len(results)} movie(s)"
        )
        return results

    # ── Duplicate detection ──────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_duplicate_movies")
    def find_duplicate_movies(self, instance: str) -> list[dict]:
        """
        Detect movies with the same tmdbId appearing more than once in an instance.
        This can happen after imports, merges, or accidental re-adds.

        Returns list of groups: {tmdb_id, title, duplicates: [movie records]}
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            return []

        movies   = self.radarr_api._make_request(instance, "movie", fallback=[]) or []
        by_tmdb: dict[int, list[dict]] = defaultdict(list)

        for m in movies:
            tmdb_id = m.get("tmdbId")
            if tmdb_id:
                by_tmdb[tmdb_id].append(m)

        duplicates = []
        for tmdb_id, group in by_tmdb.items():
            if len(group) > 1:
                duplicates.append({
                    "tmdb_id":    tmdb_id,
                    "title":      group[0].get("title"),
                    "duplicates": [
                        {
                            "movie_id": g.get("id"),
                            "title":    g.get("title"),
                            "year":     g.get("year"),
                            "path":     g.get("path"),
                            "has_file": g.get("hasFile"),
                        }
                        for g in group
                    ],
                })

        self.logger.log_info(
            f"[Quality] Duplicate movies in '{instance}': {len(duplicates)} tmdbId(s) with duplicates"
        )
        return duplicates

    # ── Upgrade suggestions ──────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("suggest_upgrades")
    def suggest_upgrades(self, instance: str, limit: int = 50, candidates: list | None = None) -> list[dict]:
        """
        Return the top N movies eligible for quality upgrade, sorted by
        popularity desc (most popular first — maximises upgrade value).

        Pass ``candidates`` to reuse an already-fetched find_cutoff_not_met result
        and avoid a redundant API call.

        Returns list of {movie_id, title, year, current_quality, popularity}
        """
        instance  = self._resolve_instance(instance)
        if candidates is None:
            candidates = self.find_cutoff_not_met(instance)

        if not candidates:
            return []

        if self.radarr_api is None:
            return candidates[:limit]

        # Enrich with popularity from movie data
        movies     = self.radarr_api._make_request(instance, "movie", fallback=[]) or []
        pop_by_id  = {m["id"]: m.get("popularity", 0) for m in movies if m.get("id")}

        for c in candidates:
            c["popularity"] = pop_by_id.get(c["movie_id"], 0)

        # Sort by popularity descending, then by title for stable ordering
        candidates.sort(key=lambda c: (-float(c.get("popularity") or 0), c.get("title", "")))
        return candidates[:limit]

    # ── Repair: trigger upgrade search ──────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("repair_trigger_upgrades")
    def repair_trigger_upgrades(
        self,
        instance: str,
        movie_ids: list[int] | None = None,
        limit: int = 20,
    ) -> dict:
        """
        Trigger upgrade searches for movies with quality_cutoff_not_met.
        If movie_ids is None, uses suggest_upgrades() to find candidates.

        Returns stats dict.
        """
        instance = self._resolve_instance(instance)
        stats = {"checked": 0, "triggered": 0, "failed": 0}

        if self.radarr_api is None:
            return stats

        if movie_ids is None:
            suggestions  = self.suggest_upgrades(instance, limit=limit)
            movie_ids    = [s["movie_id"] for s in suggestions]

        _rows = []
        for mid in movie_ids:
            stats["checked"] += 1
            if self.dry_run:
                _rows.append([str(mid)])
                stats["triggered"] += 1
                continue
            try:
                self.radarr_api._make_request(
                    instance,
                    "command",
                    method="POST",
                    payload={"name": "MoviesSearch", "movieIds": [mid]},
                )
                _rows.append([str(mid)])
                stats["triggered"] += 1
            except Exception as e:
                self.logger.log_warning(
                    f"[Quality] Upgrade search failed for movie id={mid}: {e}"
                )
                stats["failed"] += 1

        _title = f"Upgrade searches in '{instance}'"
        if self.dry_run:
            _title = f"[dry_run] {_title}"
        self.logger.log_grid(["Id"], _rows, title=_title, cap=12)

        return stats

    # ── Full quality scan ────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_quality_scan")
    def run(self, instance: str) -> dict:
        instance = self._resolve_instance(instance)
        cutoff_not_met = self.find_cutoff_not_met(instance)
        return {
            "cutoff_not_met":      cutoff_not_met,
            "duplicates":          self.find_duplicate_movies(instance),
            "upgrade_suggestions": self.suggest_upgrades(instance, candidates=cutoff_not_met),
        }
