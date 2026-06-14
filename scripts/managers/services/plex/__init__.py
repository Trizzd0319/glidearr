"""
PlexManager — the household's explicit forward-intent + identity layer.
================================================================================
Plex is **FETCH/CACHE-only (v1)** — no APPLY, no write-backs. Its one job is to
add the signals only Plex has natively and join them to the existing model:

  * the multi-user account **watchlist** (top-weighted next-watch signal +
    watchlisted-but-not-owned → acquisition candidates),
  * the **Plex-Home-user ↔ Tautulli-user ↔ rating_groups identity crosswalk** that
    lets every per-user Plex signal join the existing affinity/completion model.

The deterministic A–G scorecard stays the curation authority; Plex never owns play
history, the watched-set, affinity, or deletion (DESIGN §1).

Two passes (DESIGN §3.6), driven from main.py:
  * ``run()`` — inventory / identity / watchlist, Phase 2 **before Trakt**, so
    acquisition (last phase) reads ``plex/watchlist/union`` warm.
  * ``run_reconcile()`` — pure zero-API set-diff, Phase 2 **after Radarr+Sonarr**
    populate their library caches.

Plex is **NON-critical**: like MAL it self-disables when unconfigured / unreachable
/ scope-fails, and is left OUT of Main._validate_managers — a Plex-less or
scope-failed install must still run. Structure mirrors TautulliManager exactly.
"""
from __future__ import annotations

