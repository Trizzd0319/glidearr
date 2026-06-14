from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairQualityManager(BaseManager, ComponentManagerMixin):
    """
    Handles auditing and repairing of quality profile mismatches between
    configured expectations and Sonarr series metadata or folder logic.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrRepair"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.sonarr_api = kwargs.get("sonarr_api") or kwargs.get("api") or getattr(self.registry.get("manager", self.parent_name), "api", None)
        self.manager = kwargs.get("manager")
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.sonarr_api:
            raise ValueError("❌ SonarrRepairQualityManager could not resolve a valid API interface.")

        self.logger.log_debug(f"🛠️ Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("repair_quality_profiles")
    def repair_quality_profiles(self):
        """
        Audit and repair quality profile mismatches across Sonarr instances.
        """
        self.logger.log_info("🔍 Auditing Sonarr quality profile assignments...")
        repaired = 0
        skipped = 0

        for instance_name, api in self.sonarr_api.get_all_sonarr_apis().items():
            self.logger.log_info(f"📦 Auditing quality profiles in instance: {instance_name}")

            try:
                expected_profiles = (self.config.get("sonarr") or {}).get("expected_quality_profiles", {})
                profile_map = {p.id: p.name for p in api.profiles.all()}
                expected_map = {v: k for k, v in profile_map.items()}

                for series in api.series.all():
                    expected = expected_profiles.get(series.path.lower())
                    actual = profile_map.get(series.qualityProfileId)

                    if expected and actual and expected != actual:
                        msg = f"⚠️ Quality mismatch for {series.title}: Expected '{expected}', found '{actual}'"
                        if self.dry_run:
                            self.logger.log_warning(msg + " [dry-run]")
                        else:
                            new_id = expected_map.get(expected)
                            if new_id:
                                series.qualityProfileId = new_id
                                api.series.update(series)
                                self.logger.log_info(f"✅ Updated {series.title} → Profile: {expected}")
                                repaired += 1
                            else:
                                self.logger.log_error(f"❌ Unknown expected profile name '{expected}'")
                                skipped += 1
                    else:
                        skipped += 1

            except Exception as e:
                self.logger.log_error(f"❌ Failed to audit quality in instance '{instance_name}': {e}")

        self.logger.log_info(f"🔧 Quality repair summary → Repaired: {repaired}, Skipped: {skipped}")
