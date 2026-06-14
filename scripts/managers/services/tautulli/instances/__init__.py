from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.tautulli.instances.api import TautulliAPI
from .registrar import TautulliInstanceRegistrar
from .summary_formatter import TautulliInstanceSummaryFormatter


class TautulliInstanceManager(BaseManager):
    def __init__(self, logger, config, global_cache, **kwargs):
        super().__init__(logger, config, global_cache, **kwargs)
        self.instances = {}

    def get_instance_names(self):
        tautulli_config = self.config.get("tautulli", {})
        if isinstance(tautulli_config, dict):
            if all(isinstance(v, str) for v in tautulli_config.values()):
                return ["default"]
            return list(tautulli_config.keys())
        return []

    def get_instance(self, name="default"):
        if name in self.instances:
            return self.instances[name]

        tautulli_config = self.config.get("tautulli", {})

        # Handle flat single-instance config
        if name == "default" and all(isinstance(v, str) for v in tautulli_config.values()):
            instance_config = tautulli_config
        else:
            instance_config = tautulli_config.get(name)

        if not instance_config:
            self.logger.log_warning(f"⚠️ Tautulli instance '{name}' not found in config.")
            return None

        api = TautulliAPI(logger=self.logger, instance_config=instance_config, cache=self.global_cache)
        formatter = TautulliInstanceSummaryFormatter(api=api, logger=self.logger)

        self.instances[name] = {
            "api": api,
            "formatter": formatter
        }

        return self.instances[name]

    def validate_instance(self, name="default"):
        api = self.get_api(name)
        return api and api.validate()

    def get_summary(self, name="default"):
        instance = self.get_instance(name)
        return instance["formatter"].format_summary() if instance else {}

    def get_api(self, name="default"):
        instance = self.get_instance(name)
        return instance["api"] if instance else None
