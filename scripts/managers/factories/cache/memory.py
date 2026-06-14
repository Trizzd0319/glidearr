# beta/managers/factories/cache/memory_manager.py

from datetime import datetime, timedelta
from typing import Any, Optional

from scripts.support.utilities.logger.logger import LoggerManager


class MemoryManager:
    """
    Provides an in-memory, TTL-aware cache system for runtime use.
    Useful for avoiding repeated disk or API lookups during execution.
    """

    def __init__(self, logger=None):
        self.logger = logger or LoggerManager()
        self._cache = {}
        self._timestamps = {}
        self._access_counts = {}

    # ─────────────────────────────────────────────────────────────
    # 📦 Get/Set with TTL Support
    # ─────────────────────────────────────────────────────────────

    def set(self, key: str, value: Any):
        self._cache[key] = value
        self._timestamps[key] = datetime.utcnow()
        self._access_counts[key] = 0
        self.logger.log_debug(f"🧠 Cached in memory: {key}")

    def get(self, key: str, max_age_seconds: Optional[int] = None) -> Optional[Any]:
        if key not in self._cache:
            return None

        if max_age_seconds is not None:
            age = (datetime.utcnow() - self._timestamps[key]).total_seconds()
            if age > max_age_seconds:
                self.logger.log_debug(f"⏳ Memory cache expired for {key} ({age:.1f}s)")
                self.invalidate(key)
                return None

        self._access_counts[key] += 1
        return self._cache[key]

    def exists(self, key: str) -> bool:
        return key in self._cache

    def age_seconds(self, key: str) -> Optional[float]:
        if key in self._timestamps:
            return (datetime.utcnow() - self._timestamps[key]).total_seconds()
        return None

    def get_access_count(self, key: str) -> int:
        return self._access_counts.get(key, 0)

    # ─────────────────────────────────────────────────────────────
    # 🧹 Cleanup / Inspection
    # ─────────────────────────────────────────────────────────────

    def invalidate(self, key: str):
        self._cache.pop(key, None)
        self._timestamps.pop(key, None)
        self._access_counts.pop(key, None)
        self.logger.log_debug(f"🗑 Invalidated memory cache: {key}")

    def clear(self):
        self._cache.clear()
        self._timestamps.clear()
        self._access_counts.clear()
        self.logger.log_info("🧼 Cleared all in-memory cache")

    def keys(self):
        return list(self._cache.keys())

    def summary(self):
        self.logger.log_info("📊 Memory Cache Summary:")
        for key in self._cache:
            age = self.age_seconds(key)
            count = self.get_access_count(key)
            self.logger.log_info(f"   • {key}: {age:.1f}s old, {count} hits")

    def size(self) -> int:
        return len(self._cache)
