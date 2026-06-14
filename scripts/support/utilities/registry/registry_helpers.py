def generate_component_key(obj):
    """
    Generate a registry key based on the module and class name, starting from 'scripts'.
    Example:
    scripts.managers.services.sonarr.instance.instance.SonarrInstanceAccessor
    → scripts.managers.services.sonarr.instance.instance.sonarrinstanceaccessor (lowercased)
    """
    module_path = getattr(obj.__class__, '__module__', '')
    class_name = obj.__class__.__name__

    if module_path.startswith('scripts.'):
        module_path_clean = module_path
    else:
        # fallback if not under 'scripts' namespace
        module_path_clean = f"scripts.{module_path}"

    key = f"{module_path_clean}.{class_name}".lower()
    return key
