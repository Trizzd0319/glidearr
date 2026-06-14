from scripts.managers.factories.base_manager import BaseManager


from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class RadarrQualityCacheManager(BaseManager, ComponentManagerMixin):
    """
    Manages Radarr quality profiles, custom formats, and definitions.
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

    def refresh_quality_profiles(self, instance):
        profiles = self.radarr_api._make_request(instance, "qualityprofile", fallback=[]) if self.radarr_api else []
        if profiles:
            self.global_cache.set(f"radarr.quality_profiles.{instance}", profiles, compressed=True)
            self.logger.log_info(f"✅ Cached quality profiles for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No quality profiles for {instance}")

    def refresh_custom_formats(self, instance):
        formats = self.radarr_api._make_request(instance, "customformat", fallback=[]) if self.radarr_api else []
        if formats:
            self.global_cache.set(f"radarr.custom_formats.{instance}", formats, compressed=True)
            self.logger.log_info(f"✅ Cached custom formats for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No custom formats for {instance}")

    def get_quality_profiles(self, instance):
        return self.global_cache.get(f"radarr.{instance}.quality.profiles", default=[])

    def get_custom_formats(self, instance):
        return self.global_cache.get(f"radarr.{instance}.quality.custom_formats", default=[])

    def refresh_quality_definitions(self, instance):
        try:
            definitions = self.radarr_api._make_request(instance, "qualitydefinition", fallback=[]) if self.radarr_api else []
            if definitions:
                self.global_cache.set(f"radarr.{instance}.quality.definitions", definitions)
                self.logger.log_info(f"✅ Cached {len(definitions)} quality definitions for {instance}")
            else:
                self.logger.log_warning(f"⚠️ No quality definitions retrieved for {instance}")
        except Exception as e:
            self.logger.log_error(f"❌ Failed to refresh quality definitions for {instance}: {e}")

    def get_quality_definitions(self, instance):
        return self.global_cache.get(f"radarr.{instance}.quality.definitions", default=[])

    def log_quality_summary(self, instance):
        profiles = self.get_quality_profiles(instance)
        formats = self.get_custom_formats(instance)
        definitions = self.get_quality_definitions(instance)

        self.logger.log_info(f"📊 Radarr Quality Summary for {instance}:")
        self.logger.log_info(f" • Profiles: {len(profiles)}")
        self.logger.log_info(f" • Custom Formats: {len(formats)}")
        self.logger.log_info(f" • Definitions: {len(definitions)}")
