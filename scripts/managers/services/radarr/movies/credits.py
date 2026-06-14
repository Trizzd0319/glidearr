from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrMovieCreditsExtractorManager(BaseManager, ComponentManagerMixin):
    """
    Extracts structured cast/crew credit data from Radarr movie entries.
    Actors, directors, producers, writers, composers, and studios are all
    pulled from the Radarr movie record's credits field.
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
    @timeit("get_credits")
    def get_credits(self, instance: str, movie_id: int) -> dict:
        """Fetch and structure credits for a single movie."""
        resolved = self._resolve_instance(instance)
        movie = self.radarr_api._make_request(resolved, f"movie/{movie_id}", fallback=None)
        if not movie:
            self.logger.log_warning(f"Movie {movie_id} not found in {resolved}")
            return {}

        return self._extract_credits(movie)

    @LoggerManager().log_function_entry
    @timeit("get_people_and_studios")
    def get_people_and_studios(self, instance: str) -> dict:
        """
        Fetch all movies and return a combined people + studio mapping used
        by the relational cache builder.
        Shape: {movie_id: {"credits": {...}, "studio": str}}
        """
        resolved = self._resolve_instance(instance)
        cached = self.global_cache.get(f"radarr.credits.{resolved}", default=None)
        if cached is not None:
            return cached

        movies = self.radarr_api._make_request(resolved, "movie", fallback=[]) or []
        result: dict = {}
        for movie in movies:
            mid = movie.get("id")
            if mid is None:
                continue
            result[mid] = {
                "title":   movie.get("title"),
                "year":    movie.get("year"),
                "tmdb_id": movie.get("tmdbId"),
                "credits": self._extract_credits(movie),
                "studio":  movie.get("studio"),
            }

        self.global_cache.set(f"radarr.credits.{resolved}", result)
        self.logger.log_info(f"Extracted people and studios for {len(result)} movies in {resolved}")
        return result

    def _extract_credits(self, movie: dict) -> dict:
        """
        Parse the nested credits structure from a Radarr movie record.
        Radarr stores cast/crew under movie['credits'] with castMembers and
        crewMembers sub-lists.
        """
        raw_credits = movie.get("credits", {})

        cast_members = raw_credits.get("castMembers", [])
        crew_members = raw_credits.get("crewMembers", [])

        actors:    list = []
        directors: list = []
        producers: list = []
        writers:   list = []
        composers: list = []
        editors:   list = []
        cinematographers: list = []
        other_crew: list = []

        for member in cast_members:
            name = member.get("name") or member.get("personName") or ""
            character = member.get("character", "")
            tmdb_id = member.get("tmdbId") or member.get("personTmdbId")
            actors.append({
                "name":      name,
                "character": character,
                "tmdb_id":   tmdb_id,
                "order":     member.get("order", 999),
            })

        for member in crew_members:
            name = member.get("name") or member.get("personName") or ""
            job = (member.get("job") or "").strip()
            department = (member.get("department") or "").strip().lower()
            tmdb_id = member.get("tmdbId") or member.get("personTmdbId")
            entry = {"name": name, "job": job, "department": department, "tmdb_id": tmdb_id}

            job_lower = job.lower()
            if "director" in job_lower:
                directors.append(entry)
            elif "producer" in job_lower or "executive" in job_lower:
                producers.append(entry)
            elif "writer" in job_lower or "screenplay" in job_lower or "story" in job_lower:
                writers.append(entry)
            elif "composer" in job_lower or "music" in department:
                composers.append(entry)
            elif "editor" in job_lower or "editing" in department:
                editors.append(entry)
            elif "cinematography" in department or "director of photography" in job_lower:
                cinematographers.append(entry)
            else:
                other_crew.append(entry)

        return {
            "actors":           actors,
            "directors":        directors,
            "producers":        producers,
            "writers":          writers,
            "composers":        composers,
            "editors":          editors,
            "cinematographers": cinematographers,
            "other_crew":       other_crew,
        }

    @LoggerManager().log_function_entry
    @timeit("get_bulk_credits")
    def get_bulk_credits(self, instance: str, movie_ids: list) -> list:
        """Fetch structured credits for multiple movies."""
        resolved = self._resolve_instance(instance)
        results = []
        for movie_id in movie_ids:
            credits = self.get_credits(resolved, movie_id)
            if credits:
                results.append({"movie_id": movie_id, "credits": credits})
        return results
