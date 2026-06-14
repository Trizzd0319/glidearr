from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrCustomFormatsManager(BaseManager, ComponentManagerMixin):
    """
    Fetches, caches, and synchronises custom formats for Radarr quality profiles.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrQualityManager"
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
            self.logger.log_info(
                f"[dry_run] Would add custom format '{custom_format_payload.get('name')}' to {resolved}"
            )
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
    @timeit("sync_custom_formats")
    def sync_custom_formats(self, instance: str, new_custom_formats: list):
        resolved = self._resolve_instance(instance)
        self.logger.log_info(f"Syncing custom formats to {resolved}...")

        existing_cfs = self._deduplicate_custom_formats(
            self.radarr_api._make_request(resolved, "customformat", fallback=[]) or []
        )

        synced_count, skipped_count = 0, 0
        for cf in new_custom_formats:
            name = cf.get("name", "Unnamed")
            if self._custom_format_exists(name, existing_cfs):
                self.logger.log_debug(f"Custom Format '{name}' already exists in {resolved}. Skipping.")
                skipped_count += 1
                continue

            result = self.add_custom_format(resolved, cf)
            if result:
                self.logger.log_info(f"Added Custom Format '{name}' to {resolved}")
                synced_count += 1
            else:
                self.logger.log_warning(f"Failed to add Custom Format '{name}' to {resolved}")

        self.logger.log_info(
            f"Sync complete for {resolved} → Added: {synced_count} | Skipped: {skipped_count}"
        )

    def _custom_format_exists(self, name: str, formats: list) -> bool:
        return any(cf.get("name", "").lower() == name.lower() for cf in formats)

    def _deduplicate_custom_formats(self, formats: list) -> list:
        seen, deduped = set(), []
        for cf in formats:
            name = cf.get("name", "").lower()
            if name not in seen:
                seen.add(name)
                deduped.append(cf)
        return deduped

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
                score = fmt.get("score", 0)
                if fmt_id is not None and score != 0:
                    format_scores[fmt_id] = format_scores.get(fmt_id, 0) + score

        self.logger.log_info(f"Compiled format usage across {len(profiles)} profiles in {resolved}")
        return format_scores

    @LoggerManager().log_function_entry
    @timeit("log_unused_custom_formats")
    def log_unused_custom_formats(self, instance: str):
        resolved = self._resolve_instance(instance)
        all_formats = self.radarr_api._make_request(resolved, "customformat", fallback=[]) or []
        used_format_ids = set(self.get_profile_scores_by_format(resolved).keys())

        unused = [cf for cf in all_formats if cf.get("id") not in used_format_ids]
        if not unused:
            self.logger.log_info(f"All custom formats in {resolved} are used by at least one profile.")
        else:
            self.logger.log_info(f"{len(unused)} custom formats not used in any profile in {resolved}:")
            for cf in unused:
                self.logger.log_info(f"  Unused CF: '{cf.get('name')}' (ID: {cf.get('id')})")
