import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairInstanceReachabilityManager(BaseManager, ComponentManagerMixin):
    """
    Attempts to reach each Sonarr instance via HTTP and logs the result.
    Sets registry flags only if reachable. Honors global dry_run flag.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, manager=None, **kwargs):
        self.parent_name = "SonarrRepair"
        self.manager = manager
        # Resolve dry_run from the explicit kwarg / local `manager` param BEFORE
        # super().__init__ runs — BaseManager reassigns self.manager to a
        # registry-resolved parent, so reading self.manager afterwards is unreliable
        # (that mismatch is exactly why this previously logged dry_run=False mid dry-run).
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.logger.log_debug(f"🌐 Initialized {self.__class__.__name__} (Parent: {self.parent_name}, Dry Run = {self.dry_run})")

    @LoggerManager().log_function_entry
    @timeit("repair_instance_reachability")
    def run(self):
        instances = self.config.get("sonarr_instances", {})
        timeout_seconds = 5
        success, failure = 0, 0

        for name, cfg in instances.items():
            if name == "default_instance" or not isinstance(cfg, dict):
                self.logger.log_debug(f"⏩ Skipping invalid config entry: {name}")
                continue

            base_url = cfg.get("base_url")
            port = cfg.get("port", "⚠️ missing")
            full_url = base_url or "(no URL)"

            if not base_url:
                self.logger.log_warning(f"⚠️ Instance '{name}' is missing a base_url.")
                failure += 1
                continue

            self.logger.log_debug(f"🌐 Checking reachability for {name}: {full_url}")

            if self.dry_run:
                self.logger.log_debug("🛑 Dry run enabled — skipping actual request.")
                continue

            ssl_verify = cfg.get("ssl_verify", True)

            try:
                response = requests.get(base_url, timeout=timeout_seconds, verify=ssl_verify)
                if response.ok:
                    self.logger.log_debug(f"✅ {name} reachable (status={response.status_code})")
                    self.registry.set_flag(f"sonarr.instance.{name}.reachable", True)
                    success += 1
                else:
                    self.logger.log_warning(
                        f"⚠️ {name} responded with status {response.status_code} ({response.reason})"
                    )
                    failure += 1
            except requests.exceptions.RequestException as e:
                self.logger.log_error(f"❌ Failed to reach {name}: {e}")
                failure += 1

        self.logger.log_debug(
            f"📊 Instance reachability check finished: {success} succeeded, {failure} failed (dry_run={self.dry_run})"
        )
