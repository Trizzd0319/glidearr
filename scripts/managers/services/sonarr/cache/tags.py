from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class SonarrCacheTagManager(BaseManager, ComponentManagerMixin):
    """
    Manages Sonarr tag-related caches.
    Handles refreshing, retrieving, and syncing tags across instances.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None,
                 sonarr_cache=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = sonarr_cache or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.register()

        parent = manager or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.manager = manager or parent
        self.logger = self.logger or getattr(parent, "logger", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {self.parent_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def format_tag_cache_key(self, instance):
        return f"sonarr/{instance}/tags.json"

    def refresh_tag_cache(self, instance):
        tags = self.sonarr_api.get_tags(instance)
        self.global_cache.set(self.format_tag_cache_key(instance), tags)
        self.logger.log_info(f"✅ Refreshed tag cache for {instance}")

    def get_tags(self, instance):
        return self.global_cache.get(self.format_tag_cache_key(instance), default=[])

    def sync_tags_across_instances(self, source_instance, target_instances):
        source_tags = self.get_tags(source_instance)
        for target in target_instances:
            self.sonarr_api.update_tags(target, source_tags)
            self.global_cache.set(self.format_tag_cache_key(target), source_tags)
            self.logger.log_info(f"🔄 Synced tags from {source_instance} → {target}")

    def add_tag_to_cache(self, instance, tag_name):
        tags = self.get_tags(instance)
        if tag_name not in tags:
            tags.append(tag_name)
            self.global_cache.set(self.format_tag_cache_key(instance), tags)
            self.logger.log_info(f"➕ Added tag '{tag_name}' to cache for {instance}")

    def remove_tag_from_cache(self, instance, tag_name):
        tags = self.get_tags(instance)
        if tag_name in tags:
            tags.remove(tag_name)
            self.global_cache.set(self.format_tag_cache_key(instance), tags)
            self.logger.log_info(f"➖ Removed tag '{tag_name}' from cache for {instance}")

    def get_keep_tag_ids(self, instance):
        return [t["id"] for t in self.get_tags(instance) if t.get("label", "").lower() == "keep"]
