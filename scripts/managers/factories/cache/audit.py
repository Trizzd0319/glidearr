# beta/managers/factories/cache/audit.py

import os
from datetime import datetime
from pathlib import Path
from typing import Union

from scripts.support.utilities.logger.logger import LoggerManager


class CacheAuditManager:
    """
    Audits disk-based cache: lists file paths, sizes, types, and last modified timestamps.
    """

    def __init__(self, logger=None, base_dir: Union[str, Path] = "support/cache"):
        self.logger = logger or LoggerManager()
        self.base_dir = Path(base_dir).resolve()

    def summarize(self, extensions: tuple = (".json", ".json.gz", ".parquet", ".csv", ".last_updated")):
        """
        Logs a summary of all cache files under the base directory.
        """
        if not self.base_dir.exists():
            self.logger.log_warning(f"⚠️ Cache base directory not found: {self.base_dir}")
            return

        self.logger.log_info(f"📊 Cache Audit for: {self.base_dir}")

        total_files = 0
        total_bytes = 0

        for path in self.base_dir.rglob("*"):
            if not path.is_file() or not path.suffix in extensions:
                continue

            total_files += 1
            size_kb = path.stat().st_size / 1024
            total_bytes += path.stat().st_size
            mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat()

            self.logger.log_info(f"   • {path.relative_to(self.base_dir)} | {size_kb:.1f} KB | Last Modified: {mtime}")

        if total_files == 0:
            self.logger.log_info("ℹ️ No matching cache files found.")
        else:
            self.logger.log_info(f"✅ Found {total_files} cache files totaling {total_bytes/1024:.1f} KB.")

    def delete_all(self, confirm: bool = False):
        """
        Deletes all cache files in the base directory.
        Use with caution.
        """
        if not confirm:
            self.logger.log_warning("❌ Deletion aborted — confirmation flag not set.")
            return

        count = 0
        for path in self.base_dir.rglob("*"):
            if path.is_file():
                try:
                    path.unlink()
                    count += 1
                except Exception as e:
                    self.logger.log_warning(f"⚠️ Failed to delete {path}: {e}")

        self.logger.log_info(f"🧹 Deleted {count} cache files under {self.base_dir}")
