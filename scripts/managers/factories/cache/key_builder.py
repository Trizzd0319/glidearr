# beta/managers/factories/cache/key_builder.py

import re
from pathlib import Path
from typing import Optional

from scripts.support.config.cache_keys import CacheKeyPaths

# Characters allowed verbatim in a cache path component. Anything else is
# replaced with "_" to neutralise path-traversal / injection primitives.
# Legitimate keys (integer ids, single letters, dotted keys such as
# "radarr.movies.standard.full") consist solely of these characters, so
# sanitisation is a no-op for them and existing paths are preserved.
_SAFE_PART = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_part(part: str) -> str:
    """
    Sanitise a single cache-path component.

    Raises ValueError for components that cannot be safely used as a path
    segment (empty, absolute, or a parent reference). All other characters
    outside the safe set are replaced with "_".
    """
    cleaned = _SAFE_PART.sub("_", str(part))
    if cleaned in ("", ".", "..") or part in ("", ".", ".."):
        raise ValueError(f"Unsafe cache path component: {part!r}")
    if Path(part).is_absolute() or part.startswith(("/", "\\")):
        raise ValueError(f"Absolute cache path component not allowed: {part!r}")
    return cleaned


class CacheKeyBuilder:
    """
    Responsible for constructing structured cache file paths and string-based cache keys.
    Ensures consistent path handling for JSON, Parquet, CSV, and timestamp-based cache files.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = Path(base_dir or Path(__file__).resolve().parents[3] / "support" / "cache").resolve()

    # ─────────────────────────────────────────────────────────────
    # 📁 Disk File Path Builders
    # ─────────────────────────────────────────────────────────────

    def build_cache_path(self, *parts: str, suffix: str = ".json") -> Path:
        """
        Constructs a full Path object for a JSON (or other) cache file.
        Creates parent directories automatically.

        Each component is sanitised (see ``_sanitize_part``) to close the
        path-traversal primitive that arises when externally-controlled values
        (e.g. a Trakt username) flow into path components. After construction
        the resolved path is asserted to stay within ``base_dir``; any escape
        attempt raises ValueError rather than touching the filesystem.
        """
        safe_parts = [_sanitize_part(p) for p in parts]
        path = self.base_dir.joinpath(*safe_parts)

        # APPEND the suffix rather than replacing the trailing dot-segment.
        # Path.with_suffix treats everything after the final dot as an extension
        # and swaps it out, so a single dotted key part like
        # "radarr.tags.standard" had ".standard" stripped → "radarr.tags.json",
        # collapsing every per-instance key (standard/ultra/test) onto one shared
        # file and cross-contaminating instances. Appending keeps the whole key in
        # the filename; the endswith guard avoids doubling a suffix that a caller
        # already baked into the key (e.g. "...library_series_enriched.parquet" or
        # a "<name>.last_updated" timestamp key), which with_suffix used to absorb.
        if not path.name.endswith(suffix):
            path = path.with_name(path.name + suffix)

        if not path.resolve().is_relative_to(self.base_dir):
            raise ValueError(
                f"Refusing cache path outside base directory: {path}"
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def build_parquet_path(self, *parts: str, suffix: str = ".parquet") -> Path:
        """Constructs full Parquet file path under base cache directory."""
        return self.build_cache_path(*parts, suffix=suffix)

    def build_csv_path(self, *parts: str, suffix: str = ".csv") -> Path:
        """Constructs full CSV file path under base cache directory."""
        return self.build_cache_path(*parts, suffix=suffix)

    # ─────────────────────────────────────────────────────────────
    # 🔑 Cache Key Generators (String Keys)
    # ─────────────────────────────────────────────────────────────

    def format_cache_key(self, *parts: str) -> str:
        """Formats a slash-separated cache key from provided parts."""
        return "/".join(parts)

    def build_json_cache_key(self, service: str, instance: str, resource: str) -> str:
        """Generates a standard JSON cache key string for service/instance/resource."""
        return f"{service}/{instance}/{resource}.json"

    def build_timestamp_key(self, service: str, instance: str, category: str) -> str:
        """Generates a timestamp-specific cache key path."""
        return f"{service}/{instance}/{category}/last_updated.last_updated"

    def build_future_episodes_cache_key(self, instance: str) -> str:
        """Builds the key for Sonarr future episodes cache (dynamic replacement)."""
        key_template = CacheKeyPaths.sonarr.FUTURE_EPISODES.replace("<instance>", instance)
        return self.format_cache_key(*key_template.split("/"))

    # ─────────────────────────────────────────────────────────────
    # 🧪 Testing Utilities (Optional)
    # ─────────────────────────────────────────────────────────────

    def get_base_directory(self) -> Path:
        """Returns the absolute root cache directory."""
        return self.base_dir
