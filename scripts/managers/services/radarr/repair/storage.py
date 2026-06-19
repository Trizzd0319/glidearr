"""
RadarrRepairStorageManager
============================
Storage health checks for Radarr:
- Free space monitoring across root folders
- Recommendations for deletion when space is below threshold
- Large movie identification for manual review
"""

from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.space_targets import space_targets

# Default thresholds (used only as fallbacks when free_space_limit isn't configured)
DEFAULT_WARN_GB   = 50.0   # warn when free space drops below this
DEFAULT_CRIT_GB   = 20.0   # critical when free space drops below this
DEFAULT_LARGE_GB  = 30.0   # flag movies larger than this


class RadarrRepairStorageManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrRepairManager"

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
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)

        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(parent, "dry_run", None) if parent else None
        if _dry_run is None and self.registry:
            try:
                _root = self.registry.get("manager", "RadarrManager")
                _dry_run = getattr(_root, "dry_run", None) if _root else None
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
                f"RadarrManager, or Main. Refusing to initialize without an explicit value "
                f"from config.json to prevent accidental destructive operations."
            )
        self.dry_run = bool(_dry_run)

        self.warn_threshold_gb   = kwargs.get("warn_threshold_gb",   DEFAULT_WARN_GB)
        self.crit_threshold_gb   = kwargs.get("crit_threshold_gb",   DEFAULT_CRIT_GB)
        self.large_movie_gb      = kwargs.get("large_movie_gb",      DEFAULT_LARGE_GB)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    # ── Instance resolution ──────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    @staticmethod
    def _fmt_bytes(n: "int | float | None") -> str:
        if n is None or n != n:
            return "0 B"
        n = float(n)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(n) < 1024.0:
                return f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} PB"

    # ── Free space check ─────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("check_free_space")
    def check_free_space(self, instance: str) -> list[dict]:
        """
        Check free space across all root folders for this instance.

        Returns list of {path, free_space_gb, total_space_gb, status}
        where status is 'ok', 'warn', or 'critical'.
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            self.logger.log_warning("radarr_api not available")
            return []

        root_folders = self.radarr_api._make_request(instance, "rootfolder", fallback=[]) or []
        results = []

        # Classify against the single source of truth (space_targets): critical below the
        # floor T, warn inside the pressure band [T, U), ok at/above U. T = free_space_limit,
        # or 25% of the total drive when unset (mount-deduped via disk_total_gb) — never a
        # hardcoded GB floor. The legacy DEFAULT_CRIT/WARN_GB survive only as the last
        # resort when BOTH free_space_limit and the total drive size are unreadable.
        try:
            _total_gb = self.instance_manager.disk_total_gb(instance) if self.instance_manager else None
        except Exception:
            _total_gb = None
        crit_gb, warn_gb = space_targets(self.config, fallback_gb=self.crit_threshold_gb, total_gb=_total_gb)
        if warn_gb <= crit_gb:
            # space_targets returns no headroom band on the fallback floor (free_space_limit
            # unset). Keep a warn tier above critical so the diagnostic stays three-tier;
            # this only widens to DEFAULT_WARN_GB when total is also unknown (crit==DEFAULT_CRIT_GB).
            warn_gb = max(crit_gb, self.warn_threshold_gb)

        for rf in root_folders:
            path       = rf.get("path", "")
            free_bytes = rf.get("freeSpace", 0) or 0
            total_bytes = (rf.get("totalSpace") or 0)
            free_gb    = free_bytes / (1024 ** 3)
            # Radarr reports per-rootfolder totalSpace as 0 for Docker/mapped mounts, so a bare
            # "0.0 GB total" line looks like a disk emergency when it is just a missing field.
            # Prefer the REAL mount total (disk_total_gb via /diskspace, mount-deduped). The status
            # is decided by FREE vs the floor regardless — total never enters the verdict.
            total_gb = (total_bytes / (1024 ** 3)) or float(_total_gb or 0.0)

            if free_gb < crit_gb:
                status = "critical"
            elif free_gb < warn_gb:
                status = "warn"
            else:
                status = "ok"

            results.append({
                "path":          path,
                "free_space_gb": round(free_gb, 2),
                "total_space_gb": round(total_gb, 2),
                "status":        status,
            })

            log_fn = self.logger.log_warning if status != "ok" else self.logger.log_debug
            total_str = f"{total_gb:.0f} GB total" if total_gb > 0 else "total n/a"
            # "CRITICAL" here means below the configured free_space_limit floor (a reclaim policy),
            # NOT a full disk — name the floor so the line can't be misread as an emergency.
            note = f" (below {crit_gb:.0f} GB free_space_limit floor)" if status == "critical" else ""
            log_fn(
                f"[Storage] '{instance}' root '{path}': "
                f"{free_gb:.1f} GB free / {total_str} — {status.upper()}{note}"
            )

        return results

    # ── Deletion recommendations ─────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("recommend_deletions")
    def recommend_deletions(
        self,
        instance: str,
        target_free_gb: float = 100.0,
        limit: int = 20,
        space_info: "list[dict] | None" = None,
    ) -> list[dict]:
        """
        When storage is below threshold, recommend movies for deletion
        to recover space.

        Ranking criteria (in order):
        1. Unmonitored movies with files — no upgrade value
        2. Lowest-rated movies (imdb_rating ascending)
        3. Largest files (most space recovered)

        Returns sorted list of {movie_id, title, year, size_gb, imdb_rating, monitored, reason}
        limited to ``limit`` entries.
        """
        instance = self._resolve_instance(instance)

        # Reuse the caller's space scan when given (run() computes it once) so a root folder's
        # status isn't logged twice per pass; fall back to scanning if called standalone.
        if space_info is None:
            space_info = self.check_free_space(instance)
        if not space_info:
            return []

        lowest_free = min(s["free_space_gb"] for s in space_info)
        needed_gb   = max(0, target_free_gb - lowest_free)

        if needed_gb <= 0:
            self.logger.log_info(
                f"[Storage] '{instance}' has sufficient free space ({lowest_free:.1f} GB) "
                f"— no deletion recommendations needed."
            )
            return []

        if self.radarr_api is None:
            return []

        movies = self.radarr_api._make_request(instance, "movie", fallback=[]) or []
        candidates: list[dict] = []

        for m in movies:
            if not m.get("hasFile"):
                continue
            mf = m.get("movieFile") or {}
            size_bytes = mf.get("size") or 0
            size_gb    = size_bytes / (1024 ** 3)

            ratings    = m.get("ratings") or {}
            imdb_r     = (ratings.get("imdb") or {}).get("value") or 0
            monitored  = m.get("monitored", True)

            reason = "unmonitored" if not monitored else "low_rating"
            candidates.append({
                "movie_id":   m.get("id"),
                "title":      m.get("title"),
                "year":       m.get("year"),
                "size_gb":    round(size_gb, 2),
                "imdb_rating": imdb_r,
                "monitored":  monitored,
                "reason":     reason,
            })

        # Sort: unmonitored first, then by imdb_rating asc, then size desc
        candidates.sort(key=lambda c: (
            0 if not c["monitored"] else 1,
            float(c["imdb_rating"] or 0),
            -c["size_gb"],
        ))

        self.logger.log_info(
            f"[Storage] '{instance}' needs {needed_gb:.1f} GB — "
            f"recommending {min(limit, len(candidates))} deletion candidate(s)"
        )
        return candidates[:limit]

    # ── Large movie finder ───────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_large_movies")
    def find_large_movies(
        self,
        instance: str,
        threshold_gb: float | None = None,
    ) -> list[dict]:
        """
        Find movies larger than ``threshold_gb``.
        Returns list sorted by size descending.
        """
        instance  = self._resolve_instance(instance)
        if threshold_gb is not None:
            threshold = threshold_gb
        else:
            try:
                threshold = float(self.config.get("large_file_gb", self.large_movie_gb) if self.config else self.large_movie_gb)
            except (TypeError, ValueError):
                threshold = self.large_movie_gb

        if self.radarr_api is None:
            return []

        movies  = self.radarr_api._make_request(instance, "movie", fallback=[]) or []
        results = []

        for m in movies:
            if not m.get("hasFile"):
                continue
            mf   = m.get("movieFile") or {}
            size = (mf.get("size") or 0) / (1024 ** 3)
            if size >= threshold:
                results.append({
                    "movie_id": m.get("id"),
                    "title":    m.get("title"),
                    "year":     m.get("year"),
                    "size_gb":  round(size, 2),
                    "path":     mf.get("relativePath") or mf.get("path"),
                })

        results.sort(key=lambda r: -r["size_gb"])
        self.logger.log_info(
            f"[Storage] Large movies (>{threshold:.1f} GB) in '{instance}': {len(results)}"
        )
        return results

    # ── Full storage scan ────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_storage_scan")
    def run(self, instance: str) -> dict:
        instance = self._resolve_instance(instance)
        # Honour the configured floor: recommend deletions to keep free space at/above
        # free_space_limit (falls back to 100 GB only when it isn't set). Previously
        # this used a hardcoded 100 GB and ignored free_space_limit entirely.
        try:
            fsl = float(self.config.get("free_space_limit", 0) if self.config else 0) or 0.0
        except (TypeError, ValueError):
            fsl = 0.0
        target_free_gb = fsl if fsl > 0 else 100.0
        space_info = self.check_free_space(instance)          # scan ONCE; reused below
        return {
            "free_space":           space_info,
            "deletion_candidates":  self.recommend_deletions(
                instance, target_free_gb=target_free_gb, space_info=space_info),
            "large_movies":         self.find_large_movies(instance),
        }
