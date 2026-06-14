def split_components(all_components: dict, critical_keys: set, parent_name_match: str, logger, logger_context="ComponentSplitter", init_kwargs=None):
    """
    Splits a full component map into critical and noncritical groups.

    Args:
        all_components (dict): {name: class} map of all known components.
        critical_keys (set): names considered critical.
        parent_name_match (str): expected parent_name to match for noncritical.
        logger (LoggerManager): logger instance.
        logger_context (str): label for log context.
        init_kwargs (dict): shared kwargs to pass to each class for introspection.

    Returns:
        (critical_dict, noncritical_dict): separated dictionaries.
    """
    critical = {k: v for k, v in all_components.items() if k in critical_keys}
    noncritical = {}

    init_kwargs = init_kwargs or {}

    for name, cls in all_components.items():
        if name in critical_keys:
            continue

        try:
            temp_instance = cls(**init_kwargs)
            if getattr(temp_instance, "parent_name", "") == parent_name_match:
                noncritical[name] = cls
                logger.log_debug(f"[{logger_context}] ✅ Auto-detected noncritical component: {name}")
        except Exception as e:
            logger.log_warning(f"[{logger_context}] ⚠️ Failed to introspect {cls.__name__}: {e}")

    return critical, noncritical
