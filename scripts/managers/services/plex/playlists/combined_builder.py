"""
plex/playlists/combined_builder.py — per-user COMBINED (movie + TV) "Up Next" plan.
================================================================================
The cross-medium builder. For each tracked profile it gathers BOTH the TV candidates
(owned episodes → next-unwatched) and the MOVIE candidates (owned movies), age-gates each
medium, scores each by the same per-user model (affinity > JIT > household; movies have no
JIT), then merges them into ONE list and orders it with ``normalize_per_medium=True`` so a
top show interleaves fairly with a top film. Caches ``plex/playlists/combined_plan/{user}``
and logs a rich preview (Title | Kind | Rank | Why). Writes NOTHING to Plex; gated
``plex.movies.enabled`` (it needs the movie inventory; episodes are the TV half).

Subclasses MoviePlaylistBuilderManager → inherits every TV + movie helper (load/score/
reason/cert/affinity/preview); this module is pure orchestration + the cross-medium merge.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.cert_gate import (
    cert_allowed,
    is_restricted,
    tier_level,
)
from scripts.managers.services.plex._common import anon_label
from scripts.managers.services.plex.playlists.combined_resolver import build_combined_plan
from scripts.managers.services.plex.playlists.movie_builder import MoviePlaylistBuilderManager
from scripts.managers.services.plex.playlists.movie_resolver import movie_inputs
from scripts.managers.services.plex.playlists.tv_resolver import tv_inputs

_TV_INVENTORY_KEY = "plex/episodes/owned_inventory"
_MOVIE_INVENTORY_KEY = "plex/movies/owned_inventory"
_PLAN_KEY = "plex/playlists/combined_plan"          # + /{safe_user}
_GLIDE_PLAN_KEY = "plex/playlists/glide_plan"        # + /{safe_user} — "The Long Glide" (in-progress)
_TOUCHGO_PLAN_KEY = "plex/playlists/touchgo_plan"    # + /{safe_user} — "Touch & Go" (standalones)
# Union of movie tmdbIds in the combined plans — the space coordinator reads it (alongside
# the movie-only key) to shield recommended titles from the delete pool.
_PROTECTED_KEY = "plex/playlists/protected_movie_tmdbs/combined"


class CombinedPlaylistBuilderManager(MoviePlaylistBuilderManager):
    parent_name = "PlexManager"

    def _mood_lists_enabled(self) -> bool:
        """plex.playlists.mood_lists.enabled — build the two mood lists (The Long Glide +
        Touch & Go) alongside the blended Up Next. Default OFF → byte-identical (nothing built)."""
        return bool((self._pl_cfg().get("mood_lists", {}) or {}).get("enabled", False))

    # ── run (I/O gather → tested core) ──────────────────────────────────────────
    def run(self) -> dict:
        tracked = self._tracked_users()
        owned_eps = self._load_owned_episodes()
        owned_movies = self._load_owned_movies()
        tv_inv = self._cache_get(_TV_INVENTORY_KEY, {})
        movie_inv = self._cache_get(_MOVIE_INVENTORY_KEY, {})
        series_scores, series_genres = self._series_scores_and_genres()
        series_certs = self._series_certs()
        jit_by_user = self._jit_series_by_user(tracked)
        tv_watched = {u["safe_user"]: self._watched_for(u.get("tautulli_user_id")) for u in tracked}
        movie_watched = {u["safe_user"]: self._watched_movies_for(u.get("tautulli_user_id")) for u in tracked}
        affinity = {u["safe_user"]: self._user_affinity(u.get("tautulli_username")) for u in tracked}
        franchise_by_series, series_timeline = self._tv_franchise_maps(owned_eps)
        resume_on, resume_order, resume_weight = self._resume_cfg()
        # The Long Glide always orders by recency, so its watch-recency data is needed whenever
        # mood_lists is on too — not only when the blended-list resume_boost flag is set.
        need_recency = resume_on or self._mood_lists_enabled()
        movie_recency = ({u["safe_user"]: self._watched_movie_recency_for(u.get("tautulli_user_id"))
                          for u in tracked} if need_recency else {})
        episode_recency = ({u["safe_user"]: self._watched_episode_recency_for(u.get("tautulli_user_id"))
                            for u in tracked} if need_recency else {})
        return self._build_for_users(
            tracked, owned_eps, owned_movies, tv_inv, movie_inv, series_scores, series_genres,
            series_certs, jit_by_user, tv_watched, movie_watched, affinity,
            series_csm_ages=self._series_csm_ages(), movie_csm_ages=self._movie_csm_ages(),
            universe_order=self._movie_universe_order(movie_inv, owned_movies),
            universe_membership=self._movie_universe_membership(owned_movies),
            franchise_by_series=franchise_by_series, series_timeline=series_timeline,
            resume_boost=resume_on, resume_order=resume_order, resume_weight=resume_weight,
            movie_recency=movie_recency, episode_recency=episode_recency)

    def _build_for_users(self, tracked, owned_eps, owned_movies, tv_inv, movie_inv,
                         series_scores, series_genres, series_certs, jit_by_user,
                         tv_watched, movie_watched, affinity, episode_recency=None,
                         series_csm_ages=None, movie_csm_ages=None,
                         universe_order=None, universe_membership=None,
                         franchise_by_series=None, series_timeline=None,
                         resume_boost=False, resume_order="recency", resume_weight=0.0,
                         movie_recency=None) -> dict:
        if not tv_inv and not movie_inv:
            self.logger.log_warning("[ComboPlaylists] no owned_inventory (TV or movie) — "
                                    "enable plex.episodes.enabled / plex.movies.enabled and run the scans.")
            return {"users": len(tracked), "built": 0, "can_build": False}

        series_csm_ages = series_csm_ages or {}      # cert-gate fallback for uncertified series/movies
        movie_csm_ages = movie_csm_ages or {}
        weights = self._priority_weights()
        tv_hh_max = max((float(s) for s in series_scores.values() if s is not None), default=0.0) or 1.0
        mv_hh_max = max((s for m in owned_movies if (s := self._score(m)) is not None), default=0.0) or 1.0
        tv_display = self._display_map(tv_inv)
        mv_display = {str(v.get("rating_key")):
                      (f"{v.get('title', '')} ({v.get('year')})" if v.get("year") else (v.get("title", "") or str(v.get("rating_key"))))
                      for v in (movie_inv or {}).values() if v.get("rating_key")}
        display = {**tv_display, **mv_display}
        rk_to_tmdb = self._inventory_rk_to_tmdb(movie_inv)   # plan ratingKey -> Radarr tmdbId
        protected: set = set()                               # recommended movie tmdbIds (delete shield)
        built = 0
        for idx, u in enumerate(tracked, 1):
            user_aff = affinity.get(u["safe_user"]) or {}
            user_jit = jit_by_user.get(u["safe_user"], set())
            level = tier_level(u.get("restriction_profile"),
                               self._profile_ages().get(u.get("title")) or self._profile_ages().get(u.get("safe_user")))
            tier_name = self._TIER_NAMES[level] if 0 <= level < len(self._TIER_NAMES) else str(level)
            who = anon_label(u.get("title"), tier_name, idx)   # de-identified handle for the run log

            # AGE GATE each medium independently (owner/adult sees everything). Cert decides
            # when known; an uncertified title falls back to its Common Sense age (same as the
            # standalone TV/movie builders) so the combined list gates consistently with them.
            eps = owned_eps
            movies = owned_movies
            if is_restricted(level):
                eps = [e for e in owned_eps
                       if cert_allowed(series_certs.get(e.get("series_id")), level,
                                       csm_age=series_csm_ages.get(e.get("series_id")))]
                movies = [m for m in owned_movies
                          if cert_allowed(m.get("certification"), level,
                                          csm_age=movie_csm_ages.get(self._coerce_int(m.get("tmdb_id"))))]

            # Cold-start prior from the household's age-appropriate engagement across BOTH media
            # (a restricted profile with no affinity of its own — e.g. a parent co-views).
            user_aff = self._apply_cold_kids_prior(
                user_aff, level,
                self._series_genre_scores(eps, series_genres, series_scores)
                + [(self._as_genre_list(m.get("genres")), self._score(m)) for m in movies])

            # Per-user scores (shared helpers) → candidate inputs (shared resolvers).
            gm_opts = self._genre_match_opts()
            tv_scores = self._per_user_series_scores(series_scores, series_genres, user_aff, user_jit, tv_hh_max, weights, gm_opts)
            mv_scores = self._per_user_movie_scores(movies, user_aff, mv_hh_max, weights, gm_opts)
            tv_items, tv_stats = tv_inputs(eps, tv_inv, tv_watched.get(u["safe_user"], set()),
                                    tv_scores, episode_cap=self._episode_cap(),
                                    franchise_by_series=franchise_by_series, series_timeline=series_timeline,
                                    watch_recency=(episode_recency or {}).get(u["safe_user"], {}))
            series_recency = tv_stats.get("series_recency", {})
            mv_items, _ = movie_inputs(movies, movie_inv, movie_watched.get(u["safe_user"], set()),
                                       mv_scores, universe_order=universe_order,
                                       universe_membership=universe_membership,
                                       watch_recency=(movie_recency or {}).get(u["safe_user"], {}))

            plan, stats = build_combined_plan([tv_items, mv_items], family="up_next",
                                              max_items=self._max_items(),
                                              resume_boost=resume_boost, resume_order=resume_order,
                                              resume_weight=resume_weight,
                                              series_recency=series_recency)   # lift in-progress TV too
            if self.global_cache:
                self.global_cache.set(f"{_PLAN_KEY}/{u['safe_user']}", self._serialize(plan))
            protected.update(t for i in plan.items
                             if (t := rk_to_tmdb.get(str(i.rating_key))) is not None)

            reasons = {**self._tv_reasons(eps, tv_inv, series_genres, user_aff, user_jit),
                       **self._movie_reasons(movies, movie_inv, user_aff)}
            kinds = {**{it.rating_key: "TV" for it in tv_items},
                     **{it.rating_key: "Movie" for it in mv_items}}
            bm = stats.get("by_medium", {})
            self.logger.log_info(
                f"[ComboPlaylists] {who} -> {bm.get('episode', 0)} TV + "
                f"{bm.get('movie', 0)} movie candidate(s), {len(plan.items)} in plan.")
            self._log_preview(u, plan, stats, display, reasons, kinds=kinds, label="item", anon=who)

            # The two MOOD lists (opt-in) sliced from the same candidate pool: The Long Glide =
            # in-progress sagas/franchises/shows (resume-ordered); Touch & Go = the low-commitment
            # standalones + not-started, by affinity. Cached + previewed (write-back is separate).
            if self._mood_lists_enabled():
                glide, g_stats = build_combined_plan(
                    [tv_items, mv_items], family="glide", max_items=self._max_items(),
                    resume_boost=True, resume_order=resume_order,
                    progress_filter="in", series_recency=series_recency)
                touchgo, t_stats = build_combined_plan(
                    [tv_items, mv_items], family="touchgo", max_items=self._max_items(),
                    progress_filter="out", series_recency=series_recency)
                if self.global_cache:
                    self.global_cache.set(f"{_GLIDE_PLAN_KEY}/{u['safe_user']}", self._serialize(glide))
                    self.global_cache.set(f"{_TOUCHGO_PLAN_KEY}/{u['safe_user']}", self._serialize(touchgo))
                for mood in (glide, touchgo):
                    protected.update(t for i in mood.items
                                     if (t := rk_to_tmdb.get(str(i.rating_key))) is not None)
                self._log_preview(u, glide, g_stats, display, reasons, kinds=kinds, label="item",
                                  family_label="The Long Glide", anon=who)
                self._log_preview(u, touchgo, t_stats, display, reasons, kinds=kinds, label="item",
                                  family_label="Touch & Go", anon=who)
            built += 1
        self._publish_protected_movie_tmdbs(_PROTECTED_KEY, protected)
        self.logger.log_info(f"[ComboPlaylists] built {built} per-user combined plan(s) (dry-run — no Plex writes).")
        return {"users": len(tracked), "built": built, "can_build": True}
