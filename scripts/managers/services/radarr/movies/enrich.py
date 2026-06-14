from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrMovieEnrichmentManager(BaseManager, ComponentManagerMixin):
    """
    Enriches Radarr movie entries with structured metadata for ML and analysis.
    Combines keywords, people, studios, ratings, and technical details into a
    single enriched dict per movie.
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
    @timeit("build_enriched_movies")
    def build_enriched_movies(self, instance: str) -> list:
        """
        Fetch all movies for the instance and return a list of enriched dicts.
        Results are also written to the global cache.
        """
        resolved = self._resolve_instance(instance)
        cached = self.global_cache.get(f"radarr.movies.{resolved}.enriched", default=None)
        if cached is not None:
            return cached

        movies = self.radarr_api._make_request(resolved, "movie", fallback=[]) or []
        enriched = [self.enrich_movie(m) for m in movies]

        self.global_cache.set(f"radarr.movies.{resolved}.enriched", enriched)
        self.logger.log_info(f"Built enriched movie list: {len(enriched)} movies for {resolved}")
        return enriched

    @LoggerManager().log_function_entry
    @timeit("enrich_movie")
    def enrich_movie(self, movie: dict) -> dict:
        """Enrich an individual movie dict with structured metadata."""
        enriched = dict(movie)

        enriched["people"]     = self._extract_people(movie)
        enriched["studio"]     = movie.get("studio")
        enriched["keywords"]   = movie.get("keywords", []) + movie.get("genres", [])
        enriched["genres"]     = movie.get("genres", [])
        enriched["popularity"] = movie.get("popularity", 0)
        enriched["ratings"]    = self._extract_ratings(movie)
        enriched["tmdb_id"]    = movie.get("tmdbId")
        enriched["imdb_id"]    = movie.get("imdbId")
        enriched["has_file"]   = movie.get("hasFile", False)
        enriched["runtime"]    = movie.get("runtime", 0)
        enriched["monitored"]  = movie.get("monitored", False)
        enriched["collection"] = movie.get("collection", {})

        return enriched

    def _extract_people(self, movie: dict) -> dict:
        """Extract cast/crew from credits sub-structure."""
        raw = movie.get("credits", {})
        cast_members = raw.get("castMembers", [])
        crew_members = raw.get("crewMembers", [])

        roles: dict = {
            "actors":           [],
            "directors":        [],
            "producers":        [],
            "writers":          [],
            "composers":        [],
            "editors":          [],
            "cinematographers": [],
        }

        for member in cast_members:
            name = member.get("name") or member.get("personName") or ""
            if name:
                roles["actors"].append(name)

        for member in crew_members:
            name = member.get("name") or member.get("personName") or ""
            job  = (member.get("job") or "").lower()
            dept = (member.get("department") or "").lower()
            if not name:
                continue
            if "director" in job:
                roles["directors"].append(name)
            elif "producer" in job or "executive" in job:
                roles["producers"].append(name)
            elif "writer" in job or "screenplay" in job or "story" in job:
                roles["writers"].append(name)
            elif "composer" in job or "music" in dept:
                roles["composers"].append(name)
            elif "editor" in job or "editing" in dept:
                roles["editors"].append(name)
            elif "cinematography" in dept or "director of photography" in job:
                roles["cinematographers"].append(name)

        return roles

    def _extract_ratings(self, movie: dict) -> dict:
        ratings = movie.get("ratings") or {}
        return {
            "imdb":          (ratings.get("imdb") or {}).get("value"),
            "tmdb":          (ratings.get("tmdb") or {}).get("value"),
            "metacritic":    (ratings.get("metacritic") or {}).get("value"),
            "rottenTomatoes": (ratings.get("rottenTomatoes") or {}).get("value"),
            "trakt":         (ratings.get("trakt") or {}).get("value"),
        }
