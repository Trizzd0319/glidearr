# beta/managers/factories/cache/timestamp_handler.py

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from scripts.managers.factories.cache.constants import CacheSuffix
from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
from scripts.support.utilities.logger.logger import LoggerManager


class CacheTimestampManager:
    """
    Manages reading and writing last-updated timestamps for cache resources.
    """

    def __init__(self, logger=None, key_builder: Optional[CacheKeyBuilder] = None):
        self.logger = logger or LoggerManager()
        self.key_builder = key_builder or CacheKeyBuilder()

    # ─────────────────────────────────────────────────────────────
    # 📝 Timestamp Writers
    # ─────────────────────────────────────────────────────────────

    def update_timestamp(self, service: str, instance: str, category: str) -> bool:
        """
        Writes the current UTC timestamp to disk for a given service/instance/category.
        """
        path = self._resolve_timestamp_path(service, instance, category)
        timestamp = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(timestamp)
            self.logger.log_debug(f"⏱️ Updated timestamp → {path}: {timestamp}")
            return True
        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed to write timestamp to {path}: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # 🕒 Timestamp Readers
    # ─────────────────────────────────────────────────────────────

    def read_timestamp(self, path: Union[str, Path]) -> Optional[datetime]:
        """
        Reads and parses a timestamp from a given file path.
        """
        try:
            path = Path(path)
            if not path.exists():
                return None
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            return datetime.fromisoformat(raw)
        except Exception as e:
            self.logger.log_warning(f"⚠️ Could not read timestamp at {path}: {e}")
            return None

    def read_timestamp_by_key(self, service: str, instance: str, category: str) -> Optional[datetime]:
        """
        Reads a timestamp from the standard key path for the given cache category.
        """
        path = self._resolve_timestamp_path(service, instance, category)
        return self.read_timestamp(path)

    # ─────────────────────────────────────────────────────────────
    # 🧪 Timestamp Validators / Helpers
    # ─────────────────────────────────────────────────────────────

    def is_fresh(self, service: str, instance: str, category: str, max_age_seconds: int) -> bool:
        """
        Determines if the cached timestamp is fresh within the specified time window.
        """
        ts = self.read_timestamp_by_key(service, instance, category)
        if ts is None:
            return False
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age <= max_age_seconds

    def get_age_seconds(self, service: str, instance: str, category: str) -> Optional[int]:
        """
        Returns the number of seconds since the cache was last updated.
        """
        ts = self.read_timestamp_by_key(service, instance, category)
        if ts is None:
            return None
        return int((datetime.now(timezone.utc) - ts).total_seconds())

    # ─────────────────────────────────────────────────────────────
    # 🔐 Internal Helpers
    # ─────────────────────────────────────────────────────────────

    def _resolve_timestamp_path(self, service: str, instance: str, category: str) -> Path:
        key = f"{service}/{instance}/{category}/last_updated"
        return self.key_builder.build_cache_path(*key.split("/"), suffix=CacheSuffix.LAST_UPDATED.value)
