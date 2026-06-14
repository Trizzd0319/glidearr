import json
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from difflib import SequenceMatcher

_REGEX_MAX_LEN = 500
_REGEX_MATCH_TIMEOUT = 2.0

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class SonarrSyncCustomFormatsManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrStorage"
        class_name = self.__class__.__name__

        if class_name.endswith("Manager"):
            self.parent_name = class_name.replace("Manager", "")
        else:
            self.parent_name = class_name

        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = kwargs.get("manager") or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")


    def deduplicate_custom_formats(self, custom_formats):
        def sorted_json(obj):
            try:
                return json.dumps(obj, sort_keys=True)
            except TypeError:
                return str(obj)

        seen = set()
        unique_cfs = []
        for cf in custom_formats:
            cf_serialized = sorted_json(cf)
            if cf_serialized not in seen:
                seen.add(cf_serialized)
                unique_cfs.append(cf)
        return unique_cfs

    def custom_format_exists(self, new_cf, existing_formats):
        def is_similar(a, b, threshold=0.85):
            return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= threshold

        for cf in existing_formats:
            if is_similar(cf.get("name", ""), new_cf.get("name", "")):
                return True
            if self.regex_content_conflict(cf, new_cf):
                return True
        return False

    def regex_content_conflict(self, cf1, cf2):
        regexes1 = [
            str(f.get("value")) for spec in cf1.get("specifications", [])
            for f in spec.get("fields", []) if "value" in f
        ]
        regexes2 = [
            str(f.get("value")) for spec in cf2.get("specifications", [])
            for f in spec.get("fields", []) if "value" in f
        ]

        for r1 in regexes1:
            if len(r1) > _REGEX_MAX_LEN:
                continue
            try:
                pattern = re.compile(r1, flags=re.IGNORECASE)
            except (re.error, TypeError):
                continue

            for r2 in regexes2:
                if len(r2) > _REGEX_MAX_LEN:
                    continue
                try:
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(pattern.search, r2)
                        if future.result(timeout=_REGEX_MATCH_TIMEOUT):
                            return True
                except FuturesTimeoutError:
                    self.logger.log_warning(f"⚠️ Regex match timed out — skipping pattern: {r1[:60]!r}")
                except Exception:
                    continue
        return False

    def sync_all_custom_formats(self):
        sonarr_instances = self.config.get_sonarr_instances()
        instance_cf_map = {
            instance: self.deduplicate_custom_formats(
                self.sonarr_api._make_request(instance, "customformat") or []
            ) for instance in sonarr_instances
        }

        all_unique_cfs = []
        for cf_list in instance_cf_map.values():
            for cf in cf_list:
                if not self.custom_format_exists(cf, all_unique_cfs):
                    all_unique_cfs.append(cf)

        for instance, existing_cfs in instance_cf_map.items():
            for cf in all_unique_cfs:
                if not self.custom_format_exists(cf, existing_cfs):
                    result = self.sonarr_api.add_custom_format(instance, cf)
                    status = "✅" if result else "❌"
                    self.logger.log_info(f"{status} Synced '{cf['name']}' to {instance}")
