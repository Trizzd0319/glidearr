"""
sonarr/cache/owned_episodes.py — the FULL owned-episode inventory (playlists feed).
================================================================================
A SEPARATE, UNPRUNED artifact at ``sonarr/<instance>/owned_episodes.parquet`` — one
row per owned (hasFile) episode. It exists because the JIT/space crown-jewel
``episode_files.parquet`` is deliberately PRUNED to pilots + watched + next-unwatched
(``episode_files._do_cleanup_non_essential``); per-user playlists need the *whole*
library. This manager NEVER touches that parquet and never prunes — it only reads
Sonarr (via the already-warm sibling caches) and writes its own file.

Each row also carries the **Plex join key** ``{series_tvdb}:{season}:{episode}`` so
the Plex side (a later PR) can resolve each owned episode to a ratingKey. The key is
NULL-safe: any missing component → ``None`` (counted, never guessed) so a series with
no tvdbId can't error-stop the build.

Service-layer only (no brain logic, no Plex, no scoring). Read-only vs Sonarr.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager

# Inventory schema — a SUBSET of episode_files SCHEMA_COLUMNS: no lifecycle/decision/
# grace/is_pilot/is_watched fields (those belong to the pruned JIT parquet).
COLUMNS = [
    "episode_file_id", "series_id", "series_title", "series_tvdb_id",
    "season_number", "episode_number", "has_file", "monitored", "title",
    "air_date_utc", "is_special", "tvdb_join_key",
]


class SonarrCacheOwnedEpisodesManager(BaseManager):
    parent_name = "SonarrCacheOwnedEpisodes"

    def __init__(self, logger=None, config=None, global_cache=None, validator=None,
                 registry=None, sonarr_cache=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = sonarr_cache or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        self.instance_manager = (kwargs.get("instance_manager")
                                 or getattr(manager, "instance_manager", None))
        # Read-only vs Sonarr (only reads + a local parquet write) → dry_run is
        # informational; never gates anything here, so a safe default is fine.
        self.dry_run = bool(kwargs.get("dry_run", getattr(manager, "dry_run", True)))
        # NOTE: BaseManager.__init__ already registered this manager. We do NOT call
        # self.register() — that lives on ComponentManagerMixin, which this on-demand
        # builder (constructed fresh by the playlist builder, never looked up by name)
        # intentionally doesn't inherit.

    # ── helpers ───────────────────────────────────────────────────────────────
    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        return instance or "default"

    def _parquet_path(self, instance: str):
        """Absolute path to the OWNED-episodes parquet — a DIFFERENT file from the
        pruned ``episode_files.parquet`` (the crown-jewel invariant)."""
        p = self.global_cache.key_builder.base_dir / "sonarr" / instance / "owned_episodes.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def _join_key(tvdb, season, episode) -> str | None:
        """``{series_tvdb}:{season}:{episode}`` — NULL-safe (any None → None)."""
        if tvdb is None or season is None or episode is None:
            return None
        return f"{tvdb}:{season}:{episode}"

    @classmethod
    def _build_owned_rows(cls, series_meta: dict, episodes_by_series: dict) -> list[dict]:
        """Pure: turn {series_id: {tvdb,title}} + {series_id: [episode objs]} into the
        owned-episode rows (hasFile only). Static so it is unit-testable without I/O."""
        rows: list[dict] = []
        for series_id, eps in episodes_by_series.items():
            meta = series_meta.get(series_id, {})
            tvdb = meta.get("tvdb")
            stitle = meta.get("title", "")
            for ep in eps or []:
                if not ep.get("hasFile"):
                    continue                      # owned = has a file on disk
                season = ep.get("seasonNumber")
                episode = ep.get("episodeNumber")
                rows.append({
                    "episode_file_id": ep.get("episodeFileId"),
                    "series_id": series_id,
                    "series_title": stitle,
                    "series_tvdb_id": tvdb,
                    "season_number": season,
                    "episode_number": episode,
                    "has_file": True,
                    "monitored": bool(ep.get("monitored")),
                    "title": ep.get("title") or "",
                    "air_date_utc": ep.get("airDateUtc"),
                    "is_special": season == 0,
                    "tvdb_join_key": cls._join_key(tvdb, season, episode),
                })
        return rows

    # ── build ─────────────────────────────────────────────────────────────────
    def build_or_refresh(self, instance: str | None = None) -> pd.DataFrame:
        """Fetch the full owned-episode inventory and persist it. Reuses the sibling
        ``episode_files`` warm 24h episode cache (free hits) and the ``series`` cache
        for tvdbId — no new API surface. Always persists (read-only vs Sonarr)."""
        instance = self._resolve_instance(instance)
        series_cache = getattr(self.sonarr_cache, "series", None)
        ep_mgr = getattr(self.sonarr_cache, "episode_files", None)
        if series_cache is None or ep_mgr is None:
            self.logger.log_warning(
                "[OwnedEpisodes] series/episode_files cache unavailable — skipping.")
            return pd.DataFrame(columns=COLUMNS)

        series_meta = {
            s["id"]: {"tvdb": s.get("tvdbId"), "title": s.get("title", "")}
            for s in series_cache.iter_all_series(instance) if isinstance(s, dict) and "id" in s
        }
        # tqdm bar (stderr) over all series — a cold cache otherwise emits a wall of
        # per-series fetch lines. Errors are logged but never abort the whole build.
        from scripts.support.utilities.progress.tqdm_wrapper import tqdm
        episodes_by_series: dict = {}
        for sid in tqdm(series_meta, total=len(series_meta),
                        desc=f"📺 Owned episodes [{instance}]", unit="series"):
            try:
                bucketed = ep_mgr._get_all_episodes(instance, sid) or {}   # {season: [eps]}
                episodes_by_series[sid] = [e for eps in bucketed.values() for e in eps]
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ owned-episode fetch failed for series {sid}: {e}")

        rows = self._build_owned_rows(series_meta, episodes_by_series)
        df = pd.DataFrame(rows, columns=COLUMNS)
        if not df.empty:
            df = df.sort_values(["series_id", "season_number", "episode_number"],
                                kind="stable").reset_index(drop=True)
        path = self._parquet_path(instance)
        df.to_parquet(path, index=False)
        unresolved = int(df["tvdb_join_key"].isna().sum()) if not df.empty else 0
        self.logger.log_info(
            f"[OwnedEpisodes] {len(df)} owned episode(s) across {len(series_meta)} series "
            f"→ {path.name} ({unresolved} without a tvdb join key).")
        return df
