from datetime import datetime, timezone

import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache import CacheKeyBuilder
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class SonarrSyncTagsManager(BaseManager, ComponentManagerMixin):
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
        self.instance_manager = kwargs.get("instance_manager", getattr(parent, "instance_manager", None))
        self.key_builder = CacheKeyBuilder()

        self.master_tag_set = set()
        self.global_tag_map = {}
        self.keep_tagged_series = set()
        self._keep_set_loaded = False

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")

    def _normalize_tags(self, tag_list):
        """Ensures tag values are stringified consistently."""
        return [tag["label"] if isinstance(tag, dict) else str(tag) for tag in tag_list]

    def refresh_tags_across_instances(self):
        self.logger.log_info("🔄 Refreshing tags across all Sonarr instance...")
        all_instances = self.instance_manager.get_all_sonarr_apis()
        master_tag_set = set()
        unified_tag_map = {}

        for instance_name, api in all_instances.items():
            series_list = api.get_all_series()
            for series in series_list:
                series_id = series['id']
                tags = self._normalize_tags(series.get('tags', []))
                unified_tag_map[series_id] = tags
                master_tag_set.update(tags)
                if 'keep' in tags:
                    self.keep_tagged_series.add(series_id)

        self.global_tag_map = unified_tag_map
        self.master_tag_set = master_tag_set
        self.logger.log_info(f"✅ Collected master tag set: {sorted(self.master_tag_set)}")

    def ensure_keep_set(self, force: bool = False):
        """
        Populate ``keep_tagged_series`` from the cached tag list + the series
        cache, matching on the 'keep' tag **ID** (Sonarr series carry integer tag
        IDs, not labels — the old 'keep' in labels check never matched).

        Lazy + idempotent. Only marks itself loaded once it has actually scanned
        series, so it retries on a later call if the caches weren't warm yet.
        Read-only — never writes tags.
        """
        if self._keep_set_loaded and not force:
            return

        series_mgr = self.registry.get("manager", "SonarrCacheSeriesManager") if self.registry else None
        if series_mgr is None or not hasattr(series_mgr, "iter_all_series"):
            return  # series cache not ready — retry on a later call

        instances = []
        try:
            if self.instance_manager:
                instances = list(self.instance_manager.get_all_sonarr_apis().keys())
        except Exception:
            instances = []
        if not instances:
            return  # no instances resolved yet

        tag_mgr = self.registry.get("manager", "SonarrCacheTagManager") if self.registry else None
        scanned_any = False

        for inst in instances:
            keep_ids = set()
            if tag_mgr and hasattr(tag_mgr, "get_keep_tag_ids"):
                try:
                    keep_ids = {k for k in (tag_mgr.get_keep_tag_ids(inst) or []) if k is not None}
                except Exception:
                    keep_ids = set()
            if not keep_ids and self.sonarr_api:  # live fallback
                try:
                    raw = self.sonarr_api._make_request(inst, "tag", fallback=[]) or []
                    keep_ids = {
                        t.get("id") for t in raw
                        if str(t.get("label", "")).lower() == "keep" and t.get("id") is not None
                    }
                except Exception:
                    keep_ids = set()

            try:
                series_iter = series_mgr.iter_all_series(inst)
            except Exception:
                continue

            count = 0
            for s in series_iter:
                count += 1
                if not isinstance(s, dict):
                    continue
                if keep_ids and any(t in keep_ids for t in (s.get("tags") or [])):
                    sid = s.get("id")
                    if sid is not None:
                        self.keep_tagged_series.add(sid)
            if count:
                scanned_any = True

        if scanned_any:
            self._keep_set_loaded = True
            self.logger.log_info(
                f"🔖 Keep-tag set loaded: {len(self.keep_tagged_series)} series tagged 'keep' "
                f"across {len(instances)} instance(s)"
            )

    def is_series_tagged_keep(self, series_id):
        if not self._keep_set_loaded:
            self.ensure_keep_set()
        return series_id in self.keep_tagged_series

    def get_series_with_tag(self, tag):
        return [sid for sid, tags in self.global_tag_map.items() if tag in tags]

    def sync_tags_across_instances(self):
        self.logger.log_info("🌐 Synchronizing tags across all Sonarr instance...")
        all_instances = self.instance_manager.get_all_sonarr_apis()
        for instance_name, api in all_instances.items():
            series_list = api.get_all_series()
            for series in series_list:
                current_tags = set(self._normalize_tags(series.get('tags', [])))
                updated_tags = list(current_tags.union(self.master_tag_set))
                api.update_series_tags(series['id'], updated_tags)
                self.logger.log_info(f"✅ Synced tags for series {series['id']} on {instance_name}")

    def run_tag_data_pull(self, instance):
        all_instances = list(self.sonarr_api.get_all_sonarr_apis().items())

        for instance_name, arrapi_client in all_instances:
            instance_config = (self.config.get("sonarr_instances") or {}).get(instance_name)
            if not instance_config:
                self.logger.log_error(f"❌ No configuration found for instance '{instance_name}'")
                continue

            api_base = instance_config['base_url']
            api_key = instance_config['api']

            url = f"{api_base}/api/v3/tag"
            params = {}

            try:
                response = requests.get(url, params=params, headers={"X-Api-Key": api_key})
                response.raise_for_status()
                tags = response.json()
            except Exception as e:
                self.logger.log_error(f"❌ Failed to fetch tags for '{instance_name}': {e}")
                continue

            serialized = [{"id": t.get("id"), "label": t.get("label")} for t in tags]

            cache_key = self.key_builder.format_cache_key("sonarr", instance_name, "tags")
            updated_cache = {
                "tags": serialized,
                "meta": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "instance": instance_name,
                    "count": len(serialized)
                }
            }

            self.global_cache.set_with_pretty_output(cache_key, updated_cache)
            self.logger.log_info(f"✅ Tags cached for {instance_name} ({len(serialized)} tags)")
