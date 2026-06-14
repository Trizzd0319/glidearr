class ConfigResolver:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger

    def get_instances(self, service: str) -> dict:
        return self.config.get(f"{service}_instances", {})

    def get_default_instance(self, service: str) -> dict:
        all_instances = self.get_instances(service)
        # `default_instance` is stored as a dict {"name": "<instance>"} in the live
        # config (legacy configs used a bare string). A dict is unhashable, so it
        # cannot be passed straight to all_instances.get() — pull the name out first.
        # Mirrors ArrGateway.default_instance / SonarrInstanceManager.get_default_instance.
        di   = all_instances.get("default_instance")
        name = di.get("name") if isinstance(di, dict) else di
        if name and name in all_instances:
            return all_instances.get(name, {})
        # Configured default missing/unresolvable → fall back to the first real
        # instance entry (skipping the default_instance marker), else {}.
        for key, cfg in all_instances.items():
            if key != "default_instance" and isinstance(cfg, dict):
                return cfg
        return {}
