from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairTagsManager(BaseManager, ComponentManagerMixin):
    """
    Handles repair operations related to tags in Sonarr.
    Can remove unused tags and remap tag IDs across series.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None,
                 sonarr_api=None, instance_manager=None, cache_manager=None, dry_run=False, **kwargs):
        self.parent_name = "SonarrRepair"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.sonarr_api = sonarr_api or getattr(self.registry.get("manager", self.parent_name), "api", None)
        self.instance_manager = instance_manager or getattr(self.registry.get("manager", self.parent_name), "instance_manager", None)
        self.sonarr_cache = cache_manager or getattr(self.registry.get("manager", self.parent_name), "sonarr_cache", None)
        self.dry_run = dry_run or getattr(self.registry.get("manager", self.parent_name), "dry_run", False)

        if not self.sonarr_api or not self.instance_manager:
            raise ValueError("❌ SonarrRepairTagsManager requires API and instance manager references.")

        self.logger.log_debug(f"🛠️ Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("repair_unused_tags")
    def repair_unused_tags(self, instance_name):
        """
        Remove unused or orphaned tags from Sonarr.
        """
        instance = self.instance_manager.resolve_instance(instance_name)
        api = self.sonarr_api.get_all_sonarr_apis()[instance]

        tags = api.get_tags()
        series = api.all_series()

        used_tag_ids = {tid for s in series for tid in s.tags}
        unused_tags = [t for t in tags if t.id not in used_tag_ids]

        if not unused_tags:
            self.logger.log_info(f"✅ No unused tags found for instance: {instance}")
            return

        for tag in unused_tags:
            if self.dry_run:
                self.logger.log_info(f"[DRY-RUN] Would delete unused tag '{tag.label}' (ID {tag.id})")
            else:
                try:
                    self.logger.log_info(f"🧹 Deleting unused tag '{tag.label}' (ID {tag.id})")
                    api.delete_tag(tag.id)
                except Exception as e:
                    self.logger.log_error(f"❌ Failed to delete tag '{tag.label}' (ID {tag.id}): {e}")

    @LoggerManager().log_function_entry
    @timeit("repair_tag_map")
    def repair_tag_map(self, instance_name, tag_map):
        """
        Remap old → new tags across all series (e.g. {"web-dl": "web", "uhd": "4k"}).
        """
        instance = self.instance_manager.resolve_instance(instance_name)
        api = self.sonarr_api.get_all_sonarr_apis()[instance]

        tag_lookup = {tag.label.lower(): tag for tag in api.get_tags()}
        missing_tags = []

        # Validate tag mapping
        for old_tag, new_tag in tag_map.items():
            if old_tag.lower() not in tag_lookup:
                self.logger.log_warning(f"⚠️ Old tag '{old_tag}' not found in instance {instance}")
                missing_tags.append(old_tag)
            if new_tag.lower() not in tag_lookup:
                self.logger.log_warning(f"⚠️ New tag '{new_tag}' not found in instance {instance}")
                missing_tags.append(new_tag)

        if missing_tags:
            self.logger.log_warning(f"⚠️ Skipping remap due to missing tags: {missing_tags}")
            return

        series_list = api.all_series()
        for series in series_list:
            updated = False
            new_tags = []

            for tid in series.tags:
                tag = next((t for t in tag_lookup.values() if t.id == tid), None)
                if not tag:
                    continue

                label = tag.label.lower()
                if label in tag_map:
                    remapped = tag_lookup.get(tag_map[label].lower())
                    if remapped:
                        new_tags.append(remapped.id)
                        updated = True
                else:
                    new_tags.append(tid)

            if updated:
                if self.dry_run:
                    self.logger.log_info(f"[DRY-RUN] Would remap tags for '{series.title}' → {new_tags}")
                else:
                    try:
                        self.logger.log_info(f"🔁 Updating tags for '{series.title}' → {new_tags}")
                        api.update_series(series.id, {"tags": new_tags})
                    except Exception as e:
                        self.logger.log_error(f"❌ Failed to update tags for '{series.title}': {e}")
