from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktRatingsManager(BaseManager, ComponentManagerMixin):
    parent_name = "TraktManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent         = kwargs.get("manager")
        self.trakt_api = kwargs.get("trakt_api")

        # Resolve dry_run — walk the chain: kwargs → parent → TraktManager → Main.
        # Never default to False; raise if unresolvable.
        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(parent, "dry_run", None) if parent else None
        if _dry_run is None and self.registry:
            try:
                _trakt = self.registry.get("manager", "TraktManager")
                _dry_run = getattr(_trakt, "dry_run", None) if _trakt else None
            except Exception:
                pass
        if _dry_run is None and self.registry:
            try:
                _main = self.registry.get("manager", "Main")
                _dry_run = getattr(_main, "dry_run", None) if _main else None
            except Exception:
                pass
        if _dry_run is None:
            raise ValueError(
                f"❌ {self.__class__.__name__} could not resolve dry_run from kwargs, "
                f"TraktManager, or Main. Refusing to initialize without an explicit value "
                f"from config.json to prevent accidental destructive operations."
            )
        self.dry_run = bool(_dry_run)

        trakt_cfg = (self.config.get("trakt", {}) if self.config else {})
        self.user = trakt_cfg.get("username", "default")

    # ── Read ratings ──────────────────────────────────────────────────────

    def get_rated_shows(self) -> list:
        return self.global_cache.get_or_generate_cache(
            key=f"trakt/{self.user}/ratings/shows",
            generator_function=lambda: self.trakt_api._make_request("sync/ratings/shows"),
        ) if self.global_cache else self._fetch_ratings("shows")

    def get_rated_episodes(self) -> list:
        return self.global_cache.get_or_generate_cache(
            key=f"trakt/{self.user}/ratings/episodes",
            generator_function=lambda: self.trakt_api._make_request("sync/ratings/episodes"),
        ) if self.global_cache else self._fetch_ratings("episodes")

    def get_rated_movies(self) -> list:
        return self.global_cache.get_or_generate_cache(
            key=f"trakt/{self.user}/ratings/movies",
            generator_function=lambda: self.trakt_api._make_request("sync/ratings/movies"),
        ) if self.global_cache else self._fetch_ratings("movies")

    @timeit("get_user_ratings")
    def get_user_ratings(self) -> list:
        """Fetch all ratings across shows, episodes, and movies."""
        all_ratings: list = []
        self.logger.log_info("[TraktRatings] Fetching all user ratings...")
        for media_type in ("shows", "episodes", "movies"):
            items = (self.trakt_api._make_request(f"sync/ratings/{media_type}") or []) if self.trakt_api else []
            all_ratings.extend(items)
            self.logger.log_info(f"[TraktRatings] {len(items)} {media_type} ratings.")

        self.logger.log_info(f"[TraktRatings] Total: {len(all_ratings)} ratings.")
        return all_ratings

    # ── Auto-rating ───────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("auto_rate_watched_shows")
    def auto_rate_watched_shows(
        self,
        threshold:     float = 0.6,
        rating:        int   = 7,
        min_completed: int   = 3,
        progress_map:  dict  | None = None,
    ) -> dict:
        """
        Submit a Trakt rating for every show where the user has watched
        >= `threshold` of aired episodes and has no existing rating.

        Parameters
        ----------
        threshold:     Minimum completion fraction (default 0.6 = 60 %).
        rating:        Trakt integer 1-10 to assign (default 7 ≈ 3.5 / 5).
        min_completed: Minimum watched episodes required (guards against
                       single-episode pilots skewing the ratio to 100 %).
        progress_map:  Pre-fetched {slug: progress_dict} from
                       TraktProgressManager.get_combined_progress_watched().
                       Pass this in from TraktManager.run() to avoid a
                       redundant 100+ API-call fetch.

        Returns
        -------
        dict with keys: rated, skipped_already_rated, skipped_insufficient, errors
        """
        if not self.trakt_api:
            return {}

        # ── 1. Show IDs from watched list ─────────────────────────────────
        # sync/watched/shows gives us the full ID block (trakt, slug, tvdb, tmdb…)
        # needed to build the POST /sync/ratings payload.
        watched_shows = self.trakt_api._make_request("sync/watched/shows") or []
        id_by_slug: dict = {
            ids["slug"]: ids
            for item in watched_shows
            if (ids := ((item.get("show") or {}).get("ids") or {})).get("slug")
        }

        # ── 2. Progress data (completed / aired per show) ─────────────────
        if progress_map is None:
            prog_mgr = getattr(self.trakt_api, "progress", None)
            progress_map = prog_mgr.get_combined_progress_watched() if prog_mgr else {}

        # ── 3. Already-rated shows (don't overwrite manual ratings) ───────
        existing       = self.get_rated_shows() or []
        already_rated: set = {
            slug
            for r in existing
            if (slug := ((r.get("show") or {}).get("ids") or {}).get("slug"))
        }

        # ── 4. Build the batch ────────────────────────────────────────────
        to_rate: list         = []
        skipped_rated:        int = 0
        skipped_insufficient: int = 0

        for slug, progress in progress_map.items():
            if not progress:
                skipped_insufficient += 1
                continue

            aired     = progress.get("aired",     0) or 0
            completed = progress.get("completed", 0) or 0

            # Guard: need enough episodes and a high enough completion ratio
            if not aired or completed < min_completed or (completed / aired) < threshold:
                skipped_insufficient += 1
                continue

            if slug in already_rated:
                skipped_rated += 1
                continue

            ids = id_by_slug.get(slug)
            if not ids:
                skipped_insufficient += 1
                continue

            to_rate.append({"rating": rating, "ids": ids})

        # ── 5. Log & submit ───────────────────────────────────────────────
        self.logger.log_info(
            f"[TraktRatings] auto_rate: {len(to_rate)} to rate, "
            f"{skipped_rated} already rated, "
            f"{skipped_insufficient} below threshold / insufficient data"
        )

        if not to_rate:
            return {
                "rated":                  0,
                "skipped_already_rated":  skipped_rated,
                "skipped_insufficient":   skipped_insufficient,
                "errors":                 0,
            }

        errors = 0
        if self.dry_run:
            self.logger.log_info(
                f"[TraktRatings] dry_run — would POST {len(to_rate)} show ratings ({rating}/10):"
            )
            for entry in to_rate:
                slug = entry["ids"].get("slug", "?")
                self.logger.log_debug(f"  → {slug} ({rating}/10)")
        else:
            resp = self.trakt_api._make_request(
                "sync/ratings",
                method="POST",
                data={"shows": to_rate},
            )
            if resp is None:
                errors = len(to_rate)
                self.logger.log_warning("[TraktRatings] auto_rate: POST /sync/ratings failed.")
            else:
                added   = (resp.get("added")    or {}).get("shows", 0)
                skipped = (resp.get("existing") or {}).get("shows", 0)
                self.logger.log_info(
                    f"[TraktRatings] auto_rate: Trakt confirmed "
                    f"added={added}, existing={skipped}, "
                    f"not_found={len((resp.get('not_found') or {}).get('shows', []))}"
                )
            # Invalidate the cached show ratings so the next get_rated_shows() is fresh
            if self.global_cache:
                self.global_cache.invalidate_cache_key(f"trakt/{self.user}/ratings/shows")

        return {
            "rated":                  len(to_rate) if not errors else 0,
            "skipped_already_rated":  skipped_rated,
            "skipped_insufficient":   skipped_insufficient,
            "errors":                 errors,
        }

    # ── Auto-rating movies ────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("auto_rate_watched_movies")
    def auto_rate_watched_movies(
        self,
        movies: list[dict],
        completion_map: dict,
        watched_tmdb_ids: set[int],
        genre_affinity: dict,
        people_manager=None,
    ) -> dict:
        """
        Submit Trakt ratings for every movie the household has watched,
        scored by the movie scoring matrix in trakt/movies/scorer.py.

        Parameters
        ----------
        movies:
            Full Radarr movie list (used to build the collection index and to
            look up movie metadata such as genres / collection membership).
        completion_map:
            ``{tmdb_id (int): {"pct": float, "threshold": float}}`` from
            ``tautulli/group/<group>/tmdb_completions`` cache.
        watched_tmdb_ids:
            All tmdbIds the household has watched (Trakt + Tautulli history).
            Used for the collection-bonus calculation.
        genre_affinity:
            Household affinity dict from ``tautulli/affinity`` cache.
        people_manager:
            Optional ``TraktMoviePeopleManager`` instance used to fetch Trakt
            cast/crew for the director and actor affinity bonuses.

        Returns
        -------
        dict with keys: rated, skipped_already_rated, skipped_no_data, errors
        """
        from scripts.managers.services.trakt.movies.scorer import score_movie

        if not self.trakt_api:
            return {}

        # ── 1. Build collection index from Radarr library ─────────────────
        # {collection_tmdb_id: set of all movie tmdbIds in that collection}
        collection_members: dict[int, set[int]] = {}
        for m in movies:
            coll    = m.get("collection") or {}
            coll_id = coll.get("tmdbId")
            mid     = m.get("tmdbId")
            if coll_id and mid:
                collection_members.setdefault(int(coll_id), set()).add(int(mid))

        # ── 2. Build tmdbId → movie dict for quick lookup ─────────────────
        movie_by_tmdb: dict[int, dict] = {
            int(m["tmdbId"]): m for m in movies if m.get("tmdbId")
        }

        # ── 3. Already-rated movies — don't overwrite manual ratings ──────
        existing_ratings = self.get_rated_movies() or []
        already_rated: set[int] = {
            int(tmdb)
            for r in existing_ratings
            if (tmdb := ((r.get("movie") or {}).get("ids") or {}).get("tmdb"))
        }

        # ── 4. Score each watched movie ───────────────────────────────────
        to_rate:           list[dict] = []
        skipped_rated:     int = 0
        skipped_no_data:   int = 0

        for tmdb_id, completion_data in completion_map.items():
            tmdb_id = int(tmdb_id)

            if tmdb_id in already_rated:
                skipped_rated += 1
                continue

            movie = movie_by_tmdb.get(tmdb_id)
            if not movie:
                skipped_no_data += 1
                continue

            pct       = float(completion_data.get("pct", 0.0))
            threshold = float(completion_data.get("threshold", 0.9))

            # Require at least some engagement (< 50 % is an early walk-out)
            if pct < 0.25:
                skipped_no_data += 1
                continue

            # Fetch Trakt credits for director / actor affinity
            credits = None
            if people_manager:
                try:
                    credits = people_manager.get_people(tmdb_id)
                except Exception:
                    pass

            rating = score_movie(
                movie=movie,
                completion_pct=pct,
                completion_threshold=threshold,
                collection_members=collection_members,
                watched_tmdb_ids=watched_tmdb_ids,
                genre_affinity=genre_affinity,
                credits=credits,
            )

            ids: dict = {"tmdb": tmdb_id}
            if movie.get("imdbId"):
                ids["imdb"] = movie["imdbId"]

            to_rate.append({
                "rating": rating,
                "ids":    ids,
                "title":  movie.get("title", str(tmdb_id)),
            })

        # ── 5. Log ────────────────────────────────────────────────────────
        self.logger.log_info(
            f"[TraktRatings] auto_rate_movies: {len(to_rate)} to rate, "
            f"{skipped_rated} already rated, "
            f"{skipped_no_data} skipped (not in Radarr / below engagement floor)"
        )

        if not to_rate:
            return {
                "rated":                 0,
                "skipped_already_rated": skipped_rated,
                "skipped_no_data":       skipped_no_data,
                "errors":                0,
            }

        # ── 6. Submit ─────────────────────────────────────────────────────
        errors = 0
        if self.dry_run:
            self.logger.log_info(
                f"[TraktRatings] dry_run — would POST {len(to_rate)} movie ratings:"
            )
            for entry in to_rate:
                self.logger.log_debug(
                    f"  → {entry['title']} ({entry['rating']}/10, "
                    f"tmdb={entry['ids'].get('tmdb')})"
                )
        else:
            payload = [{"rating": e["rating"], "ids": e["ids"]} for e in to_rate]
            resp = self.trakt_api._make_request(
                "sync/ratings",
                method="POST",
                data={"movies": payload},
            )
            if resp is None:
                errors = len(to_rate)
                self.logger.log_warning(
                    "[TraktRatings] auto_rate_movies: POST /sync/ratings failed."
                )
            else:
                added   = (resp.get("added")    or {}).get("movies", 0)
                skipped = (resp.get("existing") or {}).get("movies", 0)
                not_found = len((resp.get("not_found") or {}).get("movies", []))
                self.logger.log_info(
                    f"[TraktRatings] auto_rate_movies: Trakt confirmed "
                    f"added={added}, existing={skipped}, not_found={not_found}"
                )
            # Invalidate cached movie ratings so next fetch is fresh
            if self.global_cache:
                self.global_cache.invalidate_cache_key(
                    f"trakt/{self.user}/ratings/movies"
                )

        return {
            "rated":                 len(to_rate) if not errors else 0,
            "skipped_already_rated": skipped_rated,
            "skipped_no_data":       skipped_no_data,
            "errors":                errors,
        }

    # ── Private ───────────────────────────────────────────────────────────

    def _fetch_ratings(self, media_type: str) -> list:
        if not self.trakt_api:
            return []
        return self.trakt_api._make_request(f"sync/ratings/{media_type}") or []
