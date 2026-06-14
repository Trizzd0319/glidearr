from scripts.managers.factories.base_manager import BaseManager


from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class RadarrInstanceCacheManager(BaseManager, ComponentManagerMixin):
    """
    Manages Radarr instance-specific caches, including health, metadata, and status checks.
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

    def get_instance_metadata(self, instance):
        """
        Retrieves cached metadata for a Radarr instance.
        """
        return self.global_cache.get(f"radarr.instance.metadata.{instance}") or {}

    def get_instance_health(self, instance):
        """
        Retrieves cached health/status info for a Radarr instance.
        """
        return self.global_cache.get(f"radarr.instance.health.{instance}") or []

    def refresh_all_instances(self, instances):
        """
        Refreshes both metadata and health for all configured instance.
        """
        for instance in instances:
            self.refresh_instance_metadata(instance)
            self.refresh_instance_health(instance)

    def summarize_instance(self, instance):
        """
        Summarizes key metadata and health metrics for reporting.
        """
        metadata = self.get_instance_metadata(instance)
        health = self.get_instance_health(instance)
        error_count = sum(1 for h in health if h.get('type') == 'error')

        summary = {
            "name": metadata.get('name', 'unknown'),
            "version": metadata.get('version', 'unknown'),
            "health_issues": error_count
        }
        self.logger.log_info(f"📊 Summary for instance '{instance}': {summary}")
        return summary

    def purge_instance_cache(self, instance):
        """
        Clears all cached data for a Radarr instance.
        """
        self.global_cache.delete(f"radarr.instance.metadata.{instance}")
        self.global_cache.delete(f"radarr.instance.health.{instance}")
        self.logger.log_info(f"🗑️ Cleared all cached data for instance '{instance}'")

    def refresh_instance_metadata(self, instance):
        meta = self.radarr_api._make_request(instance, "system/status", fallback={}) if self.radarr_api else {}
        if meta:
            self.global_cache.set(f"radarr.instance.metadata.{instance}", meta, compressed=True)
            self.logger.log_info(f"✅ Cached metadata for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No metadata for {instance}")

    def refresh_instance_health(self, instance):
        health = self.radarr_api._make_request(instance, "health", fallback=[]) if self.radarr_api else []
        if health:
            self.global_cache.set(f"radarr.instance.health.{instance}", health, compressed=True)
            self.logger.log_info(f"✅ Cached health for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No health data for {instance}")