import uuid
from typing import Optional

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.plex.api import PlexAPI
from scripts.managers.services.plex.collections import PlexCollectionsManager
from scripts.managers.services.plex.episodes import PlexEpisodesManager
from scripts.managers.services.plex.libraries import PlexLibrarySectionsManager
from scripts.managers.services.plex.metadata import PlexMetadataManager
from scripts.managers.services.plex.movies import PlexMoviesManager
from scripts.managers.services.plex.on_deck import PlexOnDeckManager
from scripts.managers.services.plex.playlists import PlexPlaylistsManager
from scripts.managers.services.plex.playlists.builder import PlexPlaylistBuilderManager
from scripts.managers.services.plex.playlists.combined_builder import CombinedPlaylistBuilderManager
from scripts.managers.services.plex.playlists.movie_builder import MoviePlaylistBuilderManager
from scripts.managers.services.plex.ratings import PlexRatingsManager
from scripts.managers.services.plex.users import PlexUsersManager
from scripts.managers.services.plex.validator import PlexValidatorManager
from scripts.managers.services.plex.watchlist import PlexWatchlistManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class PlexManager(BaseManager, ComponentManagerMixin):
    parent_name = "PlexManager"

    plex_api:          Optional[PlexAPI] = None
    users:             Optional[PlexUsersManager] = None
    metadata:          Optional[PlexMetadataManager] = None
    watchlist:         Optional[PlexWatchlistManager] = None
    on_deck:           Optional[PlexOnDeckManager] = None
    ratings:           Optional[PlexRatingsManager] = None
    libraries:         Optional[PlexLibrarySectionsManager] = None
    episodes:          Optional[PlexEpisodesManager] = None
    movies:            Optional[PlexMoviesManager] = None
    collections:       Optional[PlexCollectionsManager] = None
    validator_manager: Optional[PlexValidatorManager] = None
    playlists:         Optional[PlexPlaylistsManager] = None
    playlist_builder:  Optional[PlexPlaylistBuilderManager] = None
    movie_playlist_builder: Optional[MoviePlaylistBuilderManager] = None
    combined_playlist_builder: Optional[CombinedPlaylistBuilderManager] = None

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "PlexManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        # dry_run footgun: BaseManager does NOT capture it. Plex is FETCH/CACHE-only
        # in v1 so it gates nothing yet, but the wiring must exist so any future
        # write (collection write-back, ratings sync) is gated from day one.
        parent = kwargs.get("manager")
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        # FLAT config block {url, port, plex_token, plex_media_path} — NOT the nested
        # {"default": {...}} Tautulli collapses. Token key is plex_token.
        plex_cfg = (self.config.get("plex", {}) if self.config else {}) or {}
        self.configured = bool(plex_cfg.get("plex_token"))

        # STABLE X-Plex-Client-Identifier (DESIGN Q2): v2 endpoints 401 silently
        # without it and a per-run uuid4 spawns device churn / 2FA. Resolve once,
        # persist to config, reuse across all calls and runs — but ONLY when Plex is
        # actually configured, so a Plex-less install's config.json stays untouched.
        self.client_identifier = self._ensure_client_identifier(plex_cfg) if self.configured else None

        self.plex_api = PlexAPI(logger=self.logger, instance_config=plex_cfg,
                                client_identifier=self.client_identifier)
        self.enabled = self.configured
        self.account_scope_ok = False
        self.pms_version: str | None = None

        # Per-user minted tokens: IN-MEMORY ONLY (security, non-negotiable). A shared
        # dict reference threaded through init_args — PlexUsersManager populates it
        # once per run; every per-user fetcher reads it. NEVER written to any cache.
        self.user_tokens: dict[str, str] = {}

        self.init_args = {
            "logger":           self.logger,
            "config":           self.config,
            "global_cache":     self.global_cache,
            "registry":         self.registry,
            "validator":        self.validator,
            "plex_api":         self.plex_api,
            "user_tokens":      self.user_tokens,
            "parent_name":      self.parent_name,
            "dry_run":          self.dry_run,
        }

        self.component_dependencies = {
            "users":            [],
            "metadata":         [],
            "watchlist":        [],
            "on_deck":          [],
            "ratings":          [],
            "libraries":        [],
            "episodes":         [],
            "movies":           [],
            "collections":      [],
            "playlists":        [],
            "playlist_builder": [],
            "movie_playlist_builder": [],
            "combined_playlist_builder": [],
            "validator_manager": [],
        }

        self.all_component_classes = {
            "users":            PlexUsersManager,
            "metadata":         PlexMetadataManager,
            "watchlist":        PlexWatchlistManager,
            "on_deck":          PlexOnDeckManager,
            "ratings":          PlexRatingsManager,
            "libraries":        PlexLibrarySectionsManager,
            "episodes":         PlexEpisodesManager,
            "movies":           PlexMoviesManager,
            "collections":      PlexCollectionsManager,
            "playlists":        PlexPlaylistsManager,
            "playlist_builder": PlexPlaylistBuilderManager,
            "movie_playlist_builder": MoviePlaylistBuilderManager,
            "combined_playlist_builder": CombinedPlaylistBuilderManager,
            "validator_manager": PlexValidatorManager,
        }

        # The irreducible v1 (P0+P1). Everything else layers on top and is
        # noncritical. Plex itself is non-critical at the Main level regardless.
        self.critical_keys = {"users", "metadata", "watchlist"}

        self.critical_components, self.noncritical_components = split_components(
            all_components=self.all_component_classes,
            critical_keys=self.critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=self.init_args,
        )

        self.load_summary = {}
        self.logger.log_debug(f"[{self.__class__.__name__}] initialized (configured={self.configured})")

    # ── client identifier (stable, persisted) ───────────────────────────────
    def _ensure_client_identifier(self, plex_cfg: dict) -> str:
        cid = plex_cfg.get("client_identifier")
        if cid:
            return cid
        cid = str(uuid.uuid4())
        try:
            new_cfg = dict(plex_cfg)
            new_cfg["client_identifier"] = cid
            if self.config and hasattr(self.config, "set"):
                self.config.set("plex", new_cfg)
                self.logger.log_debug("[Plex] generated + persisted a stable X-Plex-Client-Identifier.")
        except Exception as e:
            self.logger.log_debug(f"[Plex] could not persist client_identifier (using ephemeral): {e}")
        return cid

    # ── component loading (mirrors TautulliManager) ──────────────────────────
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
        for name in self.component_dependencies:
            if getattr(self, name, None) is None:
                self._load_component(name)
            elif not str(self.load_summary.get(name, "")).startswith("✅"):
                self.load_summary[name] = "✅"
        failed = []
        for name in self.component_dependencies:
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

    # ── reachability ─────────────────────────────────────────────────────────
    def _is_reachable(self) -> bool:
        """Local-PMS connectivity probe (/identity). Captures the PMS version for
        version-dependent parse fallbacks (DESIGN §6.3). Returns False if Plex is
        unconfigured/unreachable — the run then degrades, never aborts."""
        if not self.plex_api or not self.plex_api.configured:
            self.logger.log_warning("[Plex] No token configured — check 'plex.plex_token'. Skipping.")
            return False
        ident = self.plex_api.get_identity()
        if not ident:
            self.logger.log_warning(
                f"[Plex] Server unreachable at {self.plex_api.base_url} — skipping local-PMS passes."
            )
            return False
        mc = ident.get("MediaContainer", ident) if isinstance(ident, dict) else {}
        self.pms_version = (mc or {}).get("version")
        return True

    # ── PASS 1: inventory / identity / watchlist (before Trakt) ──────────────
    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self):
        stats: dict = {
            "configured": self.configured, "enabled": self.enabled,
            "pms_version": None, "scope_ok": False,
            "users_tracked": 0, "users_pin_skipped": 0,
            "watchlist_items": 0, "watchlist_users": 0,
            "guid_network_hops": 0, "calls_made": 0,
        }
        if not self.enabled:
            self.logger.log_debug("[Plex] disabled (no token) — skipping inventory pass.")
            self._write_stats(stats)
            return

        reachable = self._is_reachable()
        stats["pms_version"] = self.pms_version

        # Prime the shared GUID resolver (load guid_map + build bridge dicts) once
        # so every per-user fetcher reuses one resolver / one guid_map this run.
        try:
            if self.metadata:
                self.metadata.prime()
        except Exception as e:
            self.logger.log_warning(f"[Plex] metadata prime failed: {e}")

        # Identity: scope-probe → Home enum → mint per-user tokens → crosswalk.
        # This is the gate for the whole per-user surface (watchlist/on_deck/ratings).
        try:
            if self.users:
                user_stats = self.users.run() or {}
                self.account_scope_ok = bool(user_stats.get("scope_ok"))
                stats.update({k: user_stats.get(k, stats.get(k)) for k in
                              ("scope_ok", "users_tracked", "users_pin_skipped")})
        except Exception as e:
            self.logger.log_error(f"[Plex] users/identity pass failed: {e}")

        # Watchlist (flagship, P1) — needs the per-user tokens just minted.
        try:
            if self.watchlist and self.account_scope_ok:
                wl_stats = self.watchlist.run() or {}
                stats["watchlist_items"] = wl_stats.get("items", 0)
                stats["watchlist_users"] = wl_stats.get("users", 0)
            elif self.watchlist and not self.account_scope_ok:
                self.logger.log_warning(
                    "[Plex] account scope not verified — watchlist degraded to owner-only/empty.")
        except Exception as e:
            self.logger.log_error(f"[Plex] watchlist pass failed: {e}")

        # On-deck (P2 enrichment) — gated; emits its own key for forward A/B. Uses the
        # local PMS (/library/onDeck) so it needs ``reachable`` too.
        try:
            if self.on_deck and self._cap_enabled("on_deck") and self.account_scope_ok and reachable:
                self.on_deck.run()
        except Exception as e:
            self.logger.log_error(f"[Plex] on_deck pass failed: {e}")

        # Per-user ratings (P2) — gated; folds into scoring/_shared.user_rating_score.
        # Reads the local PMS (/library/sections/all) so it needs ``reachable`` too.
        try:
            if self.ratings and self._cap_enabled("ratings") and self.account_scope_ok and reachable:
                self.ratings.run()
        except Exception as e:
            self.logger.log_error(f"[Plex] ratings pass failed: {e}")

        # Section inventory + (when reconcile.enabled) the resolved id-scan that
        # run_reconcile() diffs against the *arr libraries — local-PMS, cheap index
        # always, heavy id-scan only when opted in.
        try:
            if self.libraries and reachable:
                self.libraries.run()
        except Exception as e:
            self.logger.log_error(f"[Plex] libraries inventory failed: {e}")

        # Owned-episode tvdb→ratingKey map + coverage probe (personal playlists, P2-3)
        # — gated default-off via plex.episodes.enabled. Local-PMS only; consumes the
        # section index libraries just built, so it runs right after.
        try:
            if self.episodes and self._cap_enabled("episodes") and reachable:
                self.episodes.run()
        except Exception as e:
            self.logger.log_error(f"[Plex] episodes scan failed: {e}")

        # Owned-movie tmdb→ratingKey map + coverage probe (movie personal playlists) —
        # gated default-off via plex.movies.enabled. The movie analog of the episode scan.
        try:
            if self.movies and self._cap_enabled("movies") and reachable:
                self.movies.run()
        except Exception as e:
            self.logger.log_error(f"[Plex] movies scan failed: {e}")

        # Collections (P4, default-off) + sessions diagnostic — local-PMS only.
        try:
            if self.collections and self._cap_enabled("collections") and reachable:
                self.collections.run()
        except Exception as e:
            self.logger.log_error(f"[Plex] collections pass failed: {e}")
        try:
            if self.playlists and self._cap_enabled("playlists") and reachable:
                self.playlists.run()
        except Exception as e:
            self.logger.log_error(f"[Plex] playlists pass failed: {e}")
        try:
            if self._cap_enabled("sessions") and reachable:
                self._sessions_diagnostic()
        except Exception as e:
            self.logger.log_debug(f"[Plex] sessions diagnostic skipped: {e}")

        # Persist the merged guid_map exactly once (so each plex:// resolves ≤ once ever).
        try:
            if self.metadata:
                hops = self.metadata.flush()
                stats["guid_network_hops"] = hops
        except Exception as e:
            self.logger.log_warning(f"[Plex] guid_map flush failed: {e}")

        stats["calls_made"] = getattr(self.plex_api, "calls_made", 0)
        self._write_stats(stats)
        self.logger.log_info(
            f"[Plex] inventory complete — scope_ok={stats['scope_ok']} · "
            f"{stats['users_tracked']} users ({stats['users_pin_skipped']} pin-skipped) · "
            f"{stats['watchlist_items']} watchlist items across {stats['watchlist_users']} users · "
            f"{stats['guid_network_hops']} guid hops · {stats['calls_made']} calls."
        )

    # ── PASS 2: reconcile (after Radarr+Sonarr; zero API) ────────────────────
    @LoggerManager().log_function_entry
    @timeit("run_reconcile")
    def run_reconcile(self):
        if not self.enabled:
            return
        try:
            if self.libraries and self._cap_enabled("reconcile"):
                self.libraries.run_reconcile()
        except Exception as e:
            self.logger.log_error(f"[Plex] reconcile pass failed: {e}")

        # Per-user TV playlist build + dry-run preview (P2-5b) — runs HERE, in the
        # post-Radarr+Sonarr reconcile phase, so the on-demand owned-episode build hits
        # warm Sonarr caches and a VALIDATED Sonarr API (in PASS 1 the Sonarr API isn't
        # validated yet → live fetches 'No validated API' + cold misses). The episode
        # scan that produced plex/episodes/owned_inventory already ran in PASS 1, and its
        # cache persists into this phase. BUILD+CACHE only, NO Plex writes.
        try:
            if self.playlist_builder and self._cap_enabled("episodes"):
                self.playlist_builder.run()
        except Exception as e:
            self.logger.log_error(f"[Plex] playlist builder failed: {e}")

        # Per-user MOVIE playlist build + dry-run preview — same post-Radarr+Sonarr phase
        # so the Radarr movie_files.parquet is warm; consumes plex/movies/owned_inventory
        # (built in PASS 1). Gated plex.movies.enabled. BUILD+CACHE only, NO Plex writes.
        try:
            if self.movie_playlist_builder and self._cap_enabled("movies"):
                self.movie_playlist_builder.run()
        except Exception as e:
            self.logger.log_error(f"[Plex] movie playlist builder failed: {e}")

        # Per-user COMBINED (movie + TV) cross-medium plan — needs the movie inventory, so
        # gated on plex.movies.enabled; the TV half degrades gracefully if episodes are off.
        try:
            if self.combined_playlist_builder and self._cap_enabled("movies"):
                self.combined_playlist_builder.run()
        except Exception as e:
            self.logger.log_error(f"[Plex] combined playlist builder failed: {e}")

    # ── helpers ───────────────────────────────────────────────────────────────
    def _cap_enabled(self, cap: str) -> bool:
        """Per-capability opt-in flag under config plex.<cap>.enabled (default-off for
        the deferred/A-B capabilities)."""
        plex_cfg = (self.config.get("plex", {}) if self.config else {}) or {}
        return bool((plex_cfg.get(cap, {}) or {}).get("enabled", False))

    def _sessions_diagnostic(self):
        """One cached now-playing snapshot for run-summary colour (DESIGN §2: build
        nothing on it)."""
        resp = self.plex_api.get_sessions()
        mc = (resp or {}).get("MediaContainer", {}) if isinstance(resp, dict) else {}
        n = int(mc.get("size", 0) or 0)
        if self.global_cache:
            self.global_cache.set("plex/sessions", {"active": n})
        self.logger.log_debug(f"[Plex] sessions snapshot: {n} active stream(s).")

    def _write_stats(self, stats: dict):
        if self.global_cache:
            try:
                self.global_cache.set("plex/run_stats", stats)
            except Exception:
                pass
