from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrMonitoringMoviesManager(BaseManager, ComponentManagerMixin):
    """
    Manages movie-level monitoring control for Radarr instances.
    Handles batch monitoring, toggles, and tag-aware logic.
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

    def get_all_movies(self, instance):
        resolved = self._resolve_instance(instance)
        return self.radarr_api._make_request(resolved, "movie", fallback=[]) or []

    def update_movie_monitoring(self, movie_id, instance, monitored: bool):
        resolved = self._resolve_instance(instance)
        movie = self.radarr_api._make_request(resolved, f"movie/{movie_id}", fallback=None)
        if not movie:
            return False
        movie["monitored"] = monitored
        return bool(self.radarr_api._make_request(resolved, f"movie/{movie_id}", method="PUT", payload=movie))

    def get_monitored_movies(self, instance):
        return [m for m in self.get_all_movies(instance) if m.get("monitored")]

    def get_unmonitored_movies(self, instance):
        return [m for m in self.get_all_movies(instance) if not m.get("monitored")]

    @LoggerManager().log_function_entry
    @timeit("toggle_movie_monitoring")
    def toggle_movie_monitoring(self, movie_id: int, instance: str, monitored: bool) -> bool:
        resolved = self._resolve_instance(instance)
        movie = self.radarr_api._make_request(resolved, f"movie/{movie_id}", fallback=None)
        if not movie:
            self.logger.log_warning(f"Could not find movie {movie_id} in {resolved}")
            return False

        movie["monitored"] = monitored
        response = self.radarr_api._make_request(resolved, f"movie/{movie_id}", method="PUT", payload=movie)
        status = "monitored" if monitored else "unmonitored"
        if response:
            self.logger.log_info(f"Movie {movie_id} is now {status} in {resolved}")
        else:
            self.logger.log_warning(f"Failed to update monitoring for Movie {movie_id} in {resolved}")
        return bool(response)

    @LoggerManager().log_function_entry
    @timeit("bulk_monitor_movies")
    def bulk_monitor_movies(self, movie_ids: list, instance: str, monitor: bool = True) -> bool:
        resolved = self._resolve_instance(instance)
        if not movie_ids:
            return False

        payload = {"movieIds": movie_ids, "monitored": monitor}
        response = self.radarr_api._make_request(resolved, "movie/editor", method="PUT", payload=payload)
        if response:
            self.logger.log_info(f"Bulk updated monitoring for {len(movie_ids)} movies in {resolved}")
        else:
            self.logger.log_warning(f"Failed to bulk update monitoring for movies in {resolved}")
        return bool(response)

    @LoggerManager().log_function_entry
    @timeit("batch_update_monitoring")
    def batch_update_monitoring(self, instance, updates: dict):
        resolved = self._resolve_instance(instance)
        failed = []

        for movie_id, state in updates.items():
            try:
                self.toggle_movie_monitoring(movie_id, resolved, state)
                self.logger.log_debug(f"Updated movie {movie_id} to monitored={state}")
            except Exception as e:
                failed.append(movie_id)
                self.logger.log_warning(f"Failed to update movie {movie_id}: {e}")

        if failed:
            self.logger.log_error(f"Rolling back {len(failed)} movie updates due to errors")
            for mid in failed:
                try:
                    original_state = not updates[mid]
                    self.toggle_movie_monitoring(mid, resolved, original_state)
                    self.logger.log_info(f"Rolled back movie {mid} to monitored={original_state}")
                except Exception as e:
                    self.logger.log_warning(f"Failed to rollback movie {mid}: {e}")

    @LoggerManager().log_function_entry
    @timeit("batch_unmonitor_downloaded_if_cutoff_met")
    def batch_unmonitor_downloaded_if_cutoff_met(self, instance):
        resolved = self._resolve_instance(instance)
        movies = self.get_all_movies(resolved)
        never_unmonitor = self.config.get("never_unmonitor_tags", [])
        to_unmonitor_ids = []

        for movie in movies:
            if not movie.get("monitored") or not movie.get("hasFile"):
                continue
            if any(tag in never_unmonitor for tag in movie.get("tags", [])):
                continue
            if movie.get("qualityCutoffNotMet", False) is False and movie.get("hasFile"):
                to_unmonitor_ids.append(movie["id"])

        if to_unmonitor_ids:
            self.bulk_monitor_movies(to_unmonitor_ids, resolved, monitor=False)
            self.logger.log_info(f"Batch unmonitored {len(to_unmonitor_ids)} movies (cutoff met)")
        else:
            self.logger.log_info("No movies met cutoff for unmonitoring")

    @LoggerManager().log_function_entry
    @timeit("batch_monitor_cutoff_unmet")
    def batch_monitor_cutoff_unmet(self, instance: str) -> None:
        """Re-monitor movies whose quality cutoff is still unmet."""
        resolved = self._resolve_instance(instance)
        movies = self.get_all_movies(resolved)
        to_monitor_ids = [
            m["id"] for m in movies
            if not m.get("monitored") and m.get("qualityCutoffNotMet", False)
        ]
        if to_monitor_ids:
            self.bulk_monitor_movies(to_monitor_ids, resolved, monitor=True)
            self.logger.log_info(f"Batch monitored {len(to_monitor_ids)} movies (cutoff unmet) in {resolved}")
        else:
            self.logger.log_info("No movies needed to be monitored for cutoff")

    def auto_unmonitor_downloaded(self, instance: str) -> None:
        """Convenience alias: unmonitor downloaded movies whose cutoff is met."""
        self.batch_unmonitor_downloaded_if_cutoff_met(instance)

    def monitor_movies_with_unmet_cutoff(self, instance: str) -> None:
        """Convenience alias: re-monitor movies whose quality cutoff is still unmet."""
        self.batch_monitor_cutoff_unmet(instance)

    def should_auto_monitor(self, movie):
        ratings = movie.get("ratings", {})
        for source, data in ratings.items():
            if data.get("votes", 0) >= 1000 and data.get("value", 0) >= 8.0:
                self.logger.log_info(
                    f"Monitoring '{movie['title']}' — rating {data['value']} from {source}"
                )
                return True
        return False

    def should_auto_unmonitor(self, movie, instance: str):
        try:
            ratings = movie.get("ratings", {})
            for source, data in ratings.items():
                if data.get("votes", 0) >= 500 and data.get("value", 10) <= 4.0:
                    return True

            tag_ids = movie.get("tags", [])
            tag_labels = self.global_cache.get(f"radarr.tags.{instance}", default=[]) or []
            blacklist_keywords = {"lowest", "worst", "bottom", "avoid", "trash"}
            for tag_id in tag_ids:
                tag_label = next((t["label"].lower() for t in tag_labels if t["id"] == tag_id), None)
                if tag_label and any(keyword in tag_label for keyword in blacklist_keywords):
                    return True
        except Exception as e:
            self.logger.log_warning(f"Failed to evaluate unmonitor criteria for '{movie.get('title')}': {e}")

        return False
