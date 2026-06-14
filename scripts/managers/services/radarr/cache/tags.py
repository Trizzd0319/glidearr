from scripts.managers.factories.base_manager import BaseManager


from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class RadarrTagCacheManager(BaseManager, ComponentManagerMixin):
    """
    Manages Radarr tag-related caches.
    Handles refreshing, retrieving, and syncing tags across instances.
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

    def refresh_tag_cache(self, instance):
        tags = self.radarr_api._make_request(instance, "tag", fallback=[]) if self.radarr_api else []
        if tags:
            self.global_cache.set(f"radarr.tags.{instance}", tags, compressed=True)
            self.logger.log_info(f"✅ Cached tags for {instance} ({len(tags)} total)")
        else:
            self.logger.log_warning(f"⚠️ No tags retrieved for {instance}")

    def get_tags(self, instance):
        """Retrieve cached tags for an instance."""
        return self.global_cache.get(f"radarr.tags.{instance}", default=[])

    def sync_tags_across_instances(self, source_instance, target_instances):
        """Sync tags from a source instance to one or more targets."""
        source_tags = self.get_tags(source_instance)
        for target in target_instances:
            try:
                if self.radarr_api:
                    self.radarr_api._make_request(target, "tag", method="PUT", payload=source_tags)
                self.global_cache.set(f"radarr.tags.{target}", source_tags)
                self.logger.log_info(f"🔄 Synced tags from {source_instance} → {target}")
            except Exception as e:
                self.logger.log_error(f"❌ Failed to sync tags to {target}: {e}")

    def add_tag_to_cache(self, instance, tag_name):
        """Add a tag to the cached tag list."""
        tags = self.get_tags(instance)
        if tag_name not in [t.get("label") for t in tags]:
            new_tag = {"id": None, "label": tag_name}
            tags.append(new_tag)
            self.global_cache.set(f"radarr.tags.{instance}", tags)
            self.logger.log_info(f"➕ Added tag '{tag_name}' to cache for {instance}")

    def remove_tag_from_cache(self, instance, tag_name):
        """Remove a tag from the cached tag list."""
        tags = self.get_tags(instance)
        updated = [t for t in tags if t.get("label") != tag_name]
        if len(tags) != len(updated):
            self.global_cache.set(f"radarr.tags.{instance}", updated)
            self.logger.log_info(f"➖ Removed tag '{tag_name}' from cache for {instance}")

    def get_keep_tag_ids(self, instance):
        """Return the ID of tags labeled 'keep'."""
        return [t["id"] for t in self.get_tags(instance) if t.get("label", "").lower() == "keep"]
