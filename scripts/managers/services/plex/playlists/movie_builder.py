"""
plex/playlists/movie_builder.py — per-user MOVIE playlist BUILD + CACHE + dry-run preview.
================================================================================
The movie twin of builder.py. For each tracked Home profile: load owned movies (Radarr
``movie_files.parquet``) + ``plex/movies/owned_inventory`` (tmdb→ratingKey) + per-user
watched movies (Tautulli history) + per-user genre affinity; AGE-GATE by certification;
PERSONALIZE each movie's watchability by the user's affinity (``priority_score`` — NO JIT,
movies have no just-in-time grab); ``build_movie_plan`` (groups by collection/universe,
times by release date); cache the plan + log a dry-run preview. Writes NOTHING to Plex.
Gated behind ``plex.movies.enabled``.

Subclasses PlexPlaylistBuilderManager to REUSE its stable config/affinity/cache/preview
helpers (the TV path is frozen); only the movie-specific run / _build_for_users / watched
/ owned-load are overridden.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.cert_gate import (
    cert_allowed,
    is_restricted,
    tier_level,
)
from scripts.managers.machine_learning.playlists.per_user import genre_match, priority_score
from scripts.managers.machine_learning.playlists.rationale import explain_reason
from scripts.managers.services.plex.playlists.builder import PlexPlaylistBuilderManager
from scripts.managers.services.plex.playlists.movie_resolver import (
    _coll_key,
    build_fresh_movie_plan,
    build_movie_plan,
    watched_movie_keys,
)

_INVENTORY_KEY = "plex/movies/owned_inventory"
_PLAN_KEY = "plex/playlists/movie_plan"          # + /{safe_user}
_FRESH_PLAN_KEY = "plex/playlists/fresh_movie_plan"   # + /{safe_user} (Fresh Arrivals; opt-in)
# Union of movie tmdbIds recommended this run — the space coordinator reads it to SHIELD
# them from the delete pool (never delete a title we are actively recommending).
_PROTECTED_KEY = "plex/playlists/protected_movie_tmdbs/movie"


class MoviePlaylistBuilderManager(PlexPlaylistBuilderManager):
    parent_name = "PlexManager"

    # ── run (I/O gather → tested core) ──────────────────────────────────────────
    def run(self) -> dict:
        tracked = self._tracked_users()
        owned = self._load_owned_movies()
        inventory = self._cache_get(_INVENTORY_KEY, {})
        watched_by_user = {u["safe_user"]: self._watched_movies_for(u.get("tautulli_user_id"))
                           for u in tracked}
        affinity_by_user = {u["safe_user"]: self._user_affinity(u.get("tautulli_username"))
                            for u in tracked}
        return self._build_for_users(tracked, owned, inventory, watched_by_user, affinity_by_user,
                                     csm_ages=self._movie_csm_ages())

    def _movie_csm_ages(self) -> dict:
        """{tmdb_id(int): Common Sense age(int)} from the MDBList movie age cache — the
        cert-gate fallback for owned movies that carry no certification. {} on any failure."""
        try:
            from scripts.managers.services.mdblist import age_cache
            out: dict = {}
            for k, v in (age_cache.load() or {}).items():
                if isinstance(v, int):
                    try:
                        out[int(k)] = v
                    except (TypeError, ValueError):
                        continue
            return out
        except Exception:
            return {}

    # ── Fresh Arrivals knobs (plex.playlists.fresh_arrivals.*) — default OFF ──────
    def _fresh_enabled(self) -> bool:
        """plex.playlists.fresh_arrivals.enabled — default OFF → no fresh plan is built/cached,
        byte-identical to today."""
        return bool((self._pl_cfg().get("fresh_arrivals", {}) or {}).get("enabled", False))

    def _acquired_window_days(self) -> int:
        """plex.playlists.fresh_arrivals.acquired_window_days — how far back counts as 'fresh'."""
        try:
            return int((self._pl_cfg().get("fresh_arrivals", {}) or {}).get("acquired_window_days", 45))
        except (TypeError, ValueError):
            return 45

    def _build_for_users(self, tracked, owned, inventory, watched_by_user, affinity_by_user,
                         csm_ages=None) -> dict:
        """Per user: age-gate by cert + personalize each movie's score by genre affinity
        (no JIT), build+cache the plan, log a preview. Returns run stats. ``csm_ages`` maps
        ``tmdb_id → Common Sense age`` and is the cert-gate fallback for uncertified movies."""
        csm_ages = csm_ages or {}
        if not inventory:
            self.logger.log_warning("[MoviePlaylists] no plex/movies/owned_inventory — "
                                    "enable plex.movies.enabled and run the movie scan.")
            return {"users": len(tracked), "built": 0, "can_build": False}
        if not owned:
            self.logger.log_warning("[MoviePlaylists] no owned movies (Radarr movie_files) — nothing to build.")
            return {"users": len(tracked), "built": 0, "can_build": False}

        profile_ages = self._profile_ages()
        aff_w, hh_w, jit_w = self._priority_weights()      # jit_w unused for movies (is_jit=False)
        hh_max = max((s for m in owned if (s := self._score(m)) is not None), default=0.0) or 1.0
        display = {str(v.get("rating_key")):
                   (f"{v.get('title', '')} ({v.get('year')})" if v.get("year") else (v.get("title", "") or str(v.get("rating_key"))))
                   for v in inventory.values() if v.get("rating_key")}
        rk_to_tmdb = self._inventory_rk_to_tmdb(inventory)   # plan ratingKey -> Radarr tmdbId
        protected: set = set()                               # recommended movie tmdbIds (delete shield)
        built = 0
        for u in tracked:
            watched = watched_by_user.get(u["safe_user"], set())
            user_aff = affinity_by_user.get(u["safe_user"]) or {}

            # AGE GATE (parental controls): a restricted profile keeps only movies whose
            # certification fits its tier; an adult sees everything.
            level = tier_level(u.get("restriction_profile"),
                               profile_ages.get(u.get("title")) or profile_ages.get(u.get("safe_user")))
            user_owned = owned
            if is_restricted(level):
                user_owned = [m for m in owned
                              if cert_allowed(m.get("certification"), level,
                                              csm_age=csm_ages.get(self._coerce_int(m.get("tmdb_id"))))]

            # Cold-start prior from the household's age-appropriate engagement (see builder.py).
            user_aff = self._apply_cold_kids_prior(
                user_aff, level,
                [(self._as_genre_list(m.get("genres")), self._score(m)) for m in user_owned])

            tier_name = self._TIER_NAMES[level] if 0 <= level < len(self._TIER_NAMES) else str(level)
            top_genres = ",".join(g for g, _ in sorted(
                user_aff.items(), key=lambda kv: -kv[1])[:3]) if user_aff else "-"
            n_watched = sum(1 for x in watched if isinstance(x, str))
            gate_note = (f", age-gated {len(user_owned)}/{len(owned)} movie(s)"
                         if is_restricted(level) else "")
            self.logger.log_info(
                f"[MoviePlaylists] '{u.get('title')}' -> tautulli={u.get('tautulli_username') or '-'}, "
                f"affinity={len(user_aff)} genre(s) [{top_genres}], watched={n_watched} movie(s), "
                f"tier={tier_name}{gate_note}")

            # RANK: user-affinity > household (no JIT for movies). household normalised so a
            # household-favourite can't dominate by raw magnitude.
            movie_scores = self._per_user_movie_scores(user_owned, user_aff, hh_max, (aff_w, hh_w, jit_w),
                                                       self._genre_match_opts())

            plan, stats = build_movie_plan(user_owned, inventory, watched, movie_scores,
                                           family="up_next", max_items=self._max_items())
            if self.global_cache:
                self.global_cache.set(f"{_PLAN_KEY}/{u['safe_user']}", self._serialize(plan))
            protected.update(t for i in plan.items
                             if (t := rk_to_tmdb.get(str(i.rating_key))) is not None)
            reasons = self._movie_reasons(user_owned, inventory, user_aff)
            self._log_preview(u, plan, stats, display, reasons, label="movie")

            # Fresh Arrivals (opt-in): a SECOND per-user plan, filtered to genuinely-new
            # acquisitions (churn-immune movie.added) within the window, ranked by the same
            # per-user scores. Cached under its own key; its picks join the delete shield too.
            if self._fresh_enabled():
                fplan, fstats = build_fresh_movie_plan(
                    user_owned, inventory, watched, movie_scores,
                    acquired_window_days=self._acquired_window_days(), max_items=self._max_items())
                if self.global_cache:
                    self.global_cache.set(f"{_FRESH_PLAN_KEY}/{u['safe_user']}", self._serialize(fplan))
                protected.update(t for i in fplan.items
                                 if (t := rk_to_tmdb.get(str(i.rating_key))) is not None)
                self._log_preview(u, fplan, fstats, display, reasons, label="movie",
                                  family_label="Fresh Arrivals")
            built += 1
        self._publish_protected_movie_tmdbs(_PROTECTED_KEY, protected)
        self.logger.log_info(f"[MoviePlaylists] built {built} per-user movie plan(s) (dry-run — no Plex writes).")
        return {"users": len(tracked), "built": built, "can_build": True}

    # ── space-coordinator delete shield ──────────────────────────────────────────
    @staticmethod
    def _inventory_rk_to_tmdb(inventory) -> dict:
        """ratingKey(str) → tmdb_id(int), inverted from the owned-movie inventory (keyed by
        tmdb_id). Lets a built plan's opaque rating_keys be mapped back to the Radarr tmdbIds
        the space-coordinator delete pool keys on."""
        out: dict = {}
        for k, v in (inventory or {}).items():
            rk = v.get("rating_key") if isinstance(v, dict) else None
            if rk is None:
                continue
            try:
                out[str(rk)] = int(k)
            except (TypeError, ValueError):
                continue
        return out

    def _publish_protected_movie_tmdbs(self, key, tmdbs) -> None:
        """Publish (overwrite, per run) the union of movie tmdbIds recommended across every
        user's plan, so the space coordinator can SHIELD them from deletion — never delete a
        title we are actively recommending, most importantly a child's top Up Next pick.
        Best-effort; stored as ``{"tmdbs": [...]}`` so the JSON cache layer never sees a bare list."""
        if not self.global_cache:
            return
        try:
            self.global_cache.set(key, {"tmdbs": sorted({int(t) for t in tmdbs if t is not None})})
        except Exception:
            pass

    # ── movie-specific I/O ──────────────────────────────────────────────────────
    @staticmethod
    def _coerce_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _score(m) -> float | None:
        """A movie's household watchability_score as a FINITE float, or None. The
        movie_files numeric-column round-trip yields float NaN (not None) for an un-scored
        movie; NaN must read as None so it (a) doesn't poison hh_max (max() returns NaN if
        NaN is first) and (b) is correctly dropped/ranked-last rather than floating to a
        constant 0.1 above genuinely low-scored movies."""
        v = m.get("watchability_score")
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v == v else None        # v != v is True only for NaN

    def _per_user_movie_scores(self, user_owned, user_aff, hh_max, weights, gm_opts=None) -> dict:
        """{tmdb: per-user priority_score} (affinity > household; movies have NO JIT).
        ``gm_opts`` (mode/soft_lambda/blend_weight) selects the genre_match shape."""
        aff_w, hh_w, jit_w = weights
        gm_opts = gm_opts or {}
        out: dict = {}
        for m in user_owned or []:
            tmdb = self._coerce_int(m.get("tmdb_id"))
            if tmdb is None:
                continue
            sc = self._score(m)
            gm = genre_match(self._as_genre_list(m.get("genres")), user_aff, **gm_opts)
            if sc is None and gm is None:
                continue
            out[tmdb] = priority_score((sc / hh_max) if sc is not None else 0.0, gm,
                                       is_jit=False, affinity_weight=aff_w, jit_weight=jit_w,
                                       household_weight=hh_w)
        return out

    def _movie_reasons(self, user_owned, inventory, user_aff) -> dict:
        """{ratingKey: 'why'} for the movie preview — genre + collection/universe rationale
        (cast/crew light up once the enrichment daemon populates those columns)."""
        out: dict = {}
        for m in user_owned or []:
            tmdb = self._coerce_int(m.get("tmdb_id"))
            match = (inventory or {}).get(str(tmdb)) if tmdb is not None else None
            rk = str(match["rating_key"]) if (match and match.get("rating_key")) else None
            if rk is None or rk in out:
                continue
            out[rk] = explain_reason(
                self._as_genre_list(m.get("genres")), user_aff,
                cast=self._as_genre_list(m.get("cast_names")),
                crew=self._as_genre_list(m.get("director_names")),
                franchise_name=_coll_key(m.get("collection_name")),
                universe_name=_coll_key(m.get("universe_name")))
        return out

    def _watched_movies_for(self, user_id) -> set:
        if user_id is None or not self.registry:
            return set()
        hm = self.registry.get("manager", "TautulliWatchHistoryManager")
        if hm is None:
            taut = self.registry.get("manager", "TautulliManager")
            hm = getattr(taut, "watch_history", None) if taut else None
        if not hm or not hasattr(hm, "get_all_history_cached"):
            return set()
        try:
            return watched_movie_keys(hm.get_all_history_cached(user_id))
        except Exception:
            return set()

    def _load_owned_movies(self) -> list:
        """Owned (has_file) Radarr movies from every movie_files.parquet, deduped by tmdb.
        READ-only; carries the fields build_movie_plan + the per-user scorer consume."""
        if not (self.global_cache and getattr(self.global_cache, "key_builder", None)):
            return []
        import pandas as pd
        base = self.global_cache.key_builder.base_dir
        want = ["tmdb_id", "title", "year", "watchability_score", "genres", "certification",
                "collection_tmdb_id", "collection_name", "universe_name",
                "in_cinemas_date", "digital_release_date", "physical_release_date",
                "added_at", "has_file"]
        seen: dict = {}
        for path in sorted((base / "radarr").glob("*/movie_files.parquet")):
            try:
                df = pd.read_parquet(path)
            except Exception:
                continue
            cols = [c for c in want if c in df.columns]
            for rec in df[cols].to_dict("records"):
                if rec.get("has_file") is False:
                    continue
                tmdb = self._coerce_int(rec.get("tmdb_id"))
                if tmdb is not None and tmdb not in seen:
                    seen[tmdb] = rec
        return list(seen.values())
