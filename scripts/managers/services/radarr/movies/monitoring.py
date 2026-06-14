from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrMoviesMonitoringManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrMoviesManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.manager          = parent
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    @LoggerManager().log_function_entry
    @timeit("toggle_movie_monitoring")
    def toggle_movie_monitoring(self, movie_id: int, instance: str, monitored: bool) -> bool:
        resolved_instance = self._resolve_instance(instance)
        movie_data = self.registry.get("manager", "RadarrMoviesManager") and self.registry.get("manager", "RadarrMoviesManager").retrieval.get_movie_by_id(resolved_instance, movie_id) if self.registry.get("manager", "RadarrMoviesManager") else None
        if not movie_data:
            self.logger.log_warning(f"⚠️ Could not find movie with ID {movie_id} in {resolved_instance}.")
            return False

        movie_data["monitored"] = monitored
        response = self.radarr_api._make_request(resolved_instance, f"movie/{movie_id}", method="PUT", payload=movie_data)

        status = "monitored" if monitored else "unmonitored"
        if response:
            self.logger.log_info(f"✅ Movie {movie_id} is now {status} in {resolved_instance}.")
        else:
            self.logger.log_warning(f"⚠️ Failed to update monitoring for Movie {movie_id} in {resolved_instance}.")
        return bool(response)

    @LoggerManager().log_function_entry
    @timeit("is_monitored")
    def is_monitored(self, movie_id: int, instance: str) -> bool:
        resolved_instance = self._resolve_instance(instance)
        movie_data = self.registry.get("manager", "RadarrMoviesManager") and self.registry.get("manager", "RadarrMoviesManager").retrieval.get_movie_by_id(resolved_instance, movie_id) if self.registry.get("manager", "RadarrMoviesManager") else None
        monitored = bool(movie_data.get("monitored", False)) if movie_data else False
        self.logger.log_debug(f"🔍 Movie {movie_id} in {resolved_instance} monitored: {monitored}")
        return monitored

    @LoggerManager().log_function_entry
    @timeit("bulk_monitor_movies")
    def bulk_monitor_movies(self, movie_ids: list[int], instance: str, monitor: bool = True) -> bool:
        resolved_instance = self._resolve_instance(instance)
        if not movie_ids:
            self.logger.log_info("📭 No movie IDs provided for bulk monitoring update.")
            return False

        payload = [{"id": mid, "monitored": monitor} for mid in movie_ids]
        response = self.radarr_api._make_request(resolved_instance, "movie/editor", method="PUT", payload=payload)

        if response:
            self.logger.log_info(f"✅ Bulk updated monitoring for {len(movie_ids)} movies in {resolved_instance}.")
        else:
            self.logger.log_warning(f"⚠️ Failed to bulk update monitoring for movies in {resolved_instance}.")
        return bool(response)

    @LoggerManager().log_function_entry
    @timeit("get_monitored_movies")
    def get_monitored_movies(self, instance: str) -> list[dict]:
        resolved_instance = self._resolve_instance(instance)
        movie_list = self.radarr_api._make_request(resolved_instance, "movies") or []
        monitored = [m for m in movie_list if m.get("monitored", False)]
        self.logger.log_info(f"📦 Retrieved {len(monitored)} monitored movies from {resolved_instance}.")
        return monitored
