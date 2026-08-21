# registry/config_sync.py
from collections.abc import Mapping

from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class RegistryConfigSync:
    # Once-per-process, because the caller below runs on EVERY manager init and a
    # per-manager warning would bury the one line that matters under ~40 copies.
    _warned_config_mapping = False

    def __init__(self, registry):
        self.registry = registry

    @LoggerManager().log_function_entry
    @timeit("auto_hot_swap_from_config")
    def auto_hot_swap_from_config(self, obj):
        """Re-register an already-registered manager object under its own keys.

        ⚠️ THIS DOES NOTHING TODAY, on every manager, on every run.
        `BaseManager.__init__` calls it as
            self.registry.auto_hot_swap_from_config(self.config.raw_data)
        and `raw_data` is the config DICT -- which has neither
        `_registry_category` nor `name`, so control always reached the else
        branch. That branch then guarded on `self.registry.logger`, an attribute
        RegistryManager does not define, so the warning never emitted either:
        a no-op whose own failure detector was itself unreachable (P-A + P-D in
        one method).

        Nothing is silently repaired here, because the two readings need
        different fixes and only the operator can choose: either the CALL SITE
        is wrong (config data was never the argument this takes, and the call
        should go) or the METHOD is wrong (it was meant to apply config-driven
        swaps and was never written). The mapping case now says so out loud,
        once, so the next run decides it on evidence.
        """
        if hasattr(obj, '_registry_category') and hasattr(obj, 'name'):
            self.registry.register(obj._registry_category, obj.name, obj)
            return

        if isinstance(obj, Mapping):
            if not RegistryConfigSync._warned_config_mapping:
                RegistryConfigSync._warned_config_mapping = True
                LoggerManager().log_warning(
                    "⚠️ auto_hot_swap_from_config() was handed a config MAPPING, not a "
                    "registered object -- it takes a manager instance and has therefore "
                    "been a no-op on every manager init. Config-driven hot swap is NOT "
                    "implemented; see GLD-REG-12."
                )
            return

        LoggerManager().log_warning(
            f"⚠️ Cannot auto-register object of type {type(obj)}: missing required attributes."
        )

    @LoggerManager().log_function_entry
    @timeit("load_config_and_propagate")
    def load_config_and_propagate(self, key):
        base_config = self.registry.get("manager", "ConfigManager")
        if not base_config:
            return
        value = getattr(base_config, key, None)
        if value is None:
            return
        # _unwrap, because `entries` holds register()'s wrapper dicts: hasattr on
        # the WRAPPER is False for every manager attribute, so this propagated to
        # nothing at all. Same shape as find_by_attr's. "flags" is skipped -- its
        # values are bools, and setattr on one is meaningless.
        for cat, entries in self.registry._registry.items():
            if cat == "flags" or not isinstance(entries, dict):
                continue
            for name, entry in entries.items():
                obj = self.registry._unwrap(entry)
                if hasattr(obj, key):
                    setattr(obj, key, value)
