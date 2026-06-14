"""
RadarrCacheRelationalManager
=============================
Rich ML-oriented relational cache storing people/studio relationships
for the Radarr movie library.

Creates and maintains THREE Parquet files per instance:
- people.parquet         — deduplicated person records with aggregated stats
- movie_person_relations.parquet — bipartite graph (movie x person x role)
- studios.parquet        — production company aggregation

Storage
    ``{key_builder.base_dir}/radarr/{instance}/relational/``
    (Snappy-compressed Parquet via pyarrow)
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrCacheRelationalManager(BaseManager, ComponentManagerMixin):

    # ── Schemas ──────────────────────────────────────────────────────────────────

    PEOPLE_SCHEMA = [
        "person_tmdb_id",
        "name",
        "known_for_department",
        "profile_path",
        "popularity",
        "movie_count",
        "director_count",
        "actor_count",
        "producer_count",
        "writer_count",
        "composer_count",
        "cinematographer_count",
        "editor_count",
        "avg_imdb_rating",
        "avg_tmdb_rating",
        "top_genres",
        "top_studios",
        "first_movie_year",
        "last_movie_year",
        "known_movie_titles",
    ]

    RELATIONS_SCHEMA = [
        "movie_id",
        "tmdb_id",
        "title",
        "year",
        "person_name",
        "person_tmdb_id",
        "role_type",
        "character",
        "job",
        "department",
        "billing_order",
        "instance",
    ]

    STUDIOS_SCHEMA = [
        "studio_name",
        "movie_count",
        "avg_imdb_rating",
        "avg_tmdb_rating",
        "top_genres",
        "top_directors",
        "first_movie_year",
        "last_movie_year",
        "known_movie_titles",
        "instance",
    ]

    # ── Init ────────────────────────────────────────────────────────────────────

    def __init__(
        self,
        logger=None,
        config=None,
        global_cache=None,
        validator=None,
        registry=None,
        **kwargs,
    ):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        manager = kwargs.get("manager") or {}
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        self.manager = manager

        _api = kwargs.get("radarr_api") or getattr(manager, "radarr_api", None)
        if _api is not None and not hasattr(_api, "_make_request"):
            _api = None
        if _api is None and self.registry:
            try:
                _radarr_mgr = self.registry.get("manager", "RadarrManager")
                _api = getattr(_radarr_mgr, "radarr_api", None) if _radarr_mgr else None
            except Exception:
                pass
        self.radarr_api = _api

        self.instance_manager = (
            kwargs.get("instance_manager") or getattr(manager, "instance_manager", None)
        )

        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(manager, "dry_run", None)
        self.dry_run = bool(_dry_run) if _dry_run is not None else False

        self.register()
        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    # ── Instance resolution ──────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    # ── Path helpers ─────────────────────────────────────────────────────────────

    def _relational_dir(self, instance: str) -> Path:
        p = (
            self.global_cache.key_builder.base_dir
            / "radarr"
            / instance
            / "relational"
        )
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _people_path(self, instance: str) -> Path:
        return self._relational_dir(instance) / "people.parquet"

    def _relations_path(self, instance: str) -> Path:
        return self._relational_dir(instance) / "movie_person_relations.parquet"

    def _studios_path(self, instance: str) -> Path:
        return self._relational_dir(instance) / "studios.parquet"

    # ── Generic load ────────────────────────────────────────────────────────────

    def _load_parquet(self, path: Path, schema: list[str]) -> pd.DataFrame:
        if path.exists():
            try:
                return pd.read_parquet(path)
            except Exception as e:
                self.logger.log_warning(f"Could not read {path.name}: {e}")
        return pd.DataFrame(columns=schema)

    def _save_parquet(self, df: pd.DataFrame, path: Path) -> bool:
        try:
            df.to_parquet(path, index=False, engine="pyarrow", compression="snappy")
            self.logger.log_info(f"Saved {path.name}: {len(df)} rows")
            return True
        except Exception as e:
            self.logger.log_warning(f"Failed to save {path.name}: {e}")
            return False

    # ── Public load accessors ───────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_people")
    def get_people(self, instance: str) -> pd.DataFrame:
        instance = self._resolve_instance(instance)
        return self._load_parquet(self._people_path(instance), self.PEOPLE_SCHEMA)

    @LoggerManager().log_function_entry
    @timeit("get_relations")
    def get_relations(self, instance: str) -> pd.DataFrame:
        instance = self._resolve_instance(instance)
        return self._load_parquet(self._relations_path(instance), self.RELATIONS_SCHEMA)

    @LoggerManager().log_function_entry
    @timeit("get_studios")
    def get_studios(self, instance: str) -> pd.DataFrame:
        instance = self._resolve_instance(instance)
        return self._load_parquet(self._studios_path(instance), self.STUDIOS_SCHEMA)

    # ── Core builder from raw movie list ────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("build_relations_from_movies")
    def build_relations_from_movies(self, movies: list[dict], instance: str) -> dict:
        """
        Parse credits.cast and credits.crew from each movie, build all 3 tables.
        Returns stats dict.
        """
        instance = self._resolve_instance(instance)
        stats = {"movies": 0, "relation_rows": 0, "people": 0, "studios": 0}

        relation_rows: list[dict] = []
        # person_name -> aggregated data
        person_agg: dict[str, dict] = defaultdict(lambda: {
            "person_tmdb_id": None,
            "known_for_department": None,
            "profile_path": None,
            "popularity": None,
            "movies": [],    # list of (title, year, imdb_rating, tmdb_rating, genres, studio)
            "director_count": 0,
            "actor_count": 0,
            "producer_count": 0,
            "writer_count": 0,
            "composer_count": 0,
            "cinematographer_count": 0,
            "editor_count": 0,
        })
        # studio_name -> aggregated data
        studio_agg: dict[str, dict] = defaultdict(lambda: {
            "movies": [],  # list of (title, year, imdb_rating, tmdb_rating, genres, directors)
        })

        credits_available = any(m.get("credits") for m in movies[:5] if m)
        if not credits_available and movies:
            self.logger.log_debug(
                "[Relational] No credits data in movie payload — Radarr's bulk /movie "
                "endpoint does not include cast/crew. People and relations tables will be empty."
            )

        for movie in movies:
            stats["movies"] += 1
            movie_id = movie.get("id")
            tmdb_id  = movie.get("tmdbId")
            title    = movie.get("title", "")
            year     = movie.get("year")
            genres   = movie.get("genres") or []

            ratings  = movie.get("ratings") or {}
            imdb_r   = (ratings.get("imdb") or {}).get("value")
            tmdb_r   = (ratings.get("tmdb") or {}).get("value")

            studio   = movie.get("studio") or ""
            prod_cos = [c.get("name") for c in (movie.get("productionCompanies") or []) if c.get("name")]
            all_studios = ([studio] if studio else []) + prod_cos

            credits  = movie.get("credits") or {}
            cast_raw = credits.get("cast") or []
            crew_raw = credits.get("crew") or []

            # Collect directors for studio aggregation
            directors_for_movie: list[str] = []

            # ── Cast ────────────────────────────────────────────────────────────
            sorted_cast = sorted(
                [c for c in cast_raw if c.get("name")],
                key=lambda c: c.get("order", 9999)
            )[:10]

            for member in sorted_cast:
                name      = (member.get("name") or "").strip()
                char      = member.get("character", "")
                order     = member.get("order")
                p_tmdb_id = member.get("id")

                if not name:
                    continue

                relation_rows.append({
                    "movie_id":      movie_id,
                    "tmdb_id":       tmdb_id,
                    "title":         title,
                    "year":          year,
                    "person_name":   name,
                    "person_tmdb_id": p_tmdb_id,
                    "role_type":     "actor",
                    "character":     char,
                    "job":           None,
                    "department":    "Acting",
                    "billing_order": order,
                    "instance":      instance,
                })

                agg = person_agg[name]
                if agg["person_tmdb_id"] is None:
                    agg["person_tmdb_id"] = p_tmdb_id
                if agg["known_for_department"] is None:
                    agg["known_for_department"] = "Acting"
                agg["actor_count"] += 1
                agg["movies"].append((title, year, imdb_r, tmdb_r, genres, studio))

            # ── Crew ────────────────────────────────────────────────────────────
            for member in crew_raw:
                name      = (member.get("name") or "").strip()
                job       = (member.get("job") or "").strip()
                dept      = (member.get("department") or "").strip()
                p_tmdb_id = member.get("id")

                if not name:
                    continue

                role_type = None
                if job == "Director":
                    role_type = "director"
                    directors_for_movie.append(name)
                elif "producer" in job.lower() and dept.lower() == "production":
                    role_type = "producer"
                elif dept.lower() == "writing" or job in ("Screenplay", "Story", "Writer"):
                    role_type = "writer"
                elif job == "Original Music Composer":
                    role_type = "composer"
                elif job == "Director of Photography":
                    role_type = "cinematographer"
                elif job == "Editor":
                    role_type = "editor"

                if role_type is None:
                    continue

                relation_rows.append({
                    "movie_id":      movie_id,
                    "tmdb_id":       tmdb_id,
                    "title":         title,
                    "year":          year,
                    "person_name":   name,
                    "person_tmdb_id": p_tmdb_id,
                    "role_type":     role_type,
                    "character":     None,
                    "job":           job,
                    "department":    dept,
                    "billing_order": None,
                    "instance":      instance,
                })

                agg = person_agg[name]
                if agg["person_tmdb_id"] is None:
                    agg["person_tmdb_id"] = p_tmdb_id
                count_key = f"{role_type}_count"
                if count_key in agg:
                    agg[count_key] += 1
                agg["movies"].append((title, year, imdb_r, tmdb_r, genres, studio))

            # ── Studio aggregation ───────────────────────────────────────────────
            for s_name in all_studios:
                studio_agg[s_name]["movies"].append(
                    (title, year, imdb_r, tmdb_r, genres, directors_for_movie)
                )

        stats["relation_rows"] = len(relation_rows)

        # ── Build people DataFrame ───────────────────────────────────────────────
        people_rows: list[dict] = []
        for name, agg in person_agg.items():
            movie_list = agg["movies"]
            imdb_ratings  = [m[2] for m in movie_list if m[2] is not None]
            tmdb_ratings  = [m[3] for m in movie_list if m[3] is not None]
            all_genres    = [g for m in movie_list for g in (m[4] or [])]
            all_studios_p = [m[5] for m in movie_list if m[5]]
            years         = [m[1] for m in movie_list if m[1]]
            titles        = [m[0] for m in movie_list if m[0]]

            top_genres  = "|".join(g for g, _ in Counter(all_genres).most_common(5))
            top_studios = "|".join(s for s, _ in Counter(all_studios_p).most_common(3))
            known_titles = json.dumps(list(dict.fromkeys(reversed(titles)))[:20])

            people_rows.append({
                "person_tmdb_id":       agg["person_tmdb_id"],
                "name":                 name,
                "known_for_department": agg["known_for_department"],
                "profile_path":         agg["profile_path"],
                "popularity":           agg["popularity"],
                "movie_count":          len(set(titles)),
                "director_count":       agg["director_count"],
                "actor_count":          agg["actor_count"],
                "producer_count":       agg["producer_count"],
                "writer_count":         agg["writer_count"],
                "composer_count":       agg["composer_count"],
                "cinematographer_count": agg["cinematographer_count"],
                "editor_count":         agg["editor_count"],
                "avg_imdb_rating":      sum(imdb_ratings) / len(imdb_ratings) if imdb_ratings else None,
                "avg_tmdb_rating":      sum(tmdb_ratings) / len(tmdb_ratings) if tmdb_ratings else None,
                "top_genres":           top_genres or None,
                "top_studios":          top_studios or None,
                "first_movie_year":     min(years) if years else None,
                "last_movie_year":      max(years) if years else None,
                "known_movie_titles":   known_titles,
            })
        stats["people"] = len(people_rows)

        # ── Build studios DataFrame ──────────────────────────────────────────────
        studios_rows: list[dict] = []
        for s_name, sagg in studio_agg.items():
            movie_list = sagg["movies"]
            imdb_ratings = [m[2] for m in movie_list if m[2] is not None]
            tmdb_ratings = [m[3] for m in movie_list if m[3] is not None]
            all_genres   = [g for m in movie_list for g in (m[4] or [])]
            all_dirs     = [d for m in movie_list for d in (m[5] or [])]
            years        = [m[1] for m in movie_list if m[1]]
            titles       = [m[0] for m in movie_list if m[0]]

            top_genres  = "|".join(g for g, _ in Counter(all_genres).most_common(5))
            top_dirs    = "|".join(d for d, _ in Counter(all_dirs).most_common(5))
            known_titles = json.dumps(list(dict.fromkeys(reversed(titles)))[:20])

            studios_rows.append({
                "studio_name":     s_name,
                "movie_count":     len(set(titles)),
                "avg_imdb_rating": sum(imdb_ratings) / len(imdb_ratings) if imdb_ratings else None,
                "avg_tmdb_rating": sum(tmdb_ratings) / len(tmdb_ratings) if tmdb_ratings else None,
                "top_genres":      top_genres or None,
                "top_directors":   top_dirs or None,
                "first_movie_year": min(years) if years else None,
                "last_movie_year": max(years) if years else None,
                "known_movie_titles": known_titles,
                "instance":        instance,
            })
        stats["studios"] = len(studios_rows)

        # ── Save all three tables ────────────────────────────────────────────────
        if not self.dry_run:
            if relation_rows:
                df_rel = pd.DataFrame(relation_rows, columns=self.RELATIONS_SCHEMA)
                self._save_parquet(df_rel, self._relations_path(instance))

            if people_rows:
                df_ppl = pd.DataFrame(people_rows, columns=self.PEOPLE_SCHEMA)
                self._save_parquet(df_ppl, self._people_path(instance))

            if studios_rows:
                df_stu = pd.DataFrame(studios_rows, columns=self.STUDIOS_SCHEMA)
                self._save_parquet(df_stu, self._studios_path(instance))
        else:
            self.logger.log_info(
                f"[dry_run] Would save relational tables for '{instance}': "
                f"{len(relation_rows)} relations, {len(people_rows)} people, "
                f"{len(studios_rows)} studios"
            )

        return stats

    # ── Build from movie_files DataFrame ────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("build_from_movie_files_df")
    def build_from_movie_files_df(self, df: pd.DataFrame, instance: str) -> dict:
        """
        Take the movie_files DataFrame (already built by RadarrCacheMovieFilesManager)
        and extract people/relations/studios from the people/studio columns.
        """
        instance = self._resolve_instance(instance)
        stats = {"relation_rows": 0, "people": 0, "studios": 0}

        if df.empty:
            return stats

        relation_rows: list[dict] = []
        person_agg: dict[str, dict] = defaultdict(lambda: {
            "person_tmdb_id": None,
            "known_for_department": None,
            "profile_path": None,
            "popularity": None,
            "movies": [],
            "director_count": 0,
            "actor_count": 0,
            "producer_count": 0,
            "writer_count": 0,
            "composer_count": 0,
            "cinematographer_count": 0,
            "editor_count": 0,
        })
        studio_agg: dict[str, dict] = defaultdict(lambda: {"movies": []})

        role_col_map = {
            "director_names":        "director",
            "cast_names":            "actor",
            "producer_names":        "producer",
            "writer_names":          "writer",
            "composer_names":        "composer",
            "cinematographer_names": "cinematographer",
            "editor_names":          "editor",
        }

        for _, row in df.iterrows():
            movie_id = row.get("movie_id")
            tmdb_id  = row.get("tmdb_id")
            title    = row.get("title", "")
            year     = row.get("year")
            imdb_r   = row.get("imdb_rating")
            tmdb_r   = row.get("tmdb_rating")
            studio   = row.get("studio") or ""
            genres_raw = row.get("genres")
            genres = []
            if genres_raw:
                try:
                    genres = json.loads(genres_raw)
                except Exception:
                    genres = []

            cast_chars = (row.get("cast_characters") or "").split("|")
            cast_orders = (row.get("cast_order") or "").split("|")
            directors_for_row: list[str] = []

            for col, role_type in role_col_map.items():
                names_raw = row.get(col) or ""
                if not names_raw:
                    continue
                names = [n.strip() for n in names_raw.split("|") if n.strip()]

                for i, name in enumerate(names):
                    char  = cast_chars[i] if role_type == "actor" and i < len(cast_chars) else None
                    order = None
                    if role_type == "actor" and i < len(cast_orders):
                        try:
                            order = int(cast_orders[i])
                        except (ValueError, TypeError):
                            pass

                    if role_type == "director":
                        directors_for_row.append(name)

                    relation_rows.append({
                        "movie_id":      movie_id,
                        "tmdb_id":       tmdb_id,
                        "title":         title,
                        "year":          year,
                        "person_name":   name,
                        "person_tmdb_id": None,
                        "role_type":     role_type,
                        "character":     char,
                        "job":           None,
                        "department":    None,
                        "billing_order": order,
                        "instance":      instance,
                    })

                    agg = person_agg[name]
                    count_key = f"{role_type}_count"
                    if count_key in agg:
                        agg[count_key] += 1
                    if role_type == "actor" and agg["known_for_department"] is None:
                        agg["known_for_department"] = "Acting"
                    elif role_type == "director" and agg["known_for_department"] is None:
                        agg["known_for_department"] = "Directing"
                    agg["movies"].append((title, year, imdb_r, tmdb_r, genres, studio))

            # Studio aggregation
            for s_name in ([studio] if studio else []):
                studio_agg[s_name]["movies"].append(
                    (title, year, imdb_r, tmdb_r, genres, directors_for_row)
                )

        stats["relation_rows"] = len(relation_rows)

        # People
        people_rows: list[dict] = []
        for name, agg in person_agg.items():
            movie_list   = agg["movies"]
            imdb_ratings = [m[2] for m in movie_list if m[2] is not None]
            tmdb_ratings = [m[3] for m in movie_list if m[3] is not None]
            all_genres   = [g for m in movie_list for g in (m[4] or [])]
            all_studs    = [m[5] for m in movie_list if m[5]]
            years        = [m[1] for m in movie_list if m[1]]
            titles       = [m[0] for m in movie_list if m[0]]
            top_genres   = "|".join(g for g, _ in Counter(all_genres).most_common(5))
            top_studios  = "|".join(s for s, _ in Counter(all_studs).most_common(3))
            known_titles = json.dumps(list(dict.fromkeys(reversed(titles)))[:20])
            people_rows.append({
                "person_tmdb_id":        agg["person_tmdb_id"],
                "name":                  name,
                "known_for_department":  agg["known_for_department"],
                "profile_path":          None,
                "popularity":            None,
                "movie_count":           len(set(titles)),
                "director_count":        agg["director_count"],
                "actor_count":           agg["actor_count"],
                "producer_count":        agg["producer_count"],
                "writer_count":          agg["writer_count"],
                "composer_count":        agg["composer_count"],
                "cinematographer_count": agg["cinematographer_count"],
                "editor_count":          agg["editor_count"],
                "avg_imdb_rating":       sum(imdb_ratings) / len(imdb_ratings) if imdb_ratings else None,
                "avg_tmdb_rating":       sum(tmdb_ratings) / len(tmdb_ratings) if tmdb_ratings else None,
                "top_genres":            top_genres or None,
                "top_studios":           top_studios or None,
                "first_movie_year":      min(years) if years else None,
                "last_movie_year":       max(years) if years else None,
                "known_movie_titles":    known_titles,
            })
        stats["people"] = len(people_rows)

        # Studios
        studios_rows: list[dict] = []
        for s_name, sagg in studio_agg.items():
            movie_list   = sagg["movies"]
            imdb_ratings = [m[2] for m in movie_list if m[2] is not None]
            tmdb_ratings = [m[3] for m in movie_list if m[3] is not None]
            all_genres   = [g for m in movie_list for g in (m[4] or [])]
            all_dirs     = [d for m in movie_list for d in (m[5] or [])]
            years        = [m[1] for m in movie_list if m[1]]
            titles       = [m[0] for m in movie_list if m[0]]
            top_genres   = "|".join(g for g, _ in Counter(all_genres).most_common(5))
            top_dirs     = "|".join(d for d, _ in Counter(all_dirs).most_common(5))
            known_titles = json.dumps(list(dict.fromkeys(reversed(titles)))[:20])
            studios_rows.append({
                "studio_name":      s_name,
                "movie_count":      len(set(titles)),
                "avg_imdb_rating":  sum(imdb_ratings) / len(imdb_ratings) if imdb_ratings else None,
                "avg_tmdb_rating":  sum(tmdb_ratings) / len(tmdb_ratings) if tmdb_ratings else None,
                "top_genres":       top_genres or None,
                "top_directors":    top_dirs or None,
                "first_movie_year": min(years) if years else None,
                "last_movie_year":  max(years) if years else None,
                "known_movie_titles": known_titles,
                "instance":         instance,
            })
        stats["studios"] = len(studios_rows)

        if not self.dry_run:
            if relation_rows:
                self._save_parquet(
                    pd.DataFrame(relation_rows, columns=self.RELATIONS_SCHEMA),
                    self._relations_path(instance),
                )
            if people_rows:
                self._save_parquet(
                    pd.DataFrame(people_rows, columns=self.PEOPLE_SCHEMA),
                    self._people_path(instance),
                )
            if studios_rows:
                self._save_parquet(
                    pd.DataFrame(studios_rows, columns=self.STUDIOS_SCHEMA),
                    self._studios_path(instance),
                )
        return stats

    # ── Public run ───────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_relational")
    def run(self, instance: str) -> dict:
        """Called with raw movie list from API; builds all 3 tables."""
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            self.logger.log_warning("radarr_api not available — cannot build relational tables")
            return {"movies": 0, "relation_rows": 0, "people": 0, "studios": 0}

        # Prefer global_cache (populated by run_movie_data_pull) to avoid a
        # third fetch of the full 20k movie list in the same pipeline run.
        movies: list[dict] = []
        if self.global_cache:
            movies = self.global_cache.get(f"radarr.movies.{instance}.full") or []
        if not movies:
            movies = self.radarr_api._make_request(instance, "movie", fallback=[]) or []
        return self.build_relations_from_movies(movies, instance)

    # ── Query helpers ────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_collaborators")
    def get_collaborators(self, name: str, instance: str) -> pd.DataFrame:
        """Return DataFrame of people who worked with the given person across movies."""
        instance = self._resolve_instance(instance)
        df_rel = self.get_relations(instance)
        if df_rel.empty or "person_name" not in df_rel.columns:
            return pd.DataFrame()

        # Find movies this person was in
        person_movies = set(df_rel.loc[df_rel["person_name"] == name, "movie_id"].dropna())
        if not person_movies:
            return pd.DataFrame()

        # All other people in those movies
        collab_mask = (df_rel["movie_id"].isin(person_movies)) & (df_rel["person_name"] != name)
        return df_rel[collab_mask].reset_index(drop=True)

    @LoggerManager().log_function_entry
    @timeit("get_person_filmography")
    def get_person_filmography(self, name: str, instance: str) -> pd.DataFrame:
        """Return DataFrame of all movies a person appeared in."""
        instance = self._resolve_instance(instance)
        df_rel = self.get_relations(instance)
        if df_rel.empty or "person_name" not in df_rel.columns:
            return pd.DataFrame()
        return df_rel[df_rel["person_name"] == name].reset_index(drop=True)

    @LoggerManager().log_function_entry
    @timeit("get_studio_movies")
    def get_studio_movies(self, studio_name: str, instance: str) -> pd.DataFrame:
        """Return DataFrame of all movies from a studio."""
        instance = self._resolve_instance(instance)
        df_stu = self.get_studios(instance)
        if df_stu.empty or "studio_name" not in df_stu.columns:
            return pd.DataFrame()
        return df_stu[df_stu["studio_name"] == studio_name].reset_index(drop=True)
