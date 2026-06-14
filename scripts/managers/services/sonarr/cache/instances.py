from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class SonarrCacheInstanceManager(BaseManager, ComponentManagerMixin):
    """
    Manages Sonarr instance-specific caches, including health, metadata, and status checks.
    """

    def __init__(self, logger=None, config=None, global_cache=None, sonarr_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrCache"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = sonarr_cache or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = manager or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {self.__class__.__name__} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")
        self.register()

    def refresh_instance_metadata(self, instance):
        metadata = self.sonarr_api.get_instance_metadata(instance)
        if metadata:
            self.global_cache.set(f"sonarr.instance.metadata.{instance}", metadata)
            self.logger.log_info(f"✅ Refreshed metadata cache for instance '{instance}'")
        else:
            self.logger.log_warning(f"⚠️ No metadata retrieved for instance '{instance}'")

    def get_instance_metadata(self, instance):
        return self.global_cache.get(f"sonarr.instance.metadata.{instance}", default={})

    def refresh_instance_health(self, instance):
        health = self.sonarr_api.get_instance_health(instance)
        if health:
            self.global_cache.set(f"sonarr.instance.health.{instance}", health)
            self.logger.log_info(f"✅ Refreshed health cache for instance '{instance}'")
        else:
            self.logger.log_warning(f"⚠️ No health data retrieved for instance '{instance}'")

    def get_instance_health(self, instance):
        return self.global_cache.get(f"sonarr.instance.health.{instance}", default=[])

    def refresh_all_instances(self, instances):
        for instance in instances:
            self.refresh_instance_metadata(instance)
            self.refresh_instance_health(instance)

    def summarize_instance(self, instance):
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
        self.global_cache.delete(f"sonarr.instance.metadata.{instance}")
        self.global_cache.delete(f"sonarr.instance.health.{instance}")
        self.logger.log_info(f"🗑️ Cleared all cached data for instance '{instance}'")

    def get_all_instance_names(self):
        cfg = self.config.get("sonarr_instances", {}) or {}
        return [k for k in cfg.keys() if k != "default_instance"]
