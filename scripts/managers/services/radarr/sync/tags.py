from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrSyncTagsManager(BaseManager, ComponentManagerMixin):
    """
    Refreshes and synchronises tag assignments across Radarr instances.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrSyncManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.keep_tagged_movies: set = set()
        self.global_tag_map:     dict = {}
        self.master_tag_set:     set = set()

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    def _get_all_instances(self) -> list:
        if self.radarr_api and hasattr(self.radarr_api, "get_all_radarr_apis"):
            try:
                return list(self.radarr_api.get_all_radarr_apis().keys())
            except Exception:
                pass
        return []

    @LoggerManager().log_function_entry
    @timeit("refresh_tags_across_instances")
    def refresh_tags_across_instances(self):
        """Rebuild tag map and keep-tagged set from all instances."""
        self.logger.log_info("Refreshing tags across all Radarr instances...")
        master_tag_set: set = set()
        unified_tag_map: dict = {}

        for instance_name in self._get_all_instances():
            resolved = self._resolve_instance(instance_name)
            movies = self.radarr_api._make_request(resolved, "movie", fallback=[]) or []
            for movie in movies:
                movie_id = movie["id"]
                tags = movie.get("tags", [])
                unified_tag_map[movie_id] = tags
                master_tag_set.update(tags)
                if "keep" in tags or "keep_forever" in tags:
                    self.keep_tagged_movies.add(movie_id)

        self.global_tag_map = unified_tag_map
        self.master_tag_set = master_tag_set
        self.logger.log_info(f"Collected master tag set: {sorted(self.master_tag_set)}")

    def is_movie_tagged_keep(self, movie_id: int) -> bool:
        return movie_id in self.keep_tagged_movies

    def get_movies_with_tag(self, tag: str) -> list:
        return [mid for mid, tags in self.global_tag_map.items() if tag in tags]

    @LoggerManager().log_function_entry
    @timeit("get_tag_labels")
    def get_tag_labels(self, instance: str) -> list:
        """Fetch all tag definitions from a Radarr instance."""
        resolved = self._resolve_instance(instance)
        cached = self.global_cache.get(f"radarr.tags.{resolved}", default=None)
        if cached is not None:
            return cached
        tags = self.radarr_api._make_request(resolved, "tag", fallback=[]) or []
        self.global_cache.set(f"radarr.tags.{resolved}", tags)
        return tags

    @LoggerManager().log_function_entry
    @timeit("ensure_tag_exists")
    def ensure_tag_exists(self, instance: str, label: str) -> int:
        """Ensure a tag with the given label exists; create if missing. Returns tag ID."""
        resolved = self._resolve_instance(instance)
        tags = self.get_tag_labels(resolved)
        existing = next((t for t in tags if t.get("label", "").lower() == label.lower()), None)
        if existing:
            return existing["id"]

        if self.dry_run:
            self.logger.log_info(f"[dry_run] Would create tag '{label}' in {resolved}")
            return -1

        result = self.radarr_api._make_request(resolved, "tag", method="POST", payload={"label": label})
        if result:
            self.global_cache.set(f"radarr.tags.{resolved}", None)  # invalidate
            return result.get("id", -1)
        return -1

    @LoggerManager().log_function_entry
    @timeit("sync_tags_across_instances")
    def sync_tags_across_instances(self):
        """Synchronise tag definitions across all instances so every instance has the same set."""
        all_instances = self._get_all_instances()
        if not all_instances:
            self.logger.log_warning("No Radarr instances found for tag sync")
            return

        # Collect all unique tag labels across every instance
        all_labels: set = set()
        for instance_name in all_instances:
            resolved = self._resolve_instance(instance_name)
            tags = self.radarr_api._make_request(resolved, "tag", fallback=[]) or []
            all_labels.update(t.get("label", "") for t in tags)

        # Ensure every instance has every label
        for instance_name in all_instances:
            resolved = self._resolve_instance(instance_name)
            existing_tags = self.get_tag_labels(resolved)
            existing_labels = {t.get("label", "").lower() for t in existing_tags}
            for label in all_labels:
                if label.lower() not in existing_labels:
                    self.ensure_tag_exists(resolved, label)
                    self.logger.log_info(f"Synced tag '{label}' to {resolved}")
