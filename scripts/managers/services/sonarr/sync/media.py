from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths as Paths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSyncMediaManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrStorage"
        class_name = self.__class__.__name__

        if class_name.endswith("Manager"):
            self.parent_name = class_name.replace("Manager", "")
        else:
            self.parent_name = class_name

        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = kwargs.get("manager") or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")


    @LoggerManager().log_function_entry
    @timeit("sync_media_management_settings")
    def sync_media_management_settings(self, settings: dict):
        """
        Syncs provided media management settings to all Sonarr instance.
        Args:
            settings (dict): The media management settings to apply.
        """
        sonarr_instances = self.config.get("sonarr_instances", {})
        if not sonarr_instances:
            self.logger.log_warning("⚠️ No Sonarr instance found in config. Aborting media sync.")
            return

        for instance in sonarr_instances:
            try:
                current = self.sonarr_api._make_request(instance, "config/mediamanagement")
                if not isinstance(current, dict):
                    self.logger.log_warning(f"⚠️ Invalid media management structure from {instance}. Skipping.")
                    continue

                current.update(settings)
                result = self.sonarr_api._make_request(instance, "config/mediamanagement", method="PUT", payload=current)

                if result:
                    self.logger.log_info(f"✅ Media management settings synced for {instance}.")
                else:
                    self.logger.log_warning(f"⚠️ Sync failed or returned no result for {instance}.")

            except Exception as e:
                self.logger.log_error(f"❌ Media settings sync failed for {instance}: {e}")

    @LoggerManager().log_function_entry
    @timeit("get_metadata")
    def get_metadata(self, instance):
        self.logger.log_info(f"📚 Fetching Sonarr metadata for {instance}")
        return self.global_cache.get_or_generate_cache(
            key=Paths.sonarr.METADATA,
            generator_function=lambda: self.sonarr_api._make_request(instance, "metadata") or [],
        )

    @staticmethod
    @LoggerManager().log_function_entry
    @timeit("warm_cache")
    def warm_cache(logger, cache, config):
        """
        Preload cache keys for: sonarr/metadata
        """
        instance = config.get_default_sonarr_instance_name()
        from scripts.managers.services.sonarr.sync.media import SonarrSyncMediaManager

        manager = SonarrSyncMediaManager(logger=logger, config=config, global_cache=cache)
        cache.get_or_generate_cache(
            key=Paths.sonarr.METADATA,
            generator_function=manager.get_metadata,
            expiration_time=604800,  # 1 week
        )

    @LoggerManager().log_function_entry
    @timeit("sync_quality_across_instances")
    def sync_quality_across_instances(self):
        """
        Synchronize quality profiles and custom formats across all Sonarr instance.
        """
        self.logger.log_info("🔁 Syncing quality profiles across Sonarr instance...")

        instances = self.registry.get_all("sonarr_api")
        if not instances:
            self.logger.log_error("❌ No Sonarr instance found for sync.")
            return

        reference_instance = next(iter(instances))
        reference_api = instances[reference_instance]
        reference_profiles = reference_api._make_request(reference_instance, "qualityProfile") or []
        reference_formats = reference_api._make_request(reference_instance, "customFormat") or []

        for instance, api in instances.items():
            if instance == reference_instance:
                continue

            self.logger.log_info(f"🔄 Syncing to {instance}")

            for profile in reference_profiles:
                api._make_request(instance, "qualityProfile", method="POST", payload=profile)

            for fmt in reference_formats:
                api._make_request(instance, "customFormat", method="POST", payload=fmt)

        self.logger.log_info("✅ Quality sync complete across all instance.")