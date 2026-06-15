"""
services/routing/__init__.py — the in-run library re-organizer (RoutingManager).
================================================================================
Reconciles ALREADY-OWNED movies + shows to the correct library FOLDER when a late
signal (a Common Sense age arriving, anime detection, or a changed routing preference)
means a title's classified bucket no longer matches where it sits on disk.

Gated three ways, so it is inert until the operator opts in:
  • routing.configured — the routing onboarding step has run (else: skip entirely, so a
                         never-onboarded install does nothing).
  • routing.reorg_mode  — off (skip) / log_only (classify + LOG misplacements, move
                          NOTHING) / same_instance (actuate same-instance folder moves).
  • relocation_enabled  — same_instance ALSO requires explicit move consent; and even then
                          a dry_run never PUTs. Cross-instance migration (anime / 4K
                          instance) is NOT done here — that stays a separate, deferred path.

Classification + the move plan are the shared, pure ``library_router`` / ``library_classifier``
(identical to the add-time resolver, so add-time and re-org never disagree). This manager is
only the driver: fetch owned items via the engine's arr API, plan, then LOG or APPLY.
"""
from __future__ import annotations

from scripts.managers.machine_learning.classification import library_router
from scripts.managers.machine_learning.space.routing_targets import reorg_mode, relocation_enabled
from scripts.managers.services.mdblist import age_cache
from scripts.support.utilities.library_classifier import classify_movie, classify_show, is_anime_media


