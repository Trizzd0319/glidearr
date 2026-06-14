from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.repair.instance.config import SonarrRepairInstanceConfigManager
from scripts.managers.services.sonarr.repair.instance.credentials import SonarrRepairInstanceCredentialsManager
from scripts.managers.services.sonarr.repair.instance.flag import SonarrRepairInstanceFlagManager
from scripts.managers.services.sonarr.repair.instance.reachability import SonarrRepairInstanceReachabilityManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairInstanceManager(BaseManager, ComponentManagerMixin):
    """
    Master repair manager for all instance-level Sonarr configuration issues.
    Sequentially runs: flag cleanups, reachability checks, credential validation,
    and structural config fixes. Honors `dry_run` flag from parent.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__
        self.dry_run = kwargs.get("dry_run", False)
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        # 🔧 Initialize all submodules
        self.flag = SonarrRepairInstanceFlagManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            manager=self,
            dry_run=self.dry_run,
        )

        self.reach = SonarrRepairInstanceReachabilityManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            manager=self,
            dry_run=self.dry_run,
        )

        self.cred = SonarrRepairInstanceCredentialsManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            manager=self,
            dry_run=self.dry_run,
        )

        self.config_repair = SonarrRepairInstanceConfigManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            manager=self,
            dry_run=self.dry_run,
        )

        self.logger.log_debug(f"🛠️ {self.__class__.__name__} initialized with dry_run={self.dry_run}")

    @LoggerManager().log_function_entry
    @timeit("repair_instance_all")
    def run(self, skip_flags=False, skip_reach=False, skip_cred=False, skip_config=False):
        """
        Entry point to trigger all repairs.
        Each component may be skipped via flags for CLI use or partial debugging.
        """
        if not skip_flags:
            self.logger.log_debug("🧹 Starting flag cleanup...")
            self.flag.run()

        if not skip_reach:
            self.logger.log_debug("🌐 Starting reachability checks...")
            self.reach.run()

        if not skip_cred:
            self.logger.log_debug("🔑 Starting credential repairs...")
            self.cred.run()

        if not skip_config:
            self.logger.log_debug("⚙️ Starting config structure verification...")
            self.config_repair.run()

        self.logger.log_debug("✅ Instance repair complete.")
