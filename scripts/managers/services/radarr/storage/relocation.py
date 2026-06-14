"""
RadarrStorageRelocationManager
================================
Handles moving movie files between Radarr instances based on
resolution or genre criteria.

This is a stub implementation with logging only — actual cross-instance
file moves require coordinating two Radarr APIs, filesystem access, and
careful re-import sequencing, which is handled at the orchestration layer.
"""

from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrStorageRelocationManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrStorageManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
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
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = (
            kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        )
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    # ── Instance resolution ──────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    # ── Core methods ─────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("relocate_movie")
    def relocate_movie(
        self,
        movie_id: int,
        from_instance: str,
        to_instance: str,
    ) -> dict:
        """
        Move a movie from one Radarr instance to another.

        Steps (logged only in this stub):
        1. Fetch full movie record from from_instance
        2. Add movie to to_instance via POST /movie
        3. Trigger a disk scan / import on to_instance
        4. Remove from from_instance once confirmed on destination

        Returns a result dict with status and any error details.
        """
        from_resolved = self._resolve_instance(from_instance)
        to_resolved   = self._resolve_instance(to_instance)

        self.logger.log_info(
            f"[Relocation] movie_id={movie_id}: "
            f"'{from_resolved}' -> '{to_resolved}'"
        )

        if self.dry_run:
            self.logger.log_info(
                f"[dry_run] Would relocate movie {movie_id} "
                f"from '{from_resolved}' to '{to_resolved}'"
            )
            return {
                "movie_id":      movie_id,
                "from_instance": from_resolved,
                "to_instance":   to_resolved,
                "status":        "dry_run",
                "error":         None,
            }

        # Stub: log intent, no actual filesystem operation
        self.logger.log_info(
            f"[Relocation] Stub — actual file move not implemented. "
            f"movie_id={movie_id} would move '{from_resolved}' -> '{to_resolved}'"
        )
        return {
            "movie_id":      movie_id,
            "from_instance": from_resolved,
            "to_instance":   to_resolved,
            "status":        "stub_not_implemented",
            "error":         None,
        }

    @LoggerManager().log_function_entry
    @timeit("determine_target_instance")
    def determine_target_instance(
        self,
        movie: dict,
        free_space_map: dict[str, float],
    ) -> str | None:
        """
        Determine the best target Radarr instance for a movie based on
        resolution, genre, and available free space.

        Resolution routing:
        - 2160p / 4K -> prefer '4k' instance
        - 1080p       -> prefer '1080' instance
        - 720p        -> prefer '720' instance

        Free space guard: if the preferred instance has < 10 GB free,
        fall back to the instance with the most free space.

        Returns instance name string, or None if no suitable target found.
        """
        resolution = ""
        movie_file = movie.get("movieFile") or {}
        quality    = movie_file.get("quality") or {}
        qq         = (quality.get("quality") or {})
        res_val    = qq.get("resolution") or 0

        try:
            res_int = int(res_val)
        except (TypeError, ValueError):
            res_int = 0

        if res_int >= 2160:
            preferred = "4k"
        elif res_int >= 1080:
            preferred = "1080"
        elif res_int >= 720:
            preferred = "720"
        else:
            preferred = None

        MIN_FREE_GB = 10.0

        if preferred:
            # Resolve preferred instance name
            resolved_preferred = None
            if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
                try:
                    resolved_preferred = self.instance_manager.resolve_instance(preferred)
                except Exception:
                    pass

            if resolved_preferred and free_space_map.get(resolved_preferred, 0) >= MIN_FREE_GB:
                self.logger.log_debug(
                    f"[Relocation] '{movie.get('title')}' ({res_int}p) -> "
                    f"preferred instance '{resolved_preferred}'"
                )
                return resolved_preferred

        # Fallback: instance with most free space
        if free_space_map:
            best = max(free_space_map, key=free_space_map.get)
            if free_space_map[best] >= MIN_FREE_GB:
                self.logger.log_debug(
                    f"[Relocation] '{movie.get('title')}' — no preferred instance available, "
                    f"falling back to '{best}' ({free_space_map[best]:.1f} GB free)"
                )
                return best

        self.logger.log_warning(
            f"[Relocation] No suitable instance found for '{movie.get('title')}' — "
            f"all instances below {MIN_FREE_GB} GB threshold or free_space_map empty"
        )
        return None

    @LoggerManager().log_function_entry
    @timeit("get_relocation_candidates")
    def get_relocation_candidates(self, instance: str) -> list[dict]:
        """
        Return a list of movies in the given instance that appear to be
        in the wrong instance based on their resolution.

        Each entry: {movie_id, title, year, resolution, current_instance, suggested_instance}
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            self.logger.log_warning("radarr_api not available — cannot scan for relocation candidates")
            return []

        movies = self.radarr_api._make_request(instance, "movie", fallback=[]) or []
        free_space_map: dict[str, float] = {}

        candidates: list[dict] = []
        for movie in movies:
            if not movie.get("hasFile"):
                continue
            suggested = self.determine_target_instance(movie, free_space_map)
            if suggested and suggested != instance:
                mf  = movie.get("movieFile") or {}
                qq  = ((mf.get("quality") or {}).get("quality") or {})
                candidates.append({
                    "movie_id":          movie.get("id"),
                    "title":             movie.get("title"),
                    "year":              movie.get("year"),
                    "resolution":        qq.get("resolution"),
                    "current_instance":  instance,
                    "suggested_instance": suggested,
                })

        self.logger.log_info(
            f"[Relocation] Found {len(candidates)} candidate(s) in '{instance}'"
        )
        return candidates
