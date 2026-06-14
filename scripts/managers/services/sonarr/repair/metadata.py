from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairMetadataManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrRepair"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()
        self.logger.log_debug(f"🛠️ Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("repair_missing_metadata")
    def repair_missing_metadata(self, series_list):
        self.logger.log_info("🔍 Scanning for incomplete metadata...")
        repaired = []

        for series in series_list:
            if not series.get("tvdbId") or not series.get("title") or not series.get("year"):
                series_id = series.get("id")
                self.logger.log_info(f"🔧 Repairing metadata for series ID {series_id}...")

                fixed = {
                    "tvdbId": series.get("tvdbId") or 999999,
                    "title": series.get("title") or "Unknown Title",
                    "year": series.get("year") or 2000
                }

                repaired.append((series_id, fixed))
                self.logger.log_debug(f"✅ Metadata patched for {series_id}: {fixed}")
            else:
                self.logger.log_debug(f"✅ Metadata intact for {series.get('id')}")

        self.logger.log_info(f"🔎 Metadata repair complete. Total repaired: {len(repaired)}")
        return repaired
