from pathlib import Path

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairStorageManager(BaseManager, ComponentManagerMixin):
    """
    Validates root folder mappings, identifies misaligned storage paths,
    detects phantom/missing folders, and attempts to repair invalid mappings.
    """

    @LoggerManager().log_function_entry
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrRepair"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager")
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.sonarr_api:
            raise ValueError("❌ SonarrRepairStorageManager could not resolve a valid Sonarr API interface.")

        self.logger.log_debug(f"🛠️ Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("repair_storage_paths")
    def repair_storage_paths(self):
        """
        Audits storage mappings in Sonarr to identify missing, incorrect,
        or misaligned root folders or paths.
        """
        self.logger.log_info("🔍 Auditing Sonarr root folder storage mappings...")
        repaired = 0
        skipped = 0

        for instance_name, api in self.sonarr_api.get_all_sonarr_apis().items():
            self.logger.log_info(f"📦 Auditing storage for instance: {instance_name}")
            try:
                valid_roots = {Path(folder.path).resolve(): folder for folder in api.root_folder.all()}

                for series in api.series.all():
                    current_path = Path(series.path).resolve()
                    matched_root = next((r for r in valid_roots if current_path.is_relative_to(r)), None)

                    if not matched_root:
                        msg = f"⚠️ Series {series.title} path '{current_path}' is not mapped to a valid root folder"
                        if self.dry_run:
                            self.logger.log_warning(msg + " [dry-run]")
                        else:
                            self.logger.log_error(msg + " — no safe remapping logic implemented yet.")
                        skipped += 1
                    else:
                        self.logger.log_debug(f"✅ Valid root mapping for {series.title}: {matched_root}")
                        skipped += 1

            except Exception as e:
                self.logger.log_error(f"❌ Failed to audit storage for instance '{instance_name}': {e}")

        self.logger.log_info(f"📦 Storage repair summary → Repaired: {repaired}, Skipped: {skipped}")

    @LoggerManager().log_function_entry
    @timeit("repair_symlinks")
    def repair_symlinks(self):
        """
        Detects broken symlinks inside Sonarr storage paths and removes or logs them.
        """
        self.logger.log_info("🔗 Auditing for broken symlinks in storage paths...")

        broken_symlinks = []

        for instance_name, api in self.sonarr_api.get_all_sonarr_apis().items():
            for folder in api.root_folder.all():
                path = Path(folder.path)
                if not path.exists():
                    continue

                for sub in path.rglob("*"):
                    if sub.is_symlink() and not sub.resolve(strict=False).exists():
                        broken_symlinks.append(sub)

        if not broken_symlinks:
            self.logger.log_info("✅ No broken symlinks detected.")
            return

        self.logger.log_warning(f"⚠️ Found {len(broken_symlinks)} broken symlinks")

        for symlink in broken_symlinks:
            if self.dry_run:
                self.logger.log_warning(f"🧪 Would remove: {symlink}")
            else:
                try:
                    symlink.unlink()
                    self.logger.log_info(f"🧹 Removed broken symlink: {symlink}")
                except Exception as e:
                    self.logger.log_error(f"❌ Failed to remove {symlink}: {e}")
