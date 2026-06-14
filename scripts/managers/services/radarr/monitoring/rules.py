from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrMonitoringRulesManager(BaseManager, ComponentManagerMixin):
    """
    Applies tag-aware and rating-based monitoring rules to Radarr movies.
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
    @timeit("apply_monitoring_rules")
    def apply_monitoring_rules(self, movie_data: list, instance: str):
        """
        Apply intelligent monitoring rules to a list of movies.
        Movies with strong ratings (votes >= 1000 and rating >= 8.0) will be monitored.
        Movies already monitored or tagged 'keep' are skipped.
        """
        resolved = self._resolve_instance(instance)
        self.logger.log_info(f"Applying rating-based monitoring rules to {len(movie_data)} movies in {resolved}...")

        never_monitor_tags = set(self.config.get("never_unmonitor_tags", []))
        tag_labels = self.global_cache.get(f"radarr.tags.{resolved}", default=[]) or []

        for movie in movie_data:
            tag_ids = set(movie.get("tags", []))
            # Skip if tagged keep
            tag_names = {t["label"].lower() for t in tag_labels if t["id"] in tag_ids}
            if tag_names & never_monitor_tags:
                self.logger.log_debug(f"Skipping '{movie['title']}' — tagged keep")
                continue

            if movie.get("monitored"):
                self.logger.log_debug(f"Already monitored: {movie['title']}")
                continue

            if self.should_monitor_due_to_strong_ratings(movie):
                if self.dry_run:
                    self.logger.log_info(f"[dry_run] Would auto-monitor '{movie['title']}'")
                    continue
                try:
                    payload = dict(movie)
                    payload["monitored"] = True
                    self.radarr_api._make_request(resolved, f"movie/{movie['id']}", method="PUT", payload=payload)
                    self.logger.log_info(f"Auto-monitored '{movie['title']}' — strong public rating")
                except Exception as e:
                    self.logger.log_warning(f"Failed to monitor '{movie['title']}': {e}")

        self.logger.log_info("Monitoring rule application complete.")

    def should_auto_unmonitor(self, movie: dict, instance: str) -> bool:
        try:
            ratings = movie.get("ratings", {})
            for source, data in ratings.items():
                if data.get("votes", 0) >= 500 and data.get("value", 10) <= 4.0:
                    self.logger.log_info(
                        f"Unmonitoring '{movie['title']}' — low rating from {source}: {data['value']}"
                    )
                    return True
        except Exception as e:
            self.logger.log_warning(f"Failed to evaluate ratings for '{movie.get('title', 'unknown')}': {e}")

        tag_labels = self.global_cache.get(f"radarr.tags.{instance}", default=[]) or []
        tag_ids = movie.get("tags", [])
        blacklist_keywords = {"lowest", "worst", "bottom", "avoid", "trash"}
        for tag_id in tag_ids:
            tag_label = next((t["label"].lower() for t in tag_labels if t["id"] == tag_id), None)
            if tag_label and any(keyword in tag_label for keyword in blacklist_keywords):
                self.logger.log_info(f"Unmonitoring '{movie['title']}' — matched blacklist tag: {tag_label}")
                return True

        return False

    def should_monitor_due_to_strong_ratings(self, movie: dict) -> bool:
        try:
            ratings = movie.get("ratings", {})
            for source, data in ratings.items():
                if data.get("votes", 0) >= 1000 and data.get("value", 0) >= 8.0:
                    self.logger.log_debug(
                        f"'{movie['title']}' meets rating rule ({source} {data['value']} with {data['votes']} votes)"
                    )
                    return True
        except Exception as e:
            self.logger.log_warning(f"Rating check failed for {movie.get('title')}: {e}")
        return False
