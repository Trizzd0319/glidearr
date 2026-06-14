# beta/managers/factories/cache.py

import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache.audit import CacheAuditManager
from scripts.managers.factories.cache.compressor import CacheCompressor
from scripts.managers.factories.cache.constants import (
    CacheSuffix,
    EnrichedSuffix,
    CacheKeyTemplate,
)
from scripts.managers.factories.cache.differ import CacheDiffer
from scripts.managers.factories.cache.json_handler import CacheJsonManager
from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
from scripts.managers.factories.cache.memory import MemoryManager
from scripts.managers.factories.cache.parquet_handler import CacheParquetManager
from scripts.managers.factories.cache.timestamp_handler import CacheTimestampManager
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.logger.run_summary import RunSummaryManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.json_utils import make_json_safe


class GlobalCacheManager(BaseManager):
    SUFFIX_MAP = {
        "series": EnrichedSuffix.SERIES,
        "episodes": EnrichedSuffix.EPISODES,
        "movies": EnrichedSuffix.MOVIES,
        "people": EnrichedSuffix.PEOPLE,
    }

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger: Optional[LoggerManager] = None, config: Optional[dict] = None, **kwargs):
        super().__init__(logger=logger, config=config, global_cache=None, **kwargs)

        self.key_builder = CacheKeyBuilder()
        self.cache_root = self.key_builder.base_dir
        self.json_handler = CacheJsonManager(logger=self.logger, base_dir=self.key_builder.base_dir)
        self.parquet_handler = CacheParquetManager(logger=self.logger)
        self.timestamp_handler = CacheTimestampManager(logger=self.logger, key_builder=self.key_builder)

        self.memory = MemoryManager(logger=self.logger)
        # Run-scoped collector for the end-of-run consolidated decision/movement tables.
        # Fresh per run (one GlobalCacheManager per process); managers route per-title detail
        # here instead of logging it inline, and Main.run() renders it once at the end.
        self.run_summary = RunSummaryManager(logger=self.logger)
        self.differ = CacheDiffer(logger=self.logger)
        self.audit = CacheAuditManager(logger=self.logger, base_dir=self.cache_root)
        self.compressor = CacheCompressor(logger=self.logger)

        self.logger.log_debug("✅ GlobalCacheManager initialized with all subcomponent handlers")

    # ---------- JSON CACHE ----------
    def get(self, key: str):
        return self.json_handler.get(key)

    def get_json(self, key: str):
        path = self.key_builder.build_cache_path(*key.split("/"), suffix=CacheSuffix.JSON.value)
        return self.json_handler.load_json(path)

    def set_json(self, key: str, data: dict, compressed=False, pretty=True):
        path = self.key_builder.build_cache_path(*key.split("/"), suffix=CacheSuffix.JSON.value)
        safe = make_json_safe(data)
        return self.json_handler.save_json(path, safe, compressed=compressed, indent=2 if pretty else None)

    def json_exists(self, key: str) -> bool:
        path = self.key_builder.build_cache_path(*key.split("/"), suffix=CacheSuffix.JSON.value)
        return path.exists()

    def set_with_pretty_output(self, key: str, data, compressed: bool = False):
        path = self.key_builder.build_cache_path(*key.split("/"), suffix=CacheSuffix.JSON.value)
        return self.json_handler.set_with_pretty_output(path, data, compressed=compressed)

    def get_or_generate_cache(
        self,
        key: str,
        generator_function,
        compressed: bool = False,
        pretty: bool = True,
        expiration_time: int = None,
        log_miss: bool = True,
        log_expired: bool = True,
        regenerate_on_expiry: bool = False,
    ):
        """
        Return cached JSON data when present, otherwise generate and cache it.

        log_miss/log_expired are intentionally caller-controlled so noisy bulk
        jobs, like PilotSearch runs over thousands of series, can suppress
        per-item cache messages while normal one-off calls remain verbose.

        regenerate_on_expiry controls what happens once ``expiration_time`` is
        exceeded. Historically this method logged "regenerating" but then fell
        through to the on-disk copy and served it ANYWAY — so every expiring key
        was effectively frozen at first write. Default False preserves that
        (serve-stale) behaviour for the many callers that relied on it. Pass
        True for data that must actually refresh on its TTL (e.g. watch history
        feeding the watched-set); the generator is then called on expiry, and if
        it returns None (e.g. a rate-limited Trakt fetch) the last-good copy is
        served instead of overwriting good data with nothing.
        """
        path = self.key_builder.build_cache_path(*key.split("/"), suffix=CacheSuffix.JSON.value)

        expired = False
        if expiration_time is not None and path.exists():
            file_age = time.time() - os.path.getmtime(path)
            if file_age <= expiration_time:
                return self.get(key)
            expired = True
            if log_expired:
                if regenerate_on_expiry:
                    self.logger.log_info(f"⏰ Expired cache for {key}, regenerating (age: {file_age:.1f}s)")
                else:
                    # Not opted into TTL refresh → the stale copy is served below.
                    # Say so honestly (and at debug — non-opted-in keys hit this on
                    # every access, so it shouldn't spam info-level "regenerating").
                    self.logger.log_debug(
                        f"⏰ Expired cache for {key}, serving stale "
                        f"(age: {file_age:.1f}s; no regenerate opt-in)"
                    )

        # Serve the on-disk copy unless this key opted into real TTL refresh AND
        # it is actually expired (regenerate_on_expiry). Without the opt-in this
        # short-circuit fires even for expired keys — the legacy serve-stale
        # behaviour every existing caller depends on.
        if self.json_exists(key) and not (expired and regenerate_on_expiry):
            return self.get(key)

        if log_miss:
            # DEBUG, not INFO: a cold rebuild misses thousands of per-series keys and
            # this once flooded the log with ~15k lines. Generators that warm many keys
            # show a tqdm bar instead; a genuinely interesting single miss is still in DEBUG.
            self.logger.log_debug(f"♻️ Cache miss for {key}, calling generator...")

        data = generator_function()

        # Generator failure (None) is NOT the same as a legitimately-empty
        # result ([]/{}). When the generator returns None — e.g. a Trakt fetch
        # that was rate-limited and skipped — don't overwrite a good cache with
        # nothing. Serve the last-good copy (even if expired) so the run keeps
        # going on slightly-stale data instead of empty.
        if data is None and path.exists():
            self.logger.log_warning(
                f"⚠️ Generator returned no data for {key}; serving last-good "
                f"cache (skipped overwrite)."
            )
            return self.get(key)

        # Cache empty lists/dicts too. A valid empty API response should not
        # become a permanent cache miss on every future bulk run.
        if data is not None:
            safe = make_json_safe(data)
            if pretty:
                self.set_with_pretty_output(key, safe, compressed=compressed)
            else:
                self.set_json(key, safe, compressed=compressed, pretty=False)
        else:
            # No fresh data AND no last-good copy to serve (the path.exists() branch
            # above didn't fire). Returning None is the legitimate cache-miss signal,
            # but surface it — a caller that len()/iterates the result must handle None.
            self.logger.log_debug(
                f"Cache '{key}': generator returned None with no prior copy — returning None."
            )
        return data

    # ---------- TIMESTAMP ----------
    def update_timestamp(self, service: str, instance: str, category: str) -> bool:
        key = CacheKeyTemplate.TIMESTAMP.format(service=service, instance=instance, name=category)
        path = self.key_builder.build_cache_path(*key.split("/"), suffix=CacheSuffix.LAST_UPDATED.value)
        return self.timestamp_handler.update_timestamp(path)

    def read_timestamp(self, service: str, instance: str, category: str):
        key = CacheKeyTemplate.TIMESTAMP.format(service=service, instance=instance, name=category)
        path = self.key_builder.build_cache_path(*key.split("/"), suffix=CacheSuffix.LAST_UPDATED.value)
        return self.timestamp_handler.read_timestamp(path)

    # ---------- PARQUET CACHE ----------
    def save_enriched_dataframe(self, df: pd.DataFrame, service: str, instance: str, content_type: str = "series"):
        suffix = self.SUFFIX_MAP.get(content_type, EnrichedSuffix.SERIES).value
        key = CacheKeyTemplate.SERIES_LIBRARY.format(service=service, instance=instance) + suffix
        path = self.key_builder.build_parquet_path(*key.split("/"))
        self.logger.log_debug(f"💾 Saving enriched DataFrame to {path}")
        return self.parquet_handler.save_dataframe(path, df)

    def load_enriched_dataframe(self, service: str, instance: str, content_type: str = "series") -> pd.DataFrame:
        suffix = self.SUFFIX_MAP.get(content_type, EnrichedSuffix.SERIES).value
        key = CacheKeyTemplate.SERIES_LIBRARY.format(service=service, instance=instance) + suffix
        path = self.key_builder.build_parquet_path(*key.split("/"))
        return self.parquet_handler.load_dataframe(path)

    # ---------- DELTA DIFF ----------
    def get_delta_diff(
        self,
        new_df: pd.DataFrame,
        service: str,
        instance: str,
        content_type: str = "series",
        primary_key: str = "series_id",
        comparison_fields: Optional[list] = None
    ) -> dict:
        suffix = self.SUFFIX_MAP.get(content_type, EnrichedSuffix.SERIES)
        key = CacheKeyTemplate.SERIES_LIBRARY.format(service=service, instance=instance) + suffix.value
        cached_df = self.parquet_handler.load_parquet(key)

        if cached_df is None or cached_df.empty:
            self.logger.log_warning("⚠️ No prior enriched DataFrame found — treating all rows as new.")
            return {"added": new_df, "removed": pd.DataFrame(), "changed": pd.DataFrame()}

        if primary_key not in new_df.columns or primary_key not in cached_df.columns:
            self.logger.log_error(f"❌ Primary key '{primary_key}' missing in dataframes.")
            return {"added": new_df, "removed": pd.DataFrame(), "changed": pd.DataFrame()}

        new_df = new_df.set_index(primary_key)
        cached_df = cached_df.set_index(primary_key)

        added_df = new_df.loc[new_df.index.difference(cached_df.index)].reset_index()
        removed_df = cached_df.loc[cached_df.index.difference(new_df.index)].reset_index()

        changed_df = pd.DataFrame()
        if comparison_fields:
            diffs = (new_df.loc[new_df.index.intersection(cached_df.index), comparison_fields]
                     != cached_df.loc[new_df.index.intersection(cached_df.index), comparison_fields])
            changed_df = new_df.loc[diffs.any(axis=1)].reset_index()

        return {
            "added": added_df,
            "removed": removed_df,
            "changed": changed_df
        }

    # ---------- HELPERS ----------
    def format_cache_key(self, service: str, instance: str, resource: str = "") -> str:
        parts = [service, instance]
        if resource:
            parts.append(resource)
        return self.key_builder.format_cache_key(*parts)

    def build_cache_path(self, *parts: str, suffix: str = ".json") -> Path:
        return self.key_builder.build_cache_path(*parts, suffix=suffix)

    def exists(self, key: str) -> bool:
        """Compat shim for older callers."""
        return self.json_exists(key)

    def set(self, key: str, data: dict, pretty: bool = True) -> bool:
        """Compat shim for older callers."""
        if pretty:
            return self.set_with_pretty_output(key, data, compressed=False)
        return self.set_json(key, data, compressed=False, pretty=False)

    def get(self, key: str, default=None):
        """Compat: allow default= like old CacheManager."""
        data = self.json_handler.get(key)
        if (data is None or data == {}) and default is not None:
            return default
        return data

    def delete(self, key: str) -> bool:
        """Expose delete for callers that clean up keys."""
        return self.json_handler.delete(key)

    def invalidate_cache_key(self, key: str) -> bool:
        """Alias for delete — invalidates a cached key by removing its file."""
        return self.delete(key)

    def deduplicate_entries(self, existing, new_items, id_field="id", instance: str | None = None):
        """
        Simple merge by id_field, preferring latest entry when ids collide.
        Returns (merged_list, stats)
        """
        existing = existing or []
        new_items = new_items or []
        by_id = {item.get(id_field): item for item in existing if item.get(id_field) is not None}

        new, updated, skipped = 0, 0, 0
        for item in new_items:
            _id = item.get(id_field)
            if _id is None:
                skipped += 1
                continue
            if _id in by_id:
                by_id[_id] = item
                updated += 1
            else:
                by_id[_id] = item
                new += 1

        merged = list(by_id.values())
        stats = {"total": len(merged), "new": new, "updated": updated, "skipped": skipped}
        return merged, stats
