from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class SonarrCacheQualityManager(BaseManager, ComponentManagerMixin):
    """
    Manages Sonarr quality and profile caches.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrCache"
        class_name = self.__class__.__name__

        if class_name.endswith("Manager"):
            self.parent_name = class_name.replace("Manager", "")
        else:
            self.parent_name = class_name

        manager = kwargs.get("manager") or {}

        # ✅ Dual-cache setup
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        super().__init__(logger, config, self.global_cache, validator, registry, **kwargs)
        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = manager or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")

    def refresh_quality_profiles(self, instance):
        profiles = self.sonarr_api.get_quality_profiles(instance)
        if profiles:
            self.global_cache.set(f"sonarr/{instance}/quality_profiles.json", profiles)
            self.logger.log_info(f"✅ Refreshed quality profiles cache for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No quality profiles retrieved for {instance}")

    def get_quality_profiles(self, instance):
        return self.global_cache.get(f"sonarr/{instance}/quality_profiles.json", default=[])

    def refresh_custom_formats(self, instance):
        formats = self.sonarr_api.get_custom_formats(instance)
        if formats:
            self.global_cache.set(f"sonarr/{instance}/custom_formats.json", formats)
            self.logger.log_info(f"✅ Refreshed custom formats cache for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No custom formats retrieved for {instance}")

    def get_custom_formats(self, instance):
        return self.global_cache.get(f"sonarr/{instance}/custom_formats.json", default=[])

    def refresh_quality_definitions(self, instance):
        definitions = self.sonarr_api.get_quality_definitions(instance)
        if definitions:
            self.global_cache.set(f"sonarr/{instance}/quality_definitions.json", definitions)
            self.logger.log_info(f"✅ Refreshed quality definitions cache for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No quality definitions retrieved for {instance}")

    def get_quality_definitions(self, instance):
        return self.global_cache.get(f"sonarr/{instance}/quality_definitions.json", default=[])

    def log_quality_summary(self, instance):
        profiles = self.get_quality_profiles(instance)
        formats = self.get_custom_formats(instance)
        definitions = self.get_quality_definitions(instance)

        self.logger.log_info(f"📊 Quality Summary for {instance}:")
        self.logger.log_info(f" - Profiles: {len(profiles)} entries")
        self.logger.log_info(f" - Custom Formats: {len(formats)} entries")
        self.logger.log_info(f" - Quality Definitions: {len(definitions)} entries")
