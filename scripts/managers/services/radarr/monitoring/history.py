from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrMonitoringHistoryManager(BaseManager, ComponentManagerMixin):
    """
    Tracks Radarr movie acquisition history and download events.
    Detects stuck grabs, repeated failures, and import events for
    lifecycle and monitoring decision support.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrMonitoringManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    @LoggerManager().log_function_entry
    @timeit("get_history")
    def get_history(self, instance: str, page_size: int = 100) -> list:
        """Fetch recent Radarr history events (grabs, imports, failures)."""
        resolved = self._resolve_instance(instance)
        cached = self.global_cache.get(f"radarr.history.{resolved}", default=None)
        if cached is not None:
            return cached

        history = self.radarr_api._make_request(
            resolved,
            f"history?pageSize={page_size}&sortKey=date&sortDir=desc",
            fallback={}
        ) or {}
        records = history.get("records", []) if isinstance(history, dict) else []
        self.global_cache.set(f"radarr.history.{resolved}", records)
        self.logger.log_info(f"Fetched {len(records)} history events from {resolved}")
        return records

    @LoggerManager().log_function_entry
    @timeit("get_movie_history")
    def get_movie_history(self, instance: str, movie_id: int) -> list:
        """Fetch all history events for a specific movie."""
        resolved = self._resolve_instance(instance)
        history = self.radarr_api._make_request(
            resolved,
            f"history/movie?movieId={movie_id}",
            fallback=[],
        ) or []
        return history

    @LoggerManager().log_function_entry
    @timeit("find_stuck_grabs")
    def find_stuck_grabs(self, instance: str, stale_hours: int = 6) -> list:
        """
        Identify movies that were grabbed but never imported within stale_hours.
        Returns a list of {movie_id, title, grabbed_at} dicts.
        """
        from datetime import datetime, timezone, timedelta
        resolved = self._resolve_instance(instance)
        records = self.get_history(resolved)

        grab_times: dict = {}
        imported_ids: set = set()

        for r in records:
            movie_id  = r.get("movieId")
            event     = r.get("eventType", "")
            date_str  = r.get("date", "")
            if not movie_id:
                continue
            if event == "grabbed" and movie_id not in grab_times:
                try:
                    grabbed_at = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    grab_times[movie_id] = grabbed_at
                except ValueError:
                    pass
            elif event in ("downloadFolderImported", "movieFileImported"):
                imported_ids.add(movie_id)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)
        stuck = []
        for movie_id, grabbed_at in grab_times.items():
            if movie_id not in imported_ids and grabbed_at < cutoff:
                # Fetch title from cache or API
                movie = self.radarr_api._make_request(resolved, f"movie/{movie_id}", fallback={}) or {}
                stuck.append({
                    "movie_id":   movie_id,
                    "title":      movie.get("title", f"movie/{movie_id}"),
                    "grabbed_at": grabbed_at.isoformat(),
                })

        self.logger.log_info(f"Found {len(stuck)} stuck grabs in {resolved} (stale > {stale_hours}h)")
        return stuck

    @LoggerManager().log_function_entry
    @timeit("find_repeated_failures")
    def find_repeated_failures(self, instance: str, failure_threshold: int = 3) -> list:
        """
        Return movies that have failed to download/import >= failure_threshold times.
        """
        resolved = self._resolve_instance(instance)
        records = self.get_history(resolved)

        failure_counts: dict = {}
        for r in records:
            movie_id = r.get("movieId")
            event    = r.get("eventType", "")
            if movie_id and event in ("downloadFailed", "importFailed", "grabbed"):
                failure_counts[movie_id] = failure_counts.get(movie_id, 0) + 1

        repeated = []
        for movie_id, count in failure_counts.items():
            if count >= failure_threshold:
                movie = self.radarr_api._make_request(resolved, f"movie/{movie_id}", fallback={}) or {}
                repeated.append({
                    "movie_id":      movie_id,
                    "title":         movie.get("title", f"movie/{movie_id}"),
                    "failure_count": count,
                })

        self.logger.log_info(
            f"Found {len(repeated)} movies with >= {failure_threshold} failures in {resolved}"
        )
        return repeated

    @LoggerManager().log_function_entry
    @timeit("get_recent_imports")
    def get_recent_imports(self, instance: str, limit: int = 50) -> list:
        """Return the most recently imported movies."""
        resolved = self._resolve_instance(instance)
        records  = self.get_history(resolved)
        imports  = [
            r for r in records
            if r.get("eventType") in ("downloadFolderImported", "movieFileImported")
        ]
        return imports[:limit]
