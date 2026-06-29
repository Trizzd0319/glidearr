from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.radarr.sync.custom_formats import RadarrSyncCustomFormatsManager
from scripts.managers.services.radarr.sync.folders import RadarrSyncFoldersManager
from scripts.managers.services.radarr.sync.media_management import RadarrSyncMediaManager
from scripts.managers.services.radarr.sync.naming import RadarrSyncNamingManager
from scripts.managers.services.radarr.sync.profile_scores import RadarrSyncProfileScoresManager
from scripts.managers.services.radarr.sync.tags import RadarrSyncTagsManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class RadarrSyncManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrSyncManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = __class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.radarr_apis = {}
        self.load_summary = {}
        all_critical_loaded = True

        init_kwargs = {
            "logger":           self.logger,
            "config":           self.config,
            "global_cache":     self.global_cache,
            "validator":        self.validator,
            "registry":         self.registry,
            "radarr_api":       self.radarr_api,
            "instance_manager": self.instance_manager,
            "manager":          self,
            "dry_run":          self.dry_run,
        }

        all_component_classes = {
            "custom_formats":   RadarrSyncCustomFormatsManager,
            "folders":          RadarrSyncFoldersManager,
            "media_management": RadarrSyncMediaManager,
            "naming":           RadarrSyncNamingManager,
            "profile_scores":   RadarrSyncProfileScoresManager,
            "tags":             RadarrSyncTagsManager,
        }

        critical_keys = {"custom_formats", "folders", "media_management", "naming", "profile_scores", "tags"}

        critical_components, noncritical_components = split_components(
            all_components=all_component_classes,
            critical_keys=critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=init_kwargs,
        )

        for name, cls in critical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"radarr.sync.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"radarr.sync.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        for name, cls in noncritical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"radarr.sync.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"radarr.sync.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("radarr.sync_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Radarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded,
        )

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self):
        """Cross-instance custom-format DEFINITION + per-profile SCORE sync. INERT unless
        ``scoring.cf_sync.enabled`` (default off → complete no-op, byte-identical to before). The
        other sync leaves (tags / folders / naming / media_management) stay caller-driven and are
        NOT run here — in particular media_management.sync_quality_across_instances (a clobbering
        blind-POST) is never invoked. Definitions are synced first (additive), then scores
        (fill-only by default; overwrite needs the explicit flag + consent; always dry-run-safe)."""
        ps = getattr(self, "profile_scores", None)
        if ps is None or not ps.enabled():
            return
        self.logger.log_info("[CFSync] running cross-instance custom-format + quality-profile sync "
                             f"({self._instance_count()} instances).")
        try:
            ps.cap_profiles_to_tier()   # 0. cap every profile to its named tier (no above-tier grabs)
            ps.sync_definitions()       # 1. CF definitions exist everywhere (additive)
            ps.sync_uhd_profiles()      # 2. copy the source's 2160p profiles onto the 4K instance
            ps.apply_score_sync()       # 3. align per-profile CF scores (fill-only default, gated)
        except Exception as e:
            self.logger.log_error(f"[CFSync] sync failed: {e}")

    def _instance_count(self) -> int:
        return len([k for k in (self.config.get("radarr_instances") or {}) if k != "default_instance"]) \
            if self.config else 0
