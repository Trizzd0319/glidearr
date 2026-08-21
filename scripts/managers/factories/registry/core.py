# registry/core.py
import os
import inspect
import threading
from collections import defaultdict
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit

class RegistryCore:
    _instance = None
    _class_lock = threading.Lock()

    def __new__(cls):
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._registry = {}
                cls._instance._lock = threading.RLock()
        return cls._instance

    @staticmethod
    def _unwrap(entry):
        """The registered OBJECT, whichever of the two stored shapes `entry` is.

        TWO shapes exist and always have: `register()` stores a wrapper dict
        {instance, origin, parent_name}; `set()` stores the bare object. Every
        reader below picked one shape and was silently wrong for the other --
        `get_all` raised on a set()-written entry, `find_by_attr` and
        `load_config_and_propagate` getattr'd the WRAPPER and so could never
        match anything, and the CLI dump read key names nothing writes. One
        unwrap, used by all of them, so a shape change cannot desynchronise
        them again (P-E).
        """
        if isinstance(entry, dict) and "instance" in entry:
            return entry["instance"]
        return entry

    def get_all(self, category):
        """Return a dict of name → instance (not the wrapped dictionary)"""
        return {
            name: self._unwrap(entry)
            for name, entry in (self._registry.get(category) or {}).items()
        }

    def get_all_verbose(self, category):
        """Return full dict with origins"""
        return self._registry.get(category, {})

    @LoggerManager().log_function_entry
    @timeit("register")
    def register(self, category, name, obj, parent_name=None):
        """
        Register an object under a category and name, with origin tracking and optional parent_name.

        Args:
            category (str): e.g., "manager"
            name (str): normalized component key
            obj (object): class instance being registered
            parent_name (str): optional string name of the parent manager class
        """
        # Ensure category exists
        if category not in self._registry:
            self._registry[category] = {}

        # Try to find the first non-utility frame in the call stack
        origin = "unknown"
        utility_keywords = [
            "support/utilities/decorators/",
            "support/utilities/logger/",
            "decorators/timing",
            "logger/logger",
            "utilities/logger",
            "utilities/decorators",
            "registry/core",
            "factories/base_manager",
            # ⬇️ Added after the Source column was found to be near-useless.
            # ComponentManagerMixin.register() calls this method, and it was not
            # skipped -- so for every component that calls self.register() (which
            # is nearly all of them) the walk stopped on the MIXIN and recorded
            # component_manager.py as the origin. Worse, the mixin registers
            # AFTER BaseManager already did, so the correct origin was written
            # first and then overwritten by the wrapper's. The column named the
            # same file for dozens of rows and nobody could tell it was wrong,
            # because a plausible path is indistinguishable from the right one.
            "factories/mixins/",
            "factories/base_instance_manager",
        ]
        try:
            for frame in inspect.stack():
                filepath = frame.filename.replace("\\", "/").lower()
                if not any(util in filepath for util in utility_keywords):
                    origin = f"{frame.filename}:{frame.lineno} in {frame.function}()"
                    break
        except Exception:
            pass

        with self._lock:
            # Store instance and origin
            self._registry[category][name] = {
                "instance": obj,
                "origin": origin,
                "parent_name": parent_name,
            }

        # Attach metadata directly to the object (outside lock — object is already referenced)
        if hasattr(obj, "__class__"):
            setattr(obj, "_registry_category", category)
            setattr(obj, "_registry_name", name)
            setattr(obj, "_registered_class", obj.__class__.__name__)
            setattr(obj, "_registered_from", origin)
            setattr(obj, "parent_name", parent_name or getattr(obj, "parent_name", None))

    @LoggerManager().log_function_entry
    @timeit("get")
    def get(self, category, name):
        entry = (self._registry.get(category) or {}).get(name)
        return entry.get("instance") if isinstance(entry, dict) else entry

    @LoggerManager().log_function_entry
    @timeit("set")
    def set(self, category, name, value):
        with self._lock:
            if category not in self._registry:
                self._registry[category] = {}
            self._registry[category][name] = value
        setattr(value, "_registry_category", category)
        setattr(value, "_registry_name", name)
        setattr(value, "_registered_class", value.__class__.__name__)

    @LoggerManager().log_function_entry
    @timeit("remove")
    def remove(self, category, name):
        with self._lock:
            if category in self._registry and name in self._registry[category]:
                del self._registry[category][name]

    @LoggerManager().log_function_entry
    @timeit("list_registered")
    def list_registered(self, category=None, include_origin=True):
        """
        Lists registered objects with optional origin metadata.
        Returns:
            - Dict[str, str] if category specified (name → class @ origin)
            - Dict[str, Dict[str, str]] if all (category → name → class @ origin)
        """

        def format_entry(entry):
            if isinstance(entry, dict):
                obj = entry.get("instance")
                origin = entry.get("origin", "unknown")
            else:
                obj = entry
                origin = "unknown"
            class_name = getattr(obj, "__class__", type(obj)).__name__
            return f"{class_name} @ {origin}" if include_origin else class_name

        if category:
            return {
                name: format_entry(entry)
                for name, entry in (self._registry.get(category) or {}).items()
            }

        return {
            cat: {
                name: format_entry(entry)
                for name, entry in entries.items()
            }
            for cat, entries in self._registry.items()
        }

    @LoggerManager().log_function_entry
    @timeit("find_by_attr")
    def find_by_attr(self, attr_name, attr_value):
        """Every (category, name, object) whose `attr_name` equals `attr_value`.

        ⚠️ Returned [] unconditionally until the unwrap was added: `entries`
        holds register()'s WRAPPER DICTS, and getattr(dict, "parent_name") is
        always None, so no lookup could ever match and the emptiness was
        indistinguishable from 'nothing has that attribute' (P-C).

        "flags" is skipped deliberately -- its values are bools and strings,
        not managers, and matching one would return a row no caller can use.
        """
        results = []
        for cat, entries in self._registry.items():
            if cat == "flags" or not isinstance(entries, dict):
                continue
            for name, entry in entries.items():
                obj = self._unwrap(entry)
                if getattr(obj, attr_name, None) == attr_value:
                    results.append((cat, name, obj))
        return results

    # ✅ Flag management helpers
    def set_flag(self, flag_name, value=True):
        with self._lock:
            if "flags" not in self._registry:
                self._registry["flags"] = {}
            self._registry["flags"][flag_name] = value

    def get_flag(self, flag_name):
        with self._lock:
            return (self._registry.get("flags") or {}).get(flag_name, None)

    def has_flag(self, flag_name):
        with self._lock:
            return (self._registry.get("flags") or {}).get(flag_name, False)

    def clear_flags(self, prefix=None):
        with self._lock:
            if "flags" not in self._registry:
                return
            if prefix:
                to_delete = [k for k in self._registry["flags"] if k.startswith(prefix)]
                for k in to_delete:
                    del self._registry["flags"][k]
            else:
                self._registry["flags"].clear()
