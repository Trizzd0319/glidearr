from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.cache import SonarrCacheManager
from scripts.managers.services.sonarr.validator.auth import SonarrValidatorAuthManager
from scripts.managers.services.sonarr.validator.factory import SonarrValidatorFactoryManager
from scripts.managers.services.sonarr.validator.health import SonarrValidatorHealthManager
from scripts.managers.services.sonarr.validator.keys import SonarrValidatorKeysManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class SonarrValidatorManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, sonarr_api=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        # forward dry_run so the SonarrCacheManager child below doesn't run its episode-file ops live
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))
        self.instance_manager = kwargs.get("instance_manager") or getattr(manager, "instance_manager", None)

        self.register()

        self.sonarr_apis = {}
        self.load_summary = {}
        all_critical_loaded = True

        all_component_classes = {
            "auth_handler": SonarrValidatorAuthManager,
            "cache_manager": SonarrCacheManager,
            "health_validator": SonarrValidatorHealthManager,
            "key_validator": SonarrValidatorKeysManager,
            "api_factory": SonarrValidatorFactoryManager,
        }

        critical_keys = {"auth_handler", "cache_manager", "health_validator", "key_validator"}

        init_args = {
            "logger": self.logger,
            "config": self.config,
            "global_cache": self.global_cache,
            "validator": self.validator,
            "registry": self.registry,
            "sonarr_cache": self.sonarr_cache,
            "manager": self,
            "sonarr_api": sonarr_api or self,
            "dry_run": self.dry_run,
            "instance_manager": self.instance_manager,
        }

        critical_components, noncritical_components = split_components(
            all_components=all_component_classes,
            critical_keys=critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=init_args
        )

        for name, cls in critical_components.items():
            try:
                instance = cls(**init_args)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.validator.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.validator.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        for name, cls in noncritical_components.items():
            try:
                instance = cls(**init_args)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.validator.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.validator.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("sonarr.validator_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Sonarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded
        )

    def audit_bootstrap_instances(self, validate_keys=True, validate_health=True):
        """
        Lightweight bootstrap validation of API keys and reachability for all Sonarr instances.
        Called early in SonarrInstanceManager.__init__ to avoid downstream errors.
        """
        summary = {}

        if validate_keys:
            summary["credentials"] = self.key_validator.run_credentials_only()

        if validate_health:
            summary["health"] = self.health_validator.verify_all_instances_health()

        self.logger.log_info(f"🩺 Bootstrap Audit Results: {summary}")
        return summary
