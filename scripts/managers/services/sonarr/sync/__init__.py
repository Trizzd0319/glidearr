from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache import CacheKeyBuilder
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.sync.custom_formats import SonarrSyncCustomFormatsManager
from scripts.managers.services.sonarr.sync.folders import SonarrSyncFoldersManager
from scripts.managers.services.sonarr.sync.media import SonarrSyncMediaManager
from scripts.managers.services.sonarr.sync.naming import SonarrSyncNamingManager
from scripts.managers.services.sonarr.sync.tags import SonarrSyncTagsManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class SonarrSyncManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, sonarr_api=None, **kwargs):
        self.parent_name = __class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.key_builder = CacheKeyBuilder()
        self.sonarr_apis = {}
        self.load_summary = {}
        all_critical_loaded = True

        # Prepare full init args for all subcomponents
        init_args = {
            "logger": self.logger,
            "config": self.config,
            "global_cache": self.global_cache,   # canonical key — 'cache' left every child's global_cache=None
            "validator": self.validator,
            "registry": self.registry,
            "manager": self,
            "sonarr_api": sonarr_api,
            "instance_manager": getattr(sonarr_api, "instance_manager", None),
            "key_builder": self.key_builder,
            "dry_run": kwargs.get("dry_run", False)
        }

        all_component_classes = {
            "custom_formats": SonarrSyncCustomFormatsManager,
            "folders": SonarrSyncFoldersManager,
            "media_management": SonarrSyncMediaManager,
            "naming": SonarrSyncNamingManager,
            "tags": SonarrSyncTagsManager
        }

        critical_keys = {"custom_formats", "folders", "media_management", "naming", "tags"}

        critical_components, noncritical_components = split_components(
            all_components=all_component_classes,
            critical_keys=critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=init_args
        )

        # Load critical components
        for name, cls in critical_components.items():
            try:
                instance = cls(**init_args)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.sync.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.sync.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        # Load noncritical components
        for name, cls in noncritical_components.items():
            try:
                instance = cls(**init_args)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.sync.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.sync.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("sonarr.sync_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Sonarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded
        )

    # ── run ───────────────────────────────────────────────────────────────────
    #
    # GLD-SON-01. This class had NO run() and no prepare(), so the five sync
    # sub-managers were constructed and never driven.
    #
    # WHAT RADARR ACTUALLY DOES, which is not what "match Radarr" first suggested.
    # `RadarrSyncManager.run` drives ONLY the custom-format / profile-score path
    # and says so explicitly: "The other sync leaves (tags / folders / naming /
    # media_management) stay caller-driven and are NOT run here - in particular
    # media_management.sync_quality_across_instances (a clobbering blind-POST) is
    # never invoked."
    #
    # So the asymmetry was narrower than it looked. Radarr does not sweep all five
    # either; it runs one gated path and leaves the rest to explicit callers.
    # Sonarr now does the same.
    #
    # THE THREE THIS DELIBERATELY DOES NOT CALL, and why:
    #
    #   media_management.sync_quality_across_instances
    #       `sync_media_management_settings` issues an unconditional
    #       `PUT config/mediamanagement` with NO dry_run check. Radarr names it a
    #       clobbering blind-POST and refuses it; there is no reason Sonarr should
    #       be braver about the same call.
    #   tags.sync_tags_across_instances
    #       stores `self.dry_run` and never reads it.
    #   custom_formats.sync_all_custom_formats
    #       stores `self.dry_run` and never reads it.
    #
    # naming and folders DO honour dry_run, but both need arguments
    # (`sync_naming_settings(naming_config)`, `initialize_root_folders(instance,
    # dry_run)`) that only a caller with the config and the instance can supply -
    # which is precisely what "caller-driven" means. Driving them from here would
    # require inventing a naming_config, and a wrong one rewrites every file name
    # in the library.
    #
    # The remaining honest gap is that Sonarr has no equivalent of Radarr's
    # `profile_scores` leaf, so there is currently nothing safe to sweep. Filed as
    # GLD-SON-20 with the dry_run gaps.

    @timeit("run")
    def run(self) -> dict:
        """Report what this manager holds. Drives nothing yet - see the block above.

        Deliberately not a no-op METHOD: before this existed the manager was
        indistinguishable from absent, which is how it stayed inert unnoticed. A
        run() that reports its own inertness is the detector for its own gap.
        """
        leaves = [n for n in ("custom_formats", "folders", "media_management",
                              "naming", "tags") if getattr(self, n, None) is not None]
        ungated = [n for n in ("custom_formats", "media_management", "tags")
                   if getattr(self, n, None) is not None]
        self.logger.log_info(
            f"[SonarrSync] {len(leaves)} sync leaf/leaves loaded ({', '.join(leaves)}) - "
            f"none driven automatically. {len(ungated)} of them ignore dry_run and must "
            f"stay caller-driven until they honour it (GLD-SON-20); Radarr excludes the "
            f"same three.")
        return {"loaded": len(leaves), "driven": 0, "ungated": len(ungated)}

