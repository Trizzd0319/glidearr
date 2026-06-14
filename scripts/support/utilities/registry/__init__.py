from datetime import datetime

class RegistryHelper:
    @staticmethod
    def generate_component_key(obj):
        try:
            return f"{obj.__class__.__module__}.{obj.__class__.__name__}".lower()
        except Exception:
            return str(obj).lower()

    @staticmethod
    def register_component(registry, obj, prefix="manager"):
        """
        Register a component in the registry using a consistent key based on its module and class name.
        :param registry: The registry to register into.
        :param obj: The object to register.
        :param prefix: The registry category (default is 'manager').
        """
        key = RegistryHelper.generate_component_key(obj)
        registry.register(prefix, key, obj)

    @staticmethod
    def bulk_register_components(registry, instances: dict, prefix="manager"):
        """
        Register a batch of component instance into the registry.
        :param registry: The registry to register into.
        :param instances: Dictionary of name → instance to register.
        :param prefix: The registry category.
        """
        for instance in instances.values():
            RegistryHelper.register_component(registry, instance, prefix)

    @staticmethod
    def log_component_summary(logger, label, components):
        """
        Log a summary of all registered components.
        :param logger: Logger instance.
        :param label: Label for the registry section.
        :param components: Dictionary of name → component.
        """
        logger.log_info(f"🔧 Registered {len(components)} components under {label}:")
        for name, component in components.items():
            logger.log_info(f"   - {name}: {RegistryHelper.generate_component_key(component)}")

    @staticmethod
    def collect_instances(classes: dict, init_args: dict, fail_log: list = None, skip_keys: set = None):
        """
        Instantiate and collect a batch of classes.
        :param classes: Dictionary of name → class.
        :param init_args: Arguments to pass to each class on instantiation.
        :param fail_log: A list to collect failures.
        :param skip_keys: Optional keys to skip.
        :return: Dictionary of name → instance.
        """
        instances = {}
        for name, cls in classes.items():
            if skip_keys and name in skip_keys:
                continue
            try:
                instance = cls(**init_args)
                instances[name] = instance
            except Exception as e:
                if fail_log is not None:
                    fail_log.append((name, str(e)))
        return instances

    @staticmethod
    def register_collected_components(instances: dict, registry, logger, parent_label: str, prefix="manager"):
        """
        Registers collected instance and logs the summary.
        """
        RegistryHelper.bulk_register_components(registry, instances, prefix=prefix)
        RegistryHelper.log_component_summary(logger, parent_label, instances)