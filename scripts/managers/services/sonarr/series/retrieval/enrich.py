from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSeriesRetrievalEnrichManager(BaseManager, ComponentManagerMixin):
    """
    Enriches Sonarr series with TVDB metadata and saves to dual cache system.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrSeries"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.manager = manager
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)
        self.tvdb = kwargs.get("tvdb") or getattr(manager, "tvdb", None)
        self.series_cache = kwargs.get("series_cache") or getattr(manager, "series_cache", None)

        self.dry_run = getattr(manager, "dry_run", False)

        self.register()
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("build_enriched_series_dataframe")
    def build_enriched_series_dataframe(self, instance: str, save: bool = True, save_unenriched: bool = True) -> pd.DataFrame:
        arrapi = self.sonarr_api.get_all_sonarr_apis().get(instance)
        if not arrapi:
            self.logger.log_error(f"❌ No arrapi found for instance '{instance}'")
            return pd.DataFrame()

        tag_data = self.sonarr_cache.get(f"sonarr/{instance}/tags.json") if self.sonarr_cache else []
        tag_map = {tag["id"]: tag["label"] for tag in tag_data if isinstance(tag, dict)}

        series_list = arrapi.all_series()
        if not series_list:
            self.logger.log_error(f"❌ No series found in Sonarr for instance '{instance}'")
            return pd.DataFrame()

        tvdb_queue, unenriched, enriched = [], [], []
        ctx = get_context("spawn")
        lock = ctx.Lock()

        for s in series_list:
            row = self._extract_series_base_row(s, instance, tag_map)
            if row.get("tvdb_id"):
                tvdb_queue.append((row, row["tvdb_id"]))
            else:
                enriched.append(row)

        def enrich(entry):
            base_row, tvdb_id = entry
            tvdb_data = self.tvdb.fetch_tvdb_data(tvdb_id=tvdb_id, fallback_title=base_row["title"])
            if tvdb_data:
                base_row.update(tvdb_data)
                with lock:
                    enriched.append(base_row)
            else:
                with lock:
                    unenriched.append(base_row)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(tqdm(executor.map(enrich, tvdb_queue), total=len(tvdb_queue), desc=f"📡 Enriching [{instance}]"))

        df = pd.DataFrame(enriched)

        if save:
            output_path = self._get_output_path(instance)
            df.to_parquet(output_path, index=False)
            self.logger.log_info(f"💾 Enriched dataframe saved to {output_path}")

        if save_unenriched and unenriched:
            self.logger.log_unenriched_series(instance, unenriched)

        self.logger.log_enrichment_summary(df, unenriched)
        return df

    def _extract_series_base_row(self, series, instance, tag_map) -> dict:
        return {
            "instance": instance,
            "series_id": series.id,
            "title": series.title,
            "slug": getattr(series, "titleSlug", "N/A"),
            "path": series.path,
            "status": series.status,
            "language": getattr(series, "language", {}).get("name"),
            "is_monitored": series.monitored,
            "year": series.year,
            "tvdb_id": getattr(series, "tvdbId", None),
            "tags": [self._resolve_tag_name(getattr(t, "id", t), tag_map) for t in (series.tags or [])],
            "season_folder": series.seasonFolder,
            "runtime": series.runtime,
            "genres": series.genres,
            "added": series.added,
            "last_info_sync": getattr(series, "lastInfoSync", None),
            "quality_profile_id": series.qualityProfileId,
            "season_count": len(series.seasons),
        }

    def _resolve_tag_name(self, tag_id, tag_map):
        return tag_map.get(tag_id, f"UnknownTag-{tag_id}")

    def _get_output_path(self, instance: str) -> Path:
        cache_root = self.sonarr_cache.cache_root if self.sonarr_cache else self.global_cache.cache_root
        return Path(cache_root) / "sonarr" / instance / "library.enriched.parquet"
