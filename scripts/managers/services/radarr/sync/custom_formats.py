import json
import re
from difflib import SequenceMatcher

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrSyncCustomFormatsManager(BaseManager, ComponentManagerMixin):
    """
    Deduplicates and synchronises custom formats across Radarr instances.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrSyncManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    def deduplicate_custom_formats(self, custom_formats: list) -> list:
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

    def custom_format_exists(self, new_cf: dict, existing_formats: list) -> bool:
        def is_similar(a, b, threshold=0.85):
            return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= threshold

        for cf in existing_formats:
            if is_similar(cf.get("name", ""), new_cf.get("name", "")):
                return True
            if self._regex_content_conflict(cf, new_cf):
                return True
        return False

    def _regex_content_conflict(self, cf1: dict, cf2: dict) -> bool:
        regexes1 = [
            str(f.get("value")) for spec in cf1.get("specifications", [])
            for f in spec.get("fields", []) if "value" in f
        ]
        regexes2 = [
            str(f.get("value")) for spec in cf2.get("specifications", [])
            for f in spec.get("fields", []) if "value" in f
        ]
        for r1 in regexes1:
            try:
                pattern = re.compile(r1, flags=re.IGNORECASE)
                if any(pattern.search(r2) for r2 in regexes2):
                    return True
            except (re.error, TypeError):
                continue
        return False

    @LoggerManager().log_function_entry
    @timeit("get_custom_formats")
    def get_custom_formats(self, instance: str) -> list:
        resolved = self._resolve_instance(instance)
        cached = self.global_cache.get(f"radarr.custom_formats.{resolved}", default=None)
        if cached is not None:
            return cached
        formats = self.radarr_api._make_request(resolved, "customformat", fallback=[]) or []
        self.global_cache.set(f"radarr.custom_formats.{resolved}", formats)
        return formats

    @LoggerManager().log_function_entry
    @timeit("add_custom_format")
    def add_custom_format(self, instance: str, custom_format_payload: dict):
        resolved = self._resolve_instance(instance)
        if self.dry_run:
            self.logger.log_info(f"[dry_run] Would add custom format '{custom_format_payload.get('name')}' to {resolved}")
            return None
        self.logger.log_info(f"Adding custom format to {resolved}")
        return self.radarr_api._make_request(resolved, "customformat", method="POST", payload=custom_format_payload)

    @LoggerManager().log_function_entry
    @timeit("get_custom_format_scores")
    def get_custom_format_scores(self, instance: str) -> dict:
        resolved = self._resolve_instance(instance)
        custom_formats = self.radarr_api._make_request(resolved, "customformat", fallback=[]) or []
        if not custom_formats:
            return {}
        scores = {cf["name"]: cf.get("score", 0) for cf in custom_formats}
        self.logger.log_info(f"Retrieved {len(scores)} custom format scores from {resolved}")
        return scores

    @LoggerManager().log_function_entry
    @timeit("sync_all_custom_formats")
    def sync_all_custom_formats(self):
        """Sync deduplicated custom formats across all configured Radarr instances."""
        radarr_instances = list((self.config.get("radarr_instances") or {}).keys())
        if not radarr_instances:
            self.logger.log_warning("No Radarr instances configured for custom format sync")
            return

        instance_cf_map = {}
        for instance in radarr_instances:
            resolved = self._resolve_instance(instance)
            raw = self.radarr_api._make_request(resolved, "customformat", fallback=[]) or []
            instance_cf_map[resolved] = self.deduplicate_custom_formats(raw)

        # Build master unique set
        all_unique_cfs: list = []
        for cf_list in instance_cf_map.values():
            for cf in cf_list:
                if not self.custom_format_exists(cf, all_unique_cfs):
                    all_unique_cfs.append(cf)

        # Sync to each instance
        for instance, existing_cfs in instance_cf_map.items():
            for cf in all_unique_cfs:
                if not self.custom_format_exists(cf, existing_cfs):
                    self.add_custom_format(instance, cf)

    @LoggerManager().log_function_entry
    @timeit("get_profile_scores_by_format")
    def get_profile_scores_by_format(self, instance: str) -> dict:
        resolved = self._resolve_instance(instance)
        profiles = self.radarr_api._make_request(resolved, "qualityprofile", fallback=[]) or []
        format_scores: dict = {}
        for profile in profiles:
            for fmt in profile.get("formatItems", []):
                fmt_id = fmt.get("format")
                if isinstance(fmt_id, dict):
                    fmt_id = fmt_id.get("id")
                if fmt_id is not None and fmt.get("score", 0) != 0:
                    format_scores[fmt_id] = format_scores.get(fmt_id, 0) + fmt.get("score", 0)
        self.logger.log_info(f"Compiled format usage across {len(profiles)} profiles in {resolved}")
        return format_scores
