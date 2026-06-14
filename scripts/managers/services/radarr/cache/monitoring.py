from scripts.managers.factories.base_manager import BaseManager


from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class RadarrMonitoringCacheManager(BaseManager, ComponentManagerMixin):
    """
    Manages Radarr monitoring-related caches.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrCacheManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.manager          = parent
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def refresh_monitored_movies(self, instance):
        """Refresh the cache of monitored movies for a given Radarr instance."""
        movies = self.radarr_api._make_request(instance, "movie", fallback=[]) if self.radarr_api else []
        monitored = [m for m in movies if m.get("monitored")]
        self.global_cache.set(f"radarr.{instance}.monitoring.monitored", monitored)
        self.logger.log_info(f"✅ Refreshed monitored movies cache for {instance} ({len(monitored)} monitored)")

    def get_monitored_movies(self, instance):
        """Retrieve monitored movies from cache."""
        return self.global_cache.get(f"radarr.{instance}.monitoring.monitored", default=[])

    def enforce_keep_tags(self, movie_list):
        """Ensure all movies with a 'keep' tag remain monitored."""
        for movie in movie_list:
            if "keep" in movie.get("tags", []):
                if not movie.get("monitored") and self.radarr_api:
                    self.radarr_api._make_request(
                        "default", f"movie/{movie['id']}",
                        method="PUT", payload={**movie, "monitored": True}
                    )
                    self.logger.log_info(f"🔒 Enforced 'keep' monitoring for movie: {movie['title']}")

    def patch_movie_monitoring_state(self, instance, movie_id, movie_payload, desired_state):
        """Update a single movie's monitored state."""
        if self.radarr_api:
            self.radarr_api._make_request(
                instance, f"movie/{movie_id}",
                method="PUT", payload={**movie_payload, "monitored": desired_state}
            )
        self.logger.log_info(f"🔧 Patched monitoring → {movie_id}: {desired_state}")

    def refresh_monitoring_rules(self, instance):
        rules = self.radarr_api._make_request(instance, "config/ui", fallback={}) if self.radarr_api else {}
        if rules:
            self.global_cache.set(f"radarr.monitoring.rules.{instance}", rules, compressed=True)
            self.logger.log_info(f"✅ Cached monitoring rules for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No monitoring rules for {instance}")
