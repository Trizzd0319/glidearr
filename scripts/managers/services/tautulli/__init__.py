import re
from typing import Optional

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.factories.mixins.ordered_components import topo_order
from scripts.managers.services.tautulli.api import TautulliAPI
from scripts.managers.services.tautulli.devices import TautulliDevicesManager
from scripts.managers.services.tautulli.episodes import TautulliEpisodesManager
from scripts.managers.services.tautulli.instances import TautulliInstanceManager
from scripts.managers.services.tautulli.metadata import TautulliMetadataManager
from scripts.managers.services.tautulli.series import TautulliSeriesManager
from scripts.managers.services.tautulli.transcode import TautulliTranscodeManager
from scripts.managers.services.tautulli.users import TautulliUsersManager
from scripts.managers.services.tautulli.validator import TautulliValidatorManager
from scripts.managers.services.tautulli.watch_history import TautulliWatchHistoryManager

from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class TautulliManager(BaseManager, ComponentManagerMixin):
    parent_name = "TautulliManager"

    tautulli_api:     Optional[TautulliAPI] = None
    devices:          Optional[TautulliDevicesManager] = None
    episodes:         Optional[TautulliEpisodesManager] = None
    instance:         Optional[TautulliInstanceManager] = None
    metadata:         Optional[TautulliMetadataManager] = None
    series:           Optional[TautulliSeriesManager] = None
    transcode:        Optional[TautulliTranscodeManager] = None
    users:            Optional[TautulliUsersManager] = None
    validator_manager: Optional[TautulliValidatorManager] = None
    watch_history:    Optional[TautulliWatchHistoryManager] = None

    @LoggerManager().log_function_entry
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        # Register self under the parent-link (matches Sonarr/Radarr/Trakt/MAL). The
        # base __init__ already registers by name; this adds the parent_name link.
        self.register()

        # dry_run must be captured explicitly — BaseManager does NOT, so without
        # this every submanager built from init_args would silently default to
        # False (the documented propagation footgun). Tautulli is read-only today,
        # but this keeps it consistent with Sonarr/Radarr/Trakt/MAL.
        self.dry_run = kwargs.get("dry_run", False)

        tautulli_cfg = self.config.get("tautulli", {}) if self.config else {}
        # Multi-instance format: {"default": {"url": ..., "api": ...}}
        # Flat format:           {"url": ..., "api": ...}
        if tautulli_cfg and not all(isinstance(v, str) for v in tautulli_cfg.values()):
            tautulli_cfg = tautulli_cfg.get("default", next(iter(tautulli_cfg.values()), {}))
        # Service-specific API handle (``tautulli_api``, never a generic ``api``) to
        # match the sonarr_api / radarr_api / trakt_api naming convention.
        self.tautulli_api = TautulliAPI(logger=self.logger, instance_config=tautulli_cfg)

        self.init_args = {
            "logger":       self.logger,
            "config":       self.config,
            "global_cache": self.global_cache,
            "registry":     self.registry,
            "validator":    self.validator,
            "tautulli_api": self.tautulli_api,
            "parent_name":  self.parent_name,
            "dry_run":      self.dry_run,
        }

        self.component_dependencies = {
            "devices":          [],
            "episodes":         [],
            "instance":         [],
            "metadata":         [],
            "series":           [],
            "transcode":        [],
            "users":            [],
            "watch_history":    [],
            "validator_manager": [],
        }

        self.all_component_classes = {
            "devices":          TautulliDevicesManager,
            "episodes":         TautulliEpisodesManager,
            "instance":         TautulliInstanceManager,
            "metadata":         TautulliMetadataManager,
            "series":           TautulliSeriesManager,
            "transcode":        TautulliTranscodeManager,
            "users":            TautulliUsersManager,
            "watch_history":    TautulliWatchHistoryManager,
            "validator_manager": TautulliValidatorManager,
        }

        self.critical_keys = {
            "watch_history", "users", "metadata",
            "episodes", "series", "transcode", "devices",
        }

        self.critical_components, self.noncritical_components = split_components(
            all_components=self.all_component_classes,
            critical_keys=self.critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=self.init_args,
        )

        self.load_summary = {}
        self.logger.log_debug(f"[{self.__class__.__name__}] initialized")

    def _load_component(self, name: str, auto_load_deps: bool = True, log_dependencies: bool = True):
        if hasattr(self, name) and getattr(self, name) is not None:
            return getattr(self, name)
        existing = self.registry.get("manager", name)
        if existing:
            setattr(self, name, existing)
            return existing
        component_class = self.critical_components.get(name) or self.noncritical_components.get(name)
        if not component_class:
            self.load_summary[name] = "❌ unknown"
            return None
        for dep in self.component_dependencies.get(name, []):
            if not getattr(self, dep, None) and auto_load_deps:
                self._load_component(dep)
        try:
            instance = self._singleton(name, component_class, **self.init_args)
            setattr(self, name, instance)
            self.load_summary[name] = "✅"
            return instance
        except Exception as e:
            self.load_summary[name] = "❌"
            self.logger.log_error(f"[{self.__class__.__name__}] ❌ {name}: {e}")
            return None

    @LoggerManager().log_function_entry
    @timeit("prepare")
    def prepare(self):
        cls = self.__class__.__name__
        # Iterate in dependency order (matches Sonarr/Radarr via topo_order). The
        # components declare no inter-dependencies today, so this equals insertion
        # order — it just keeps the load order honouring any deps added here later.
        order = topo_order(self.component_dependencies)
        # Load every declared component (matches Sonarr/Radarr). Previously this
        # only loaded critical_keys, leaving instance/validator_manager unloaded.
        for name in order:
            if getattr(self, name, None) is None:
                self._load_component(name)
            elif not str(self.load_summary.get(name, "")).startswith("✅"):
                self.load_summary[name] = "✅"
        # Prepare sub-components; a prepare() failure flips that component to ❌
        # rather than being silently swallowed.
        failed = []
        for name in order:
            component = getattr(self, name, None)
            if component and hasattr(component, "prepare"):
                try:
                    component.prepare()
                except Exception as e:
                    failed.append(name)
                    self.load_summary[name] = "❌"
                    self.logger.log_error(f"[{cls}] ❌ {name}.prepare(): {e}")
        names = list(self.component_dependencies.keys())
        n_ok = sum(1 for n in names if str(self.load_summary.get(n, '')).startswith('✅'))
        if failed:
            self.logger.log_warning(
                f"[{cls}] {n_ok}/{len(names)} components prepared; failed: {', '.join(failed)}")
        else:
            self.logger.log_debug(f"[{cls}] {len(names)}/{len(names)} components prepared")

    def _is_reachable(self) -> bool:
        """Quick connectivity check — returns False if Tautulli is unreachable or misconfigured."""
        if not self.tautulli_api or not self.tautulli_api.api_key:
            self.logger.log_warning(
                "[Tautulli] No API key configured — check 'tautulli.api' in config. Skipping."
            )
            return False
        resp = self.tautulli_api.get_server_info()
        if not resp:
            self.logger.log_warning(
                f"[Tautulli] Server unreachable at {self.tautulli_api.base_url} — skipping data collection."
            )
            return False
        return True

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self):
        self.logger.log_info("[Tautulli] Starting full data collection...")

        if not self._is_reachable():
            return

        # 1. User list — real-time (fast Tautulli call)
        user_list = self.users.get_all_users()
        self.logger.log_info(f"[Tautulli] {len(user_list)} users found.")

        # 2. Full watch history, paginated — cached 24 h
        all_entries = self.watch_history.get_all_history_cached()
        self.logger.log_info(f"[Tautulli] {len(all_entries)} history entries loaded.")

        # 3. (removed) Per-user watch-time / player-stats fetch.
        #    This fired 2 real-time API calls per user but the results were only
        #    debug-logged — never stored or consumed downstream. Removed as dead
        #    work. Re-add WITH caching (global_cache.set) if a consumer ever needs
        #    per-user watch-time / player stats.

        # 4. Metadata index — cached 7 d (one API call per unique rating_key)
        rating_keys = list({str(e.get("rating_key")) for e in all_entries if e.get("rating_key")})
        metadata_index = self.metadata.get_metadata_index_cached(rating_keys)
        self.logger.log_info(f"[Tautulli] Metadata index: {len(metadata_index)} unique items.")

        # 5. Library list — real-time
        library_index = self.metadata.get_library_index()
        self.logger.log_info(f"[Tautulli] {len(library_index)} libraries found.")

        # 6. (removed) Tautulli play-statistics aggregation calls.
        #    8 calls were fired here but their return values were discarded (the
        #    API layer does not cache), so they did no work. Removed. Re-add WITH
        #    assignment + global_cache.set() if these aggregations are ever
        #    surfaced (e.g. in the Discord run summary).

        # 7. Derived stats from the cached history — pure computation, no extra API calls
        transcode_stats  = self.transcode.get_transcode_stats(all_entries)
        platform_stats   = self.devices.get_platform_usage(all_entries)
        device_codec_mtx = self.transcode.get_device_codec_matrix(all_entries)
        fingerprint_mtx  = self.transcode.get_transcode_fingerprint_matrix(all_entries)
        series_stats     = self.series.get_series_completion_stats(all_entries)
        episode_stats    = self.episodes.get_episode_completion_stats(all_entries)
        # Persist the derived play-statistics signals the scorers consume:
        #   tautulli/transcode — stream-codec-pair transcode tally; read by Radarr
        #                        space_pressure and Sonarr episode_files (Group-D).
        #   tautulli/platforms — per-platform play counts; same readers.
        #   tautulli/device_codec_matrix — per-device play/transcode matrix, the
        #                        keystone signal for per-device profile selection.
        #   tautulli/transcode_fingerprint — per-device (codec,audio,sub,res/HDR,location)
        #                        capability matrix (JSON-safe record list); read by the
        #                        Stage-C remote-play gate on the 4K bonus copy.
        if self.global_cache:
            self.global_cache.set("tautulli/transcode", transcode_stats)
            self.global_cache.set("tautulli/platforms", platform_stats)
            self.global_cache.set("tautulli/device_codec_matrix", device_codec_mtx)
            self.global_cache.set("tautulli/transcode_fingerprint", fingerprint_mtx)

        # 8. Genre/actor/director affinity — derived from already-cached inputs
        genre_affinity = self.users.compute_genre_affinity(all_entries, metadata_index)

        # 8a. Per-user affinity matrices — separate signal per Tautulli account,
        #     useful for individual movie / show recommendation vectors.
        per_user_affinity = self.users.compute_per_user_genre_affinity(
            all_entries, metadata_index, user_list
        )
        if self.global_cache:
            for _username, _affinity in per_user_affinity.items():
                # Sanitize: replace Windows-forbidden path characters and the
                # cache-key separator (/) so the username is safe as a directory
                # name on all platforms.  The original username is preserved as
                # a key inside the affinity dict itself.
                _safe = re.sub(r'[\\/:*?"<>|]', '_', _username).strip()
                self.global_cache.set(f"tautulli/users/{_safe}/affinity", _affinity)

        # 9. Store affinity in global cache so Radarr/Trakt can consume it
        if self.global_cache:
            self.global_cache.set("tautulli/affinity", genre_affinity)

        # 10. Group movie completions — per-group max completion %, keyed by tmdb_id.
        #     Default to a single household-wide group when none is configured so
        #     the tmdb_completions map (consumed by Radarr ratings/space-pressure
        #     AND the owned-movie watched-set) is always built. A memberless
        #     "household" group counts every user (see get_group_movie_completions).
        rating_groups_cfg = (self.config.get("rating_groups", {}) if self.config else {}) or {"household": {}}
        if rating_groups_cfg:
            group_completions = self.watch_history.get_group_movie_completions(
                all_entries, rating_groups_cfg
            )
            for group_name, rk_map in group_completions.items():
                tmdb_map: dict = {}
                resolved_rk_count = 0  # rating_keys that DID resolve to a tmdb_id
                # Unresolved rating_keys split into two buckets:
                #   not_in_metadata  — API returned nothing (item deleted from Plex)
                #   no_tmdb_guid     — metadata fetched but no tmdb:// GUID present
                unresolved_not_in_metadata: list = []
                unresolved_no_tmdb_guid: dict    = {}  # rk → {title, year, guids, guid}

                # Build a title+year → tmdb_id lookup from Radarr for fallback
                # resolution of deleted Plex items.
                radarr_title_map: dict[tuple, int] = {}
                try:
                    radarr_mgr = self.registry.get("manager", "RadarrManager")
                    if radarr_mgr and self.global_cache:
                        all_movies = self.global_cache.get("radarr.movies.standard.full") or []
                        for m in all_movies:
                            t = (m.get("title") or "").lower().strip()
                            y = str(m.get("year") or "")
                            tid = m.get("tmdbId")
                            if t and tid:
                                radarr_title_map[(t, y)] = int(tid)
                                # Also index without year as looser fallback
                                if (t, "") not in radarr_title_map:
                                    radarr_title_map[(t, "")] = int(tid)
                except Exception:
                    pass

                for rk, data in rk_map.items():
                    md      = metadata_index.get(rk)
                    tmdb_id = md.get("tmdb_id") if md else None

                    if not tmdb_id:
                        if md is None:
                            # Deleted item — attempt title+year fallback via Radarr
                            unresolved_not_in_metadata.append(rk)
                        else:
                            guid = md.get("guid", "")
                            # Filter out trailers/extras — iva:// and local:// are
                            # never real movies with tmdb IDs, skip silently.
                            if isinstance(guid, str) and (
                                guid.startswith("iva://")
                                or guid.startswith("local://")
                            ):
                                continue
                            # Try imdb → tmdb bridge via Radarr movie list
                            imdb_id = next(
                                (g.get("id", "")[7:] for g in (md.get("guids") or [])
                                 if isinstance(g, dict) and g.get("id", "").startswith("imdb://")),
                                None
                            )
                            if imdb_id and self.global_cache:
                                try:
                                    radarr_mgr = self.registry.get("manager", "RadarrManager")
                                    if radarr_mgr:
                                        all_movies = self.global_cache.get("radarr.movies.standard.full") or []
                                        matched = next(
                                            (m for m in all_movies if m.get("imdbId") == imdb_id),
                                            None
                                        )
                                        if matched and matched.get("tmdbId"):
                                            tmdb_id = int(matched["tmdbId"])
                                except Exception:
                                    pass

                            if not tmdb_id:
                                unresolved_no_tmdb_guid[rk] = {
                                    "title": md.get("title", ""),
                                    "year":  md.get("year"),
                                    "guids": md.get("guids", []),
                                    "guid":  guid,
                                }
                                continue

                    # Fallback for deleted items: try title+year match against Radarr
                    if not tmdb_id and md is None and radarr_title_map:
                        # We don't have metadata for this rk, so we can't resolve
                        # by title here — it stays in not_in_metadata bucket.
                        pass

                    if not tmdb_id:
                        continue

                    resolved_rk_count += 1
                    existing = tmdb_map.get(tmdb_id, {})
                    if data.get("pct", 0.0) >= existing.get("pct", 0.0):
                        tmdb_map[tmdb_id] = data

                if self.global_cache:
                    self.global_cache.set(
                        f"tautulli/group/{group_name}/tmdb_completions",
                        tmdb_map,
                    )
                    # Cache unresolved keys so they can be investigated manually.
                    # not_in_metadata  → item was deleted/replaced in Plex; try:
                    #   GET /api/v2?cmd=get_metadata&rating_key=<rk>
                    # no_tmdb_guid     → item still exists but has no tmdb:// GUID
                    #   (likely imdb-only); guids field shows what IS present.
                    no_tmdb_count = len(unresolved_not_in_metadata) + len(unresolved_no_tmdb_guid)
                    if no_tmdb_count:
                        self.global_cache.set(
                            f"tautulli/debug/group/{group_name}/unresolved_rating_keys",
                            {
                                "total_unresolved":    no_tmdb_count,
                                "not_in_metadata":     unresolved_not_in_metadata,
                                "no_tmdb_guid":        unresolved_no_tmdb_guid,
                            },
                        )
                        self.logger.log_debug(
                            f"[Tautulli] Group '{group_name}': unresolved rating_keys cached → "
                            f"tautulli/debug/group/{group_name}/unresolved_rating_keys "
                            f"({len(unresolved_not_in_metadata)} missing from metadata, "
                            f"{len(unresolved_no_tmdb_guid)} missing tmdb guid)"
                        )

                # resolved_rk_count - len(tmdb_map) = rating_keys that mapped to a
                # tmdb_id that was already seen (same movie, multiple Plex entries).
                duplicate_rk_count = resolved_rk_count - len(tmdb_map)
                no_tmdb_count      = len(unresolved_not_in_metadata) + len(unresolved_no_tmdb_guid)
                detail_parts = []
                if duplicate_rk_count:
                    detail_parts.append(
                        f"{duplicate_rk_count} duplicate Plex entr{'ies' if duplicate_rk_count != 1 else 'y'} "
                        f"(same movie, multiple rating_keys)"
                    )
                if len(unresolved_not_in_metadata):
                    detail_parts.append(
                        f"{len(unresolved_not_in_metadata)} missing from metadata (deleted/replaced items)"
                    )
                if len(unresolved_no_tmdb_guid):
                    detail_parts.append(
                        f"{len(unresolved_no_tmdb_guid)} have no tmdb guid (imdb-only or unidentified)"
                    )
                detail = f" [{', '.join(detail_parts)}]" if detail_parts else ""
                self.logger.log_info(
                    f"[Tautulli] Group '{group_name}': {len(rk_map)} Plex items tracked, "
                    f"{len(tmdb_map)} unique movies resolved to tmdb_id{detail}."
                )

        self.logger.log_table(
            ["Signal", "Count"],
            [
                ["users",            len(user_list)],
                ["history entries",  len(all_entries)],
                ["metadata items",   len(metadata_index)],
                ["libraries",        len(library_index)],
                ["shows",            len(series_stats)],
                ["transcode formats", len(transcode_stats)],
                ["platforms",        len(platform_stats)],
                ["genres tracked",   len(genre_affinity.get('genres', {}))],
            ],
            title="[Tautulli] Run complete",
            caption="Final tally of the signals this Tautulli collection run produced.",
            descriptions=[
                "Tautulli/Plex accounts found",
                "watch-history records loaded from cache",
                "unique rating_keys with cached metadata",
                "Plex libraries discovered",
                "shows with completion stats computed",
                "stream codec-pair transcode formats tallied",
                "distinct client platforms seen in history",
                "genres present in the household affinity map",
            ],
        )
