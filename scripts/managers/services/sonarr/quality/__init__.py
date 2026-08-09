from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.quality.adjustment import SonarrQualityAdjustmentManager
from scripts.managers.services.sonarr.quality.custom_formats import SonarrQualityCustomFormatsManager
from scripts.managers.services.sonarr.quality.filesizes import SonarrQualityFileSizesManager
from scripts.managers.services.sonarr.quality.selector import SonarrQualitySelectorManager
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.managers.component_splitter import split_components


class SonarrQualityManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(
        self,
        logger=None,
        config=None,
        global_cache=None,
        validator=None,
        registry=None,
        sonarr_api=None,
        cache_manager=None,
        **kwargs
    ):
        self.parent_name = __class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.key_builder = kwargs.get("key_builder")
        self.dry_run = kwargs.get("dry_run", False)
        self.sonarr_cache = cache_manager or getattr(self, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(self, "global_cache", None)
        self.sonarr_apis = {}
        self.load_summary = {}
        all_critical_loaded = True

        if not self.key_builder:
            raise ValueError("❌ SonarrQualityManager requires key_builder but none was provided.")

        all_component_classes = {
            "adjustments": SonarrQualityAdjustmentManager,
            "custom_formats": SonarrQualityCustomFormatsManager,
            "file_sizes": SonarrQualityFileSizesManager,
            "selector": SonarrQualitySelectorManager,
        }

        critical_keys = {"adjustments", "custom_formats", "file_sizes", "selector"}
        _unknown = critical_keys - set(all_component_classes)
        if _unknown:
            raise KeyError(
                f"{self.__class__.__name__}: critical_keys names component(s) that do not "
                f"exist: {sorted(_unknown)}. Known: {sorted(all_component_classes)}."
            )

        # Hoisted out of the split_components(...) call so the construction loops
        # below can reuse it. They previously read
        # ``cls(**critical_components[name]["init_kwargs"])`` -- but
        # critical_components[name] IS the class, and subscripting a class raises
        # TypeError: 'type' object is not subscriptable. Every one of the four
        # components failed, the per-component except swallowed it into
        # load_summary, and all_critical_loaded went False. Never observed because
        # this manager is filtered out of SonarrManager's loadable set and is
        # superseded by orchestration/quality.py, which has the loop right.
        quality_init_kwargs = {
            "logger": self.logger,
            "config": self.config,
            "global_cache": self.global_cache,
            "validator": self.validator,
            "registry": self.registry,
            "manager": self,
            "sonarr_api": sonarr_api,
            "cache_manager": self.sonarr_cache,
            "key_builder": self.key_builder,
            "dry_run": self.dry_run,
        }

        critical_components, noncritical_components = split_components(
            all_components=all_component_classes,
            critical_keys=critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=quality_init_kwargs,
        )

        for name, cls in critical_components.items():
            try:
                instance = cls(**quality_init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.quality.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.quality.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        for name, cls in noncritical_components.items():
            try:
                instance = cls(**quality_init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.quality.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.quality.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("sonarr.quality_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Sonarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded,
        )
