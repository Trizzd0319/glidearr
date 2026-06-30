import gzip
import json

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache import make_json_safe
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrCacheSeriesManager(BaseManager, ComponentManagerMixin):
    """
    Manages letter-bucketed gzip series cache files for Sonarr.

    Accessible as ``sonarr_cache.series`` from any component, mirroring the
    pattern used by ``sonarr_cache.episodes``, ``sonarr_cache.tags``, etc.

    All methods accept a pre-resolved instance name string — callers are
    responsible for resolving ``None`` → default instance before calling here.
    """

    _ALL_LETTERS = "abcdefghijklmnopqrstuvwxyz0123456789_"

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None,
                 sonarr_cache=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        manager = kwargs.get("manager") or {}
        self.sonarr_cache = sonarr_cache or getattr(manager, "sonarr_cache", None) or self
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        self.manager = manager

        self.register()

        if not self.logger:
            raise ValueError(f"❌ {self.parent_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    # ─────────────────────────────────────────────
    # 📂 Path Helpers (bypass build_cache_path)
    # ─────────────────────────────────────────────
    # CacheKeyBuilder.build_cache_path appends the requested suffix to the key,
    # which mangles compound extensions: a "d.json.gz" key + ".json" suffix →
    # "d.json.gz.json".  For letter bucket files we therefore construct paths
    # directly from cache_root so that the on-disk extension is exactly ".json.gz".

    def _library_dir(self, instance: str):
        """Return the Path to the library directory, creating it if needed.

        Uses ``key_builder.base_dir`` (absolute, derived from source file location)
        so that letter-bucket files always land in the same directory as every
        other cache file produced by ``build_cache_path``, regardless of the
        working directory from which the script is invoked.
        """
        d = self.global_cache.key_builder.base_dir / "sonarr" / instance / "library"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _letter_file(self, instance: str, letter: str):
        """Return the exact Path for a letter bucket .json.gz file."""
        return self._library_dir(instance) / f"{letter}.json.gz"

    # ─────────────────────────────────────────────
    # 🔡 Letter Bucket Utilities
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_series_bucket_letter")
    def get_series_bucket_letter(self, title: str) -> str:
        c = (title or "").strip().lower()[:1]
        return c if c.isalnum() else "_"

    @LoggerManager().log_function_entry
    @timeit("list_cached_letters")
    def list_cached_letters(self, instance: str) -> list[str]:
        folder = self._library_dir(instance)
        # Use p.name[:-len(".json.gz")] rather than p.stem; Path.stem only strips
        # the last extension (.gz), leaving "a.json" instead of "a".
        return sorted(
            p.name[:-len(".json.gz")]
            for p in folder.glob("*.json.gz")
            if p.is_file()
        )

    @LoggerManager().log_function_entry
    @timeit("clear_letter_cache")
    def clear_letter_cache(self, instance: str, letter: str):
        self._bucket_memo_invalidate(instance, letter)
        path = self._letter_file(instance, letter)
        if path.exists():
            path.unlink()
            self.logger.log_info(f"🧹 Cleared letter cache '{letter}.json.gz' for instance '{instance}'")

    # ─────────────────────────────────────────────
    # 📦 Load & Save Cache Files
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("load_letter_cache")
    # ── In-memory bucket memo ─────────────────────────────────────────────────
    # load_letter_cache gunzips + JSON-parses a bucket file on every call, and the
    # series lookups (by id/title/tvdb, iter_all_series, get_all_series_ids) each
    # rescan all ~40 buckets — so the same files were re-loaded thousands of times
    # per run (profiler run-014: load_letter_cache ≈ 59s self over 6469 calls).
    # Bucket contents don't change mid-run except where WE write, so we read
    # through an in-memory memo and update/invalidate it on every write below.
    # NOTE: callers must treat the returned list as read-only (shared reference).
    def _bucket_memo(self) -> dict:
        return self.__dict__.setdefault("_bucket_memo_store", {})

    def _bucket_memo_set(self, instance: str, letter: str, data: list) -> None:
        self._bucket_memo()[(instance, letter)] = data

    def _bucket_memo_invalidate(self, instance: str, letter: str = None) -> None:
        memo = self.__dict__.get("_bucket_memo_store")
        if not memo:
            return
        if letter is None:
            for k in [k for k in memo if k[0] == instance]:
                memo.pop(k, None)
        else:
            memo.pop((instance, letter), None)

    def load_letter_cache(self, instance: str, letter: str) -> list:
        memo = self._bucket_memo()
        key  = (instance, letter)
        cached = memo.get(key)
        if cached is not None:
            return cached
        path = self._letter_file(instance, letter)
        if path.exists():
            with gzip.open(path, "rt", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = []
        memo[key] = data
        return data

    @LoggerManager().log_function_entry
    @timeit("save_series_to_letter_file")
    def save_series_to_letter_file(self, instance: str, series: dict):
        letter = self.get_series_bucket_letter(series.get("title", ""))
        path = self._letter_file(instance, letter)

        existing = []
        if path.exists():
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                pass

        sid = str(series.get("id"))
        existing = [s for s in existing if str(s.get("id")) != sid]
        existing.append(series)

        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(make_json_safe(existing), f, indent=2)
        self._bucket_memo_set(instance, letter, existing)  # keep memo in sync

        self.logger.log_info(f"💾 Series {sid} saved to letter bucket '{letter}.json.gz'")

    @LoggerManager().log_function_entry
    @timeit("rebuild_bucketed_series_cache")
    def rebuild_bucketed_series_cache(self, instance: str, all_series: list):
        self._bucket_memo_invalidate(instance)  # full rebuild — drop stale memo
        buckets: dict[str, list] = {}
        for s in all_series:
            letter = self.get_series_bucket_letter(s.get("title", ""))
            buckets.setdefault(letter, []).append(s)

        total_series = len(all_series)
        total_buckets = len(buckets)
        written_series = 0

        self.logger.log_info(
            f"📂 Writing {total_series} series across {total_buckets} letter bucket(s) for '{instance}'…"
        )

        # One tqdm bar (stderr) instead of a per-bucket progress line; errors still log.
        from scripts.support.utilities.progress.tqdm_wrapper import tqdm
        for letter, series_list in tqdm(buckets.items(), total=total_buckets,
                                        desc=f"📂 Buckets [{instance}]", unit="bucket"):
            try:
                path = self._letter_file(instance, letter)
                with gzip.open(path, "wt", encoding="utf-8") as f:
                    json.dump(make_json_safe(series_list), f, indent=2)
                self._bucket_memo_set(instance, letter, series_list)
                written_series += len(series_list)
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ bucket '{letter}' write failed: {e}")
        self.logger.log_info(
            f"📦 Wrote {written_series}/{total_series} series across "
            f"{total_buckets} bucket(s) for '{instance}'.")

    # ─────────────────────────────────────────────
    # 🔍 Retrieval & Deduplication
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_all_series_ids")
    def get_all_series_ids(self, instance: str) -> set[str]:
        ids: set[str] = set()
        for letter in "abcdefghijklmnopqrstuvwxyz0123456789_":
            for s in self.load_letter_cache(instance, letter):
                sid = s.get("id")
                if sid:
                    ids.add(str(sid))
        return ids

    @LoggerManager().log_function_entry
    @timeit("get_cached_series_by_id")
    def get_cached_series_by_id(self, instance: str, series_id: str) -> dict | None:
        for letter in "abcdefghijklmnopqrstuvwxyz0123456789_":
            for s in self.load_letter_cache(instance, letter):
                if str(s.get("id")) == str(series_id):
                    return s
        return None

    @LoggerManager().log_function_entry
    @timeit("deduplicate_series_data")
    def deduplicate_series_data(self, existing: list, new_data: list) -> tuple[dict, dict]:
        merged = {str(s.get("id")): s for s in existing if isinstance(s, dict)}
        stats = {"new": 0, "updated": 0, "skipped": 0}

        for series in new_data:
            sid = str(series.get("id"))
            if sid not in merged:
                stats["new"] += 1
            elif merged[sid] != series:
                stats["updated"] += 1
            else:
                stats["skipped"] += 1
                continue
            merged[sid] = series

        return merged, stats

    # ─────────────────────────────────────────────
    # 💾 Persistence Verification
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("persist_letter_cache")
    def persist_letter_cache(self, instance: str):
        """
        Verifies the letter-bucketed cache is fully on disk for the given instance.
        All writes happen eagerly during rebuild/save; this is a confirmation step.
        """
        try:
            folder = self._library_dir(instance)
            letter_files = list(folder.glob("*.json.gz")) if folder.exists() else []
            total_entries = 0
            for f in letter_files:
                # Strip the full ".json.gz" compound extension so load_letter_cache
                # receives "a" not "a.json".
                letter = f.name[:-len(".json.gz")]
                entries = self.load_letter_cache(instance, letter)
                total_entries += len(entries)
            self.logger.log_info(
                f"💾 Cache verified for '{instance}': {len(letter_files)} bucket(s), {total_entries} series total."
            )
        except Exception as e:
            self.logger.log_warning(f"⚠️ Could not verify persisted cache for '{instance}': {e}")

    # ─────────────────────────────────────────────
    # 📊 Debug + Reporting
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("summarize_cache_statistics")
    def summarize_cache_statistics(self, instance: str):
        total, missing_ids, corrupt = 0, 0, 0
        for letter in self.list_cached_letters(instance):
            try:
                series_list = self.load_letter_cache(instance, letter)
                count = len(series_list)
                missing = sum("id" not in s for s in series_list)
                self.logger.log_info(f"🔎 {letter}.json.gz → {count} entries ({missing} missing IDs)")
                total += count
                missing_ids += missing
            except Exception as e:
                self.logger.log_warning(f"⚠️ Failed to read {letter}.json.gz: {e}")
                corrupt += 1

        self.logger.log_info(
            f"📊 Cache Summary — Total Series: {total}, Missing IDs: {missing_ids}, Corrupt Files: {corrupt}"
        )

    # ─────────────────────────────────────────────
    # 🔁 Full-Library Reads
    # ─────────────────────────────────────────────

    def iter_all_series(self, instance: str):
        """Generator — yields every cached series one at a time without loading
        the full library into memory. Prefer this inside other methods that
        scan the whole cache to avoid duplicating the loop logic."""
        for letter in self._ALL_LETTERS:
            yield from self.load_letter_cache(instance, letter)

    @LoggerManager().log_function_entry
    @timeit("get_all_series")
    def get_all_series(self, instance: str) -> list:
        """Return all cached series as a flat list."""
        return list(self.iter_all_series(instance))

    @LoggerManager().log_function_entry
    @timeit("get_series_count")
    def get_series_count(self, instance: str) -> int:
        """Return the total number of cached series across all letter buckets."""
        return sum(len(self.load_letter_cache(instance, letter)) for letter in self._ALL_LETTERS)

    @LoggerManager().log_function_entry
    @timeit("get_all_titles")
    def get_all_titles(self, instance: str) -> set[str]:
        """Return the set of all cached series titles (used e.g. by Tautulli sync)."""
        return {s["title"] for s in self.iter_all_series(instance) if s.get("title")}

    # ─────────────────────────────────────────────
    # 🔍 Single-Series Lookups
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_series_by_title")
    def get_series_by_title(self, instance: str, title: str) -> dict | None:
        """Case-insensitive title lookup across all letter buckets."""
        title_lower = title.lower()
        for s in self.iter_all_series(instance):
            if s.get("title", "").lower() == title_lower:
                return s
        self.logger.log_debug(f"❌ Series with title '{title}' not found in '{instance}'")
        return None

    @LoggerManager().log_function_entry
    @timeit("get_series_by_tvdb_id")
    def get_series_by_tvdb_id(self, instance: str, tvdb_id: int) -> dict | None:
        """Find a cached series by its TVDB ID."""
        for s in self.iter_all_series(instance):
            if str(s.get("tvdbId")) == str(tvdb_id):
                return s
        self.logger.log_debug(f"❌ Series with TVDB ID {tvdb_id} not found in '{instance}'")
        return None

    @LoggerManager().log_function_entry
    @timeit("get_title_by_series_id")
    def get_title_by_series_id(self, instance: str, series_id: int) -> str | None:
        """Return just the title string for the given series ID."""
        s = self.get_cached_series_by_id(instance, str(series_id))
        return s.get("title") if s else None

    @LoggerManager().log_function_entry
    @timeit("is_series_in_library")
    def is_series_in_library(self, instance: str, tvdb_id: int) -> bool:
        """Return True if a series with the given TVDB ID is present in cache."""
        return self.get_series_by_tvdb_id(instance, tvdb_id) is not None

    # ─────────────────────────────────────────────
    # 🔽 Filtered Reads
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_monitored_series")
    def get_monitored_series(self, instance: str) -> list:
        """Return all series where ``monitored=True``."""
        return [s for s in self.iter_all_series(instance) if s.get("monitored")]

    @LoggerManager().log_function_entry
    @timeit("get_unmonitored_series")
    def get_unmonitored_series(self, instance: str) -> list:
        """Return all series where ``monitored=False``."""
        return [s for s in self.iter_all_series(instance) if not s.get("monitored")]

    @LoggerManager().log_function_entry
    @timeit("get_series_by_status")
    def get_series_by_status(self, instance: str, status: str) -> list:
        """Return all series matching ``status`` ('continuing', 'ended', 'upcoming'...)."""
        status_lower = status.lower()
        return [s for s in self.iter_all_series(instance) if s.get("status", "").lower() == status_lower]

    @LoggerManager().log_function_entry
    @timeit("list_series_by_tag_id")
    def list_series_by_tag_id(self, instance: str, tag_id: int) -> list:
        """Return all series that carry the given integer Sonarr tag ID."""
        return [s for s in self.iter_all_series(instance) if tag_id in s.get("tags", [])]

    # ─────────────────────────────────────────────
    # 🗺️ Map Generation
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_series_tags_map")
    def get_series_tags_map(self, instance: str) -> dict[int, list]:
        """Return ``{series_id: [tag_ids]}`` for all cached series."""
        return {s["id"]: s.get("tags", []) for s in self.iter_all_series(instance) if "id" in s}

    @LoggerManager().log_function_entry
    @timeit("get_series_quality_map")
    def get_series_quality_map(self, instance: str) -> dict[int, int]:
        """Return ``{series_id: qualityProfileId}`` for all cached series."""
        return {
            s["id"]: s["qualityProfileId"]
            for s in self.iter_all_series(instance)
            if "id" in s and "qualityProfileId" in s
        }

    @LoggerManager().log_function_entry
    @timeit("get_series_path_map")
    def get_series_path_map(self, instance: str) -> dict[int, str]:
        """Return ``{series_id: path}`` for all cached series that have a path."""
        return {s["id"]: s["path"] for s in self.iter_all_series(instance) if "id" in s and "path" in s}

    @LoggerManager().log_function_entry
    @timeit("get_series_root_folder_map")
    def get_series_root_folder_map(self, instance: str) -> dict[int, str]:
        """Return ``{series_id: rootFolderPath}`` for all cached series."""
        return {
            s["id"]: s["rootFolderPath"]
            for s in self.iter_all_series(instance)
            if "id" in s and "rootFolderPath" in s
        }

    # ─────────────────────────────────────────────
    # 🔀 Delta Rebuild
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("delta_rebuild_series_cache")
    def delta_rebuild_series_cache(self, instance: str, live_series: list) -> dict:
        """
        Smart delta sync: only rewrite letter buckets whose content has changed.

        Compares the incoming ``live_series`` list (full live API response) against
        the on-disk letter-bucket cache **per bucket**.  Buckets where nothing
        changed are left completely untouched, keeping disk I/O minimal for large
        libraries.

        Parameters
        ----------
        instance:
            Resolved Sonarr instance name.
        live_series:
            Complete list of series dicts returned by the Sonarr ``/series``
            endpoint (full library — Sonarr v3 has no delta endpoint).

        Returns
        -------
        dict
            ``{"rewritten": N, "skipped": N, "added": N, "removed": N}``
        """
        stats = {"rewritten": 0, "skipped": 0, "added": 0, "removed": 0}

        # Group the live series by letter bucket
        live_by_letter: dict[str, dict[str, dict]] = {}  # letter → {id_str: series}
        for s in live_series:
            letter = self.get_series_bucket_letter(s.get("title", ""))
            live_by_letter.setdefault(letter, {})[str(s.get("id"))] = s

        # Determine all letters to consider (union of live + on-disk)
        disk_letters = set(self.list_cached_letters(instance))
        all_letters  = set(live_by_letter.keys()) | disk_letters

        for letter in sorted(all_letters):
            live_map: dict[str, dict] = live_by_letter.get(letter, {})

            # Load existing on-disk data for this bucket
            if letter in disk_letters:
                try:
                    disk_data = self.load_letter_cache(instance, letter)
                except Exception as e:
                    self.logger.log_warning(
                        f"⚠️ Could not load bucket '{letter}' for delta compare: {e}"
                    )
                    disk_data = []
            else:
                disk_data = []

            disk_map: dict[str, dict] = {str(s.get("id")): s for s in disk_data}

            # Compute changes
            added_ids   = set(live_map) - set(disk_map)
            removed_ids = set(disk_map) - set(live_map)
            changed_ids = {
                sid for sid in set(live_map) & set(disk_map)
                if live_map[sid] != disk_map[sid]
            }

            if not added_ids and not removed_ids and not changed_ids:
                stats["skipped"] += 1
                continue

            # Rewrite the bucket with the live data
            new_entries = list(live_map.values())
            path = self._letter_file(instance, letter)
            try:
                with gzip.open(path, "wt", encoding="utf-8") as f:
                    json.dump(make_json_safe(new_entries), f, indent=2)
            except Exception as e:
                self.logger.log_warning(
                    f"⚠️ Failed to rewrite bucket '{letter}' during delta sync: {e}"
                )
                continue

            self._bucket_memo_set(instance, letter, new_entries)
            stats["rewritten"] += 1
            stats["added"]     += len(added_ids)
            stats["removed"]   += len(removed_ids)

            self.logger.log_debug(
                f"  🔀 '{letter}.json.gz' rewritten — "
                f"+{len(added_ids)} added, -{len(removed_ids)} removed, "
                f"{len(changed_ids)} changed"
            )

        # Remove on-disk buckets that no longer have any live series
        for letter in disk_letters - set(live_by_letter.keys()):
            path = self._letter_file(instance, letter)
            try:
                self._bucket_memo_invalidate(instance, letter)
                path.unlink()
                self.logger.log_info(
                    f"🗑️ Removed empty bucket '{letter}.json.gz' for '{instance}'"
                )
            except Exception as e:
                self.logger.log_warning(
                    f"⚠️ Could not remove empty bucket '{letter}': {e}"
                )

        return stats

    # ─────────────────────────────────────────────
    # ✏️ Mutations
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("remove_series")
    def remove_series(self, instance: str, series_id: int) -> bool:
        """Remove a series from its letter bucket by ID.

        Returns True if the series was found and removed, False if not found.
        Does not touch buckets that don't contain the series.
        """
        sid = str(series_id)
        for letter in self._ALL_LETTERS:
            path = self._letter_file(instance, letter)
            if not path.exists():
                continue
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    entries = json.load(f)
            except Exception:
                continue
            before = len(entries)
            entries = [s for s in entries if str(s.get("id")) != sid]
            if len(entries) < before:
                with gzip.open(path, "wt", encoding="utf-8") as f:
                    json.dump(make_json_safe(entries), f, indent=2)
                self._bucket_memo_set(instance, letter, entries)
                self.logger.log_info(f"🗑️ Removed series {series_id} from bucket '{letter}.json.gz' in '{instance}'")
                return True
        self.logger.log_warning(f"⚠️ Series {series_id} not found in any letter bucket for '{instance}'")
        return False
