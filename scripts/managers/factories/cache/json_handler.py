# beta/managers/factories/cache/json_handler.py

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from scripts.managers.factories.cache.constants import CacheSuffix
from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
from scripts.support.utilities.json_utils import make_json_safe


class JsonSanitizer:
    """Utility to recursively convert objects into JSON-serializable formats."""

    @classmethod
    def sanitize(cls, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: cls.sanitize(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [cls.sanitize(i) for i in obj]
        elif isinstance(obj, datetime):
            return obj.isoformat()
        else:
            return obj


class CacheJsonManager:
    """
    Manages all JSON cache operations:
    - get/set/delete via keys
    - pretty output
    - load or initialize fallback
    """

    def __init__(self, logger=None, base_dir: Union[str, Path] = "support/cache"):
        self.logger = logger or print
        self.key_builder = CacheKeyBuilder(base_dir)

    # ─────────────────────────────────────────────────────────────
    # 🔐 JSON Read Operations
    # ─────────────────────────────────────────────────────────────

    def get(self, key: str) -> dict:
        """Retrieve JSON object from a cache key."""
        path = self._resolve_path(key)
        return self.load_json(path)

    def exists(self, key_or_path: Union[str, Path]) -> bool:
        """Check whether a cache file exists (by key or direct path)."""
        path = self._resolve_path(key_or_path) if isinstance(key_or_path, str) else key_or_path
        return path.is_file()

    def load_json(self, path: Path) -> dict:
        """Load JSON from a path, or return empty dict if missing / empty / corrupt."""
        # A 0-byte file is NOT "corrupt JSON" — it's a killed write, a partial restore,
        # or a file-sync (OneDrive) dehydration. Treat it as a clean cache MISS at DEBUG
        # (not a scary warning), so the caller re-fetches + overwrites it and a
        # bulk-poisoned cache can't flood the log with thousands of decode warnings.
        try:
            if path.stat().st_size == 0:
                self.logger.log_debug(f"📭 Empty (0-byte) JSON cache, treating as miss: {path}")
                return {}
        except FileNotFoundError:
            self.logger.log_debug(f"📭 JSON cache not found: {path}")
            return {}
        except OSError:
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.log_debug(f"📭 JSON cache not found: {path}")
        except json.JSONDecodeError as e:
            self.logger.log_warning(f"⚠️ Failed to decode JSON at {path}: {e}")
        except Exception as e:
            self.logger.log_warning(f"⚠️ Unexpected error loading JSON at {path}: {e}")
        return {}

    def load_or_initialize(self, key: str, default: Optional[dict] = None) -> dict:
        """
        Loads cache by key, or initializes it with default if missing/corrupt.
        """
        path = self._resolve_path(key)
        data = self.load_json(path)
        if not data:
            self.logger.log_info(f"📂 Initializing empty cache: {path}")
            self.save_json(path, default or {}, indent=2)
            return default or {}
        return data

    # ─────────────────────────────────────────────────────────────
    # 💾 JSON Write Operations
    # ─────────────────────────────────────────────────────────────

    def save_json(self, path: Path, data: dict, compressed=False, indent=None):
        data = make_json_safe(data)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
        return True

    def set(self, key: str, data: dict, indent: Optional[int] = None) -> bool:
        """Save sanitized JSON data to file by key."""
        path = self._resolve_path(key)
        return self.save_json(path, data, indent=indent)

    def set_with_pretty_output(self, path: Path, data, compressed: bool = False):
        return self.save_json(path, data, compressed=compressed, indent=2)

    # ─────────────────────────────────────────────────────────────
    # 🗑 JSON Cleanup Operations
    # ─────────────────────────────────────────────────────────────

    def delete(self, key_or_path: Union[str, Path]) -> bool:
        """Delete a JSON file by key or path."""
        path = self._resolve_path(key_or_path) if isinstance(key_or_path, str) else key_or_path
        try:
            if path.exists():
                path.unlink()
                self.logger.log_info(f"🗑 Deleted cache file: {path}")
                return True
            return False
        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed to delete JSON file at {path}: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # 🛠 Internal Utilities
    # ─────────────────────────────────────────────────────────────

    def _resolve_path(self, key: str) -> Path:
        """Convert a cache key to a JSON file path."""
        return self.key_builder.build_cache_path(*key.split("/"), suffix=CacheSuffix.JSON.value)
