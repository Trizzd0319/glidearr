import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrMovieDataframeBuilderManager(BaseManager, ComponentManagerMixin):
    """
    Transforms enriched Radarr movie records into structured pandas DataFrames
    for ML pipelines, auditing, and analytics.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrMoviesManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    @LoggerManager().log_function_entry
    @timeit("build_movie_dataframe")
    def build_movie_dataframe(self, instance: str) -> "pd.DataFrame":
        """
        Load enriched movies from cache (or build them) and return a flat DataFrame.
        The DataFrame is also stored in global_cache under radarr.movies.{instance}.dataframe.
        """
        resolved = self._resolve_instance(instance)
        cached = self.global_cache.get(f"radarr.movies.{resolved}.dataframe", default=None)
        if cached is not None and isinstance(cached, pd.DataFrame) and not cached.empty:
            return cached

        # Fall back to enriched list in cache
        enriched = self.global_cache.get(f"radarr.movies.{resolved}.enriched", default=None)
        if not enriched:
            # Build from raw movies
            movies = self.radarr_api._make_request(resolved, "movie", fallback=[]) or []
            enriched = movies  # use raw if enriched not yet available

        df = self.build_dataframe(enriched)
        self.global_cache.set(f"radarr.movies.{resolved}.dataframe", df)
        self.logger.log_info(f"Built movie DataFrame with {len(df)} rows for {resolved}")
        return df

    @LoggerManager().log_function_entry
    @timeit("build_dataframe")
    def build_dataframe(self, enriched_movies: list) -> "pd.DataFrame":
        """Convert a list of enriched movie dicts into a pandas DataFrame."""
        flat_records = [self._flatten_movie(m) for m in enriched_movies]
        df = pd.DataFrame(flat_records)
        self.logger.log_info(f"Generated movie dataframe with {len(df)} rows")
        return df

    def _flatten_movie(self, movie: dict) -> dict:
        """Flatten nested movie metadata to top-level keys for dataframe use."""
        people = movie.get("people", {})
        ratings = movie.get("ratings", {})

        return {
            "id":               movie.get("id"),
            "title":            movie.get("title"),
            "year":             movie.get("year"),
            "runtime":          movie.get("runtime"),
            "tmdb_id":          movie.get("tmdbId") or movie.get("tmdb_id"),
            "imdb_id":          movie.get("imdbId") or movie.get("imdb_id"),
            "genres":           ", ".join(movie.get("genres", [])),
            "keywords":         ", ".join(k for k in movie.get("keywords", []) if isinstance(k, str)),
            "studio":           movie.get("studio"),
            "collection":       (movie.get("collection") or {}).get("title"),
            "actors":           ", ".join(people.get("actors", [])),
            "directors":        ", ".join(people.get("directors", [])),
            "producers":        ", ".join(people.get("producers", [])),
            "writers":          ", ".join(people.get("writers", [])),
            "composers":        ", ".join(people.get("composers", [])),
            "editors":          ", ".join(people.get("editors", [])),
            "cinematographers": ", ".join(people.get("cinematographers", [])),
            "imdb_rating":      ratings.get("imdb"),
            "tmdb_rating":      ratings.get("tmdb"),
            "trakt_rating":     ratings.get("trakt"),
            "metacritic":       ratings.get("metacritic"),
            "rotten_tomatoes":  ratings.get("rottenTomatoes"),
            "popularity":       movie.get("popularity"),
            "has_file":         movie.get("hasFile") or movie.get("has_file"),
            "monitored":        movie.get("monitored"),
            "path":             movie.get("path"),
            "tags":             ", ".join(str(t) for t in movie.get("tags", [])),
        }
