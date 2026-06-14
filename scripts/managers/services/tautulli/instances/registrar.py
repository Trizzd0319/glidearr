# tautulli/instance/archived_registrar.py

class TautulliInstanceRegistrar:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger

    def register_instance(self, name="default"):
        instance_config = (self.config.get("tautulli") or {}).get(name)
        if not instance_config:
            self.logger.log_warning(f"No Tautulli config found for instance '{name}'")
            return None

        self.logger.log_info(f"✅ Registered Tautulli instance '{name}'")
        return instance_config
