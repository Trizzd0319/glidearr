from datetime import datetime, timezone

import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrQualityCustomFormatsManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrQuality"
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
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.key_builder = kwargs.get("key_builder", getattr(parent, "key_builder", None))
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🛠️ Initialized {class_name} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("get_custom_formats")
    def get_custom_formats(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        return self.global_cache.get_or_generate_cache(
            key=CacheKeyPaths.sonarr.CUSTOM_FORMATS,
            generator_function=lambda: self.sonarr_api._make_request(resolved_instance, "customformat") or [],
        )

    @LoggerManager().log_function_entry
    @timeit("add_custom_format")
    def add_custom_format(self, instance, custom_format_payload):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        self.logger.log_info(f"➕ Adding custom format to {resolved_instance}.")
        return self.sonarr_api._make_request(resolved_instance, "customformat", method="POST", payload=custom_format_payload)

    @LoggerManager().log_function_entry
    @timeit("get_custom_format_scores")
    def get_custom_format_scores(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        self.logger.log_info(f"📊 Fetching Custom Format scores from {resolved_instance}...")

        custom_formats = self.sonarr_api._make_request(resolved_instance, "customformat") or []
        if not custom_formats:
            self.logger.log_info(f"⚠️ No Custom Formats found in {resolved_instance}. Returning empty scores.")
            return {}

        scores = {cf["name"]: cf.get("score", 0) for cf in custom_formats}
        self.logger.log_info(f"✅ Retrieved {len(scores)} Custom Format scores from {resolved_instance}.")
        return scores

    @LoggerManager().log_function_entry
    @timeit("sync_custom_formats")
    def sync_custom_formats(self, instance, new_custom_formats):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        self.logger.log_info(f"🔁 Syncing custom formats to {resolved_instance}...")

        existing_cfs = self.sonarr_api._make_request(resolved_instance, "customformat") or []
        existing_cfs = self._deduplicate_custom_formats(existing_cfs)

        synced_count = 0
        skipped_count = 0

        for cf in new_custom_formats:
            name = cf.get("name", "Unnamed")
            if self._custom_format_exists(name, existing_cfs):
                self.logger.log_info(f"⏭️ Custom Format '{name}' already exists in {resolved_instance}. Skipping.")
                skipped_count += 1
                continue

            result = self.add_custom_format(resolved_instance, cf)
            if result:
                self.logger.log_info(f"✅ Added Custom Format '{name}' to {resolved_instance}.")
                synced_count += 1
            else:
                self.logger.log_warning(f"❌ Failed to add Custom Format '{name}' to {resolved_instance}.")

        self.logger.log_info(
            f"📦 Sync Complete for {resolved_instance} → Added: {synced_count} | Skipped: {skipped_count} | Total: {len(new_custom_formats)}"
        )

    @LoggerManager().log_function_entry
    @timeit("_custom_format_exists")
    def _custom_format_exists(self, name, formats):
        return any(cf.get("name", "").lower() == name.lower() for cf in formats)

    @LoggerManager().log_function_entry
    @timeit("_deduplicate_custom_formats")
    def _deduplicate_custom_formats(self, formats):
        seen = set()
        deduped = []
        for cf in formats:
            name = cf.get("name", "").lower()
            if name not in seen:
                seen.add(name)
                deduped.append(cf)
        return deduped

    @LoggerManager().log_function_entry
    @timeit("get_profile_scores_by_format")
    def get_profile_scores_by_format(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)

        profiles = self.sonarr_api._make_request(resolved_instance, "qualityProfile") or []
        format_scores = {}

        for profile in profiles:
            for fmt in profile.get("formatItems", []):
                fmt_id = (fmt.get("format") or {}).get("id")
                if fmt_id is not None and fmt.get("enabled"):
                    format_scores[fmt_id] = format_scores.get(fmt_id, 0) + fmt.get("score", 0)

        self.logger.log_info(f"📈 Compiled format usage across {len(profiles)} profiles.")
        return format_scores

    @LoggerManager().log_function_entry
    @timeit("log_unused_custom_formats")
    def log_unused_custom_formats(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)

        all_formats = self.sonarr_api._make_request(resolved_instance, "customformat") or []
        used_format_ids = set(self.get_profile_scores_by_format(resolved_instance).keys())

        unused = [cf for cf in all_formats if cf.get("id") not in used_format_ids]

        if not unused:
            self.logger.log_info(f"✅ All custom formats in {resolved_instance} are currently used by at least one profile.")
        else:
            self.logger.log_info(
                f"⚠️ {len(unused)} Custom Formats are not in use by any profile in {resolved_instance}."
            )
            for cf in unused:
                self.logger.log_info(f" - 🛌 Unused CF: '{cf.get('name')}' (ID: {cf.get('id')})")

    @LoggerManager().log_function_entry
    @timeit("run_custom_format_data_pull")
    def run_custom_format_data_pull(self, instance):
        all_instances = list(self.sonarr_api.get_all_sonarr_apis().items())

        for instance_name, arrapi_client in all_instances:
            instance_config = (self.config.get("sonarr_instances") or {}).get(instance_name)
            if not instance_config:
                self.logger.log_error(f"❌ No configuration found for instance '{instance_name}'")
                continue

            api_base = instance_config['base_url']
            api_key = instance_config['api']

            url = f"{api_base}/api/v3/customformat"
            params = {}

            try:
                response = requests.get(url, params=params, headers={"X-Api-Key": api_key})
                response.raise_for_status()
                custom_formats = response.json()
            except Exception as e:
                self.logger.log_error(f"❌ Failed to fetch custom formats for '{instance_name}': {e}")
                continue

            cache_key = self.key_builder.format_cache_key("sonarr", instance_name, "custom_formats")
            updated_cache = {
                "customFormats": custom_formats,
                "meta": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "instance": instance_name,
                    "count": len(custom_formats)
                }
            }

            self.global_cache.set_with_pretty_output(cache_key, updated_cache)
            self.logger.log_info(f"✅ Custom formats cached for {instance_name} ({len(custom_formats)} formats)")