class RoutingManager:
    def __init__(self, config=None, logger=None, *, radarr=None, sonarr=None, dry_run=False, **kwargs):
        self.config = config or {}
        self.logger = logger
        self.dry_run = bool(dry_run)
        self._radarr_im = self._im(radarr)
        self._sonarr_im = self._im(sonarr)
        self._routing = self.config.get("routing", {}) or {}
        self._root_folders = self.config.get("rootFolders", {}) or {}
        self._movie_root_folders = self.config.get("movieRootFolders", {}) or {}
        self._anime_genres = {str(g).lower() for g in (self.config.get("animeGenres", []) or []) if g}
        self._kids_genres = [str(g) for g in (self.config.get("kidsGenres", []) or []) if g]
        self._kids_certs = [str(c) for c in (self.config.get("kidsCertifications", []) or []) if c]
        self._reality_genres = [str(g) for g in (self.config.get("realityGenres", []) or []) if g]
        self._doc_genres = [str(g) for g in (self.config.get("documentaryGenres", []) or []) if g]
        self._preschool_genres = [str(g) for g in (self.config.get("preschoolGenres", []) or []) if g]
        self._non_kids_genres = [str(g) for g in (self.config.get("nonKidsGenres", []) or []) if g]
        self._movie_ages = None       # CSM caches, lazy-loaded once
        self._show_ages = None

    @staticmethod
    def _im(mgr):
        """The instance-manager / arr-api off a service manager (exposes _get_apis + _make_request)."""
        if mgr is None:
            return None
        return (getattr(mgr, "instance_manager", None) or getattr(mgr, "radarr_api", None)
                or getattr(mgr, "sonarr_api", None))

    def _log(self, level, msg):
        if self.logger and hasattr(self.logger, level):
            getattr(self.logger, level)(msg)

    # ── entry ─────────────────────────────────────────────────────────────────
    def run(self):
        if not self._routing.get("configured"):
            return                              # never-onboarded → today's behaviour (nothing)
        mode = reorg_mode(self.config)
        if mode == "off":
            return
        self._reorg(is_show=False, im=self._radarr_im, get_ep="movie",
                    put_ep="movie/editor", id_key="movieIds", mode=mode)
        self._reorg(is_show=True, im=self._sonarr_im, get_ep="series",
                    put_ep="series/editor", id_key="seriesIds", mode=mode)

    # ── per-service ───────────────────────────────────────────────────────────
    def _reorg(self, *, is_show, im, get_ep, put_ep, id_key, mode):
        if im is None or not hasattr(im, "_get_apis") or not hasattr(im, "_make_request"):
            return
        classify = self._classifier(is_show)
        anime_media_fn = (lambda it: self._anime_media(it)) if is_show else None
        # same_instance moves require the mode AND consent AND a live (non-dry) run.
        apply = (mode == "same_instance") and relocation_enabled(self.config) and not self.dry_run
        for name in list((im._get_apis() or {}).keys()):
            try:
                items = im._make_request(name, get_ep, fallback=[]) or []
            except Exception as e:
                self._log("log_warning", f"[Routing] {get_ep} fetch failed for '{name}': {e}")
                continue
            plans = library_router.plan_moves(
                items, is_show=is_show, routing=self._routing,
                root_folders=self._root_folders, movie_root_folders=self._movie_root_folders,
                classify=classify, anime_media=anime_media_fn)
            if not plans:
                continue
            kind = "show" if is_show else "movie"
            self._log("log_info", f"[Routing] {name}: {len(plans)} {kind}(s) misplaced "
                                  f"({'applying same-instance moves' if apply else 'log only'})")
            for p in plans:
                self._log("log_info", f"   {p['title']}: {p['current_root'] or '?'} -> "
                                      f"{p['target_root'] or '(stay)'}  [{p['reason']}]")
            if apply:
                self._apply(im, name, put_ep, id_key, plans, is_show)

    def _apply(self, im, name, put_ep, id_key, plans, is_show):
        groups: dict = {}
        for p in plans:
            if p.get("id") is None:
                continue
            groups.setdefault((p.get("target_root"), p.get("new_series_type")), []).append(p["id"])
        for (target, stype), ids in groups.items():
            payload = {id_key: ids, "moveFiles": bool(target)}
            if target:
                payload["rootFolderPath"] = target
            if is_show and stype:
                payload["seriesType"] = stype
            try:
                im._make_request(name, put_ep, method="PUT", payload=payload)
                self._log("log_success", f"[Routing] {name}: relocated {len(ids)} -> {target or '(seriesType only)'}")
            except Exception as e:
                self._log("log_warning", f"[Routing] {name}: editor batch failed ({len(ids)}): {e}")

    # ── classification helpers (mirror the add-time resolver) ─────────────────
    def _movie_age(self, tmdb):
        if self._movie_ages is None:
            self._movie_ages = age_cache.load(age_cache.AGE_CACHE_PATH)
        return age_cache.age_for(tmdb, cache=self._movie_ages)

    def _show_age(self, tmdb):
        if self._show_ages is None:
            self._show_ages = age_cache.load(age_cache.TV_AGE_CACHE_PATH)
        return age_cache.age_for(tmdb, cache=self._show_ages)

    @staticmethod
    def _olang(it):
        ol = it.get("originalLanguage")
        return ol.get("name") if isinstance(ol, dict) else ol

    def _classifier(self, is_show):
        """A ``classify(item) -> category`` closure over the live arr object, matching the
        resolver's classify call (CSM-primary). is_uhd is left False — the same-instance
        re-organizer routes by content; the 4K/anime INSTANCE split is the deferred path."""
        if is_show:
            def classify(it):
                return classify_show(
                    genres=it.get("genres"), certification=it.get("certification"),
                    series_type=it.get("seriesType"), original_language=self._olang(it),
                    recommended_age=self._show_age(it.get("tmdbId")),
                    anime_genres=self._anime_genres, kids_genres=self._kids_genres,
                    kids_certs=self._kids_certs, reality_genres=self._reality_genres,
                    documentary_genres=self._doc_genres, preschool_genres=self._preschool_genres,
                    non_kids_genres=self._non_kids_genres)
            return classify

        def classify(it):
            return classify_movie(
                genres=it.get("genres"), certification=it.get("certification"),
                original_language=self._olang(it), studio=it.get("studio"),
                recommended_age=self._movie_age(it.get("tmdbId")), is_uhd=False,
                anime_genres=self._anime_genres, kids_genres=self._kids_genres,
                kids_certs=self._kids_certs, preschool_genres=self._preschool_genres,
                non_kids_genres=self._non_kids_genres)
        return classify

    def _anime_media(self, it):
        return is_anime_media(
            genres=it.get("genres"), series_type=it.get("seriesType"),
            original_language=self._olang(it), anime_genres=self._anime_genres)
