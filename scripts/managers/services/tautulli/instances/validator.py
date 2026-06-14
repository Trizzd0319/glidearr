from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.tautulli.instances.api import TautulliAPI


class TautulliInstanceValidatorManager(BaseManager):
    def __init__(self, logger=None, config=None, global_cache=None, **kwargs):
        super().__init__(logger, config, global_cache, **kwargs)

    def validate(self, name="default") -> bool:
        tautulli_cfg = self.config.get("tautulli", {}) if self.config else {}
        if all(isinstance(v, str) for v in tautulli_cfg.values()):
            instance_config = tautulli_cfg  # flat single-instance config
        else:
            instance_config = tautulli_cfg.get(name)

        if not instance_config:
            if name == "backup":
                self.logger.log_info("⚠️ Tautulli 'backup' instance not configured — skipping.")
            else:
                self.logger.log_warning(f"⚠️ Tautulli instance '{name}' not found in config.")
            return False

        api = TautulliAPI(logger=self.logger, instance_config=instance_config)
        return bool(api.validate())
