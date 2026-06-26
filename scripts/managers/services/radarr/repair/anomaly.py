"""
RadarrRepairAnomalyManager
===========================
Detects anomalous movie states in Radarr:
- Movies in the wrong instance (resolution mismatch vs. instance policy)
- Movies with missing files but still monitored
"""

from __future__ import annotations

from datetime import datetime, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.classification.keep_policy import resolve_keep_policy
from scripts.managers.machine_learning.space.downgrade_planner import UNIVERSE_PROTECT_MIN
from scripts.managers.machine_learning.lifecycle.monitor_policy import (
    release_available,
    triage_action,
)
from scripts.managers.machine_learning.lifecycle.stale_prune_policy import (
    budget_delete_cohort,
    clock_age,
    expedite_dwell,
    franchise_delete_exempt,
    prune_below_floor_action,
    prune_score_gate,
    restore_cooldown_active,
)
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.space_targets import (
    coordinator_owns_deletion, deletions_enabled, space_targets,
)


# ── Grid-cell formatters (shared by every Radarr movie table) ────────────────────
# Keep the dry-run / repair grids self-explanatory: a Year disambiguates same-titled
# entries (e.g. several "Demon Slayer" films) and a critic Rating gives an outside
# anchor to read the watchability Score against. Both are pulled straight from the
# already-cached Radarr movie dict — no extra API calls.

def _movie_year(movie: dict | None) -> str:
    y = (movie or {}).get("year")
    return str(y) if y else "-"


def _movie_rating(movie: dict | None) -> str:
    """Compact, human-recognizable critic anchor for a Radarr movie dict:
    IMDb -> TMDb -> Trakt as ``x.x`` (out of 10), else Rotten Tomatoes ->
    Metacritic as ``n%`` (out of 100), else '-'."""
    r = (movie or {}).get("ratings") or {}
    for key in ("imdb", "tmdb", "trakt"):
        v = (r.get(key) or {}).get("value")
        if v:
            try:
                return f"{float(v):.1f}"
            except (TypeError, ValueError):
                pass
    for key in ("rottenTomatoes", "metacritic"):
        v = (r.get(key) or {}).get("value")
        if v:
            try:
                return f"{int(round(float(v)))}%"
            except (TypeError, ValueError):
                pass
    return "-"


class RadarrRepairAnomalyManager(BaseManager, ComponentManagerMixin):
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
        self.radarr_api      = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    # ── Instance resolution ──────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    # ── Keep-policy resolution ───────────────────────────────────────────────────

    def _resolve_keep_policy(self, movie: dict, tag_label_map: dict[int, str]) -> str | None:
        """Resolve a movie's keep_policy from its Radarr tag labels — delegates to
        the brain (classification.keep_policy.resolve_keep_policy). Priority:
        keep_forever > keep_movie > keep_universe > universe > None. A non-None result
        is an explicit user override: never unmonitored regardless of its score."""
        return resolve_keep_policy(movie, tag_label_map)

    # ── Shared scoring context ───────────────────────────────────────────────────

    def _build_scoring_context(self, instance: str) -> dict:
        """Gather the (read-only) global_cache inputs the watchability scorer needs,
        once, so triage_monitored_missing AND repair_unmonitored_with_files score
        owned/missing movies from the same data without duplicating the lookups."""
        gc = self.global_cache
        all_movies: list[dict] = (gc.get(f"radarr.movies.{instance}.full") or []) if gc else []
        movie_by_tmdb = {int(m["tmdbId"]): m for m in all_movies if m.get("tmdbId")}
        movie_by_id   = {int(m["id"]): m for m in all_movies if m.get("id")}

        genre_affinity = (gc.get("tautulli/affinity") if gc else None) or {}

        watched_tmdb_ids: set[int] = set()
        for entry in ((gc.get("trakt/history/movies") if gc else None) or []):
            tid = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
            if tid:
                watched_tmdb_ids.add(int(tid))

        # Belt-and-suspenders: also fold in movies the household watched per
        # Tautulli (Plex is the upstream source of truth and is pushed to Trakt
        # anyway). This keeps the watched-set populated even when the live Trakt
        # history fetch is rate-limited and served stale. A movie counts as
        # watched when its max completion meets its threshold.
        if gc:
            rating_groups_cfg = (self.config.get("rating_groups", {}) if self.config else {}) or {"household": {}}
            for group_name in rating_groups_cfg:
                comp = gc.get(f"tautulli/group/{group_name}/tmdb_completions") or {}
                for tmdb_str, data in comp.items():
                    try:
                        pct = float((data or {}).get("pct", 0.0))
                        thr = float((data or {}).get("threshold", 0.85))
                    except (TypeError, ValueError):
                        continue
                    if pct >= thr:
                        try:
                            watched_tmdb_ids.add(int(tmdb_str))
                        except (TypeError, ValueError):
                            continue

        collection_members: dict[int, set[int]] = {}
        for m in all_movies:
            coll_id = (m.get("collection") or {}).get("tmdbId")
            mid     = m.get("tmdbId")
            if coll_id and mid:
                collection_members.setdefault(int(coll_id), set()).add(int(mid))

        tag_label_map: dict[int, str] = {}
        try:
            raw_tags = (
                (gc.get(f"radarr.tags.{instance}") if gc else None)
                or (self.radarr_api._make_request(instance, "tag", fallback=[]) if self.radarr_api else [])
                or []
            )
            tag_label_map = {t["id"]: t["label"] for t in raw_tags if t.get("id") is not None}
        except Exception:
            pass

        people_mgr = None
        try:
            trakt_movies = self.registry.get("manager", "TraktMoviesManager") if self.registry else None
            people_mgr   = getattr(trakt_movies, "people", None) if trakt_movies else None
        except Exception:
            pass

        return {
            "all_movies": all_movies,
            "movie_by_tmdb": movie_by_tmdb,
            "movie_by_id": movie_by_id,
            "genre_affinity": genre_affinity,
            "watched_tmdb_ids": watched_tmdb_ids,
            "collection_members": collection_members,
            "tag_label_map": tag_label_map,
            "people_mgr": people_mgr,
        }

    # ── Resolution mismatch detection ───────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_resolution_mismatches")
    def find_resolution_mismatches(self, instance: str) -> list[dict]:
        """
        Detect movies in the wrong instance based on resolution.

        Resolution routing policy (configurable defaults):
        - 2160p -> '4k' instance
        - 1080p -> '1080' instance
        - 720p  -> '720' instance

        Returns list of {movie_id, title, year, resolution, current_instance, expected_instance}
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            self.logger.log_warning("radarr_api not available — cannot scan for resolution mismatches")
            return []

        # Derive the expected resolution tier for this instance from its name/config
        inst_lower = instance.lower()
        if "4k" in inst_lower or "2160" in inst_lower or "uhd" in inst_lower:
            expected_res_min, expected_res_max = 2160, 9999
        elif "1080" in inst_lower:
            expected_res_min, expected_res_max = 1080, 2159
        elif "720" in inst_lower or "hd" in inst_lower:
            expected_res_min, expected_res_max = 720, 1079
        else:
            # Unknown instance type — cannot determine policy
            self.logger.log_debug(
                f"[Anomaly] Instance '{instance}' has no recognisable resolution tier — "
                f"skipping resolution mismatch check."
            )
            return []

        movies = self.radarr_api._make_request(instance, "movie", fallback=[]) or []
        mismatches: list[dict] = []

        for movie in movies:
            if not movie.get("hasFile"):
                continue
            mf  = movie.get("movieFile") or {}
            qq  = ((mf.get("quality") or {}).get("quality") or {})
            res = qq.get("resolution")
            if res is None:
                continue

            try:
                res_int = int(res)
            except (TypeError, ValueError):
                continue

            if not (expected_res_min <= res_int <= expected_res_max):
                mismatches.append({
                    "movie_id":         movie.get("id"),
                    "title":            movie.get("title"),
                    "year":             movie.get("year"),
                    "resolution":       res_int,
                    "current_instance": instance,
                    "expected_instance": self._suggest_instance(res_int),
                })

        self.logger.log_info(
            f"[Anomaly] Resolution mismatch scan for '{instance}': "
            f"{len(mismatches)} issue(s) found out of {len(movies)} movies."
        )
        return mismatches

    @timeit("_suggest_instance")
    def _suggest_instance(self, resolution: int) -> str:
        if resolution >= 2160:
            return "4k"
        elif resolution >= 1080:
            return "1080"
        elif resolution >= 720:
            return "720"
        return "default"

    # ── Missing file but monitored ───────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_monitored_missing_files")
    def find_monitored_missing_files(self, instance: str) -> list[dict]:
        """
        Find movies that are monitored but have no file (hasFile=False).
        These may represent stuck downloads or misconfigured entries.

        Returns list of {movie_id, title, year, tmdb_id, monitored, has_file}
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            self.logger.log_warning("radarr_api not available — cannot scan for missing files")
            return []

        movies  = self.radarr_api._make_request(instance, "movie", fallback=[]) or []
        missing = [
            {
                "movie_id":  m.get("id"),
                "title":     m.get("title"),
                "year":      m.get("year"),
                "tmdb_id":   m.get("tmdbId"),
                "monitored": m.get("monitored"),
                "has_file":  m.get("hasFile"),
            }
            for m in movies
            if m.get("monitored") and not m.get("hasFile")
        ]

        self.logger.log_info(
            f"[Anomaly] Monitored-missing scan for '{instance}': "
            f"{len(missing)} movie(s) monitored but missing file."
        )
        return missing

    # ── Unmonitored movies with files ────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_unmonitored_with_files")
    def find_unmonitored_with_files(self, instance: str) -> list[dict]:
        """
        Find movies that have a file but are NOT monitored.
        These are consuming disk space without upgrade protection.
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            return []

        movies = self.radarr_api._make_request(instance, "movie", fallback=[]) or []
        result = [
            {
                "movie_id":  m.get("id"),
                "title":     m.get("title"),
                "year":      m.get("year"),
                "tmdb_id":   m.get("tmdbId"),
                "monitored": m.get("monitored"),
                "has_file":  m.get("hasFile"),
            }
            for m in movies
            if not m.get("monitored") and m.get("hasFile")
        ]

        self.logger.log_info(
            f"[Anomaly] Unmonitored-with-file scan for '{instance}': "
            f"{len(result)} movie(s) have files but are unmonitored."
        )
        return result

    # ── Repair: re-monitor missing ────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("repair_monitored_missing")
    def repair_monitored_missing(self, instance: str, movie_ids: list[int] | None = None) -> dict:
        """
        Trigger a search for all monitored movies that are missing files.
        If movie_ids is provided, only those are searched.

        Returns stats dict with triggered/failed counts.
        """
        instance = self._resolve_instance(instance)
        stats = {"checked": 0, "triggered": 0, "failed": 0}

        if self.radarr_api is None:
            return stats

        candidates = self.find_monitored_missing_files(instance)
        if movie_ids is not None:
            candidates = [c for c in candidates if c["movie_id"] in movie_ids]

        # Year + critic Rating for the grid — cheap cache read, no extra API calls.
        _full = (self.global_cache.get(f"radarr.movies.{instance}.full") or []) if self.global_cache else []
        movie_by_id = {int(m["id"]): m for m in _full if m.get("id")}

        def _row(c):
            mv  = movie_by_id.get(int(c["movie_id"])) if c.get("movie_id") else None
            _yr = c.get("year")
            return [str(c["title"])[:28], str(_yr) if _yr else "-",
                    _movie_rating(mv), str(c["movie_id"])]

        _rows: list[list] = []
        for c in candidates:
            stats["checked"] += 1
            mid   = c["movie_id"]
            title = c["title"]
            if self.dry_run:
                _rows.append(_row(c))
                stats["triggered"] += 1
                continue
            try:
                # Cancel any in-flight queue item first so the fresh search
                # uses the best-scored quality profile, not a stale grab.
                from scripts.managers.factories.mixins.queue_cancel import QueueCancelMixin
                _qc            = QueueCancelMixin()
                _qc.radarr_api = self.radarr_api
                _qc.logger     = self.logger
                _qc.dry_run    = False
                q_cancelled    = _qc._cancel_radarr_queue_for_movie(
                    instance, mid, movie_title=title
                )
                if q_cancelled:
                    stats["queue_cancelled"] = stats.get("queue_cancelled", 0) + q_cancelled

                self.radarr_api._make_request(
                    instance,
                    "command",
                    method="POST",
                    payload={"name": "MoviesSearch", "movieIds": [mid]},
                )
                _rows.append(_row(c))
                stats["triggered"] += 1
            except Exception as e:
                self.logger.log_warning(
                    f"[Anomaly] Search trigger failed for '{c['title']}' (id={mid}): {e}"
                )
                stats["failed"] += 1

        _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
        if _rs is not None:
            _rs.add_rows("radarr", "Search missing movies", instance,
                         ["Title", "Year", "Rating", "Id"], _rows, order=33)
        else:
            self.logger.log_grid(
                ["Title", "Year", "Rating", "Id"], _rows,
                title=(
                    f"Radarr: search missing movies{' [dry_run]' if self.dry_run else ''}"
                    f"  ({len(candidates)} monitored-missing)"
                ),
                cap=28,
            )
        return stats

    # ── Repair: re-monitor movies with files ────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("repair_unmonitored_with_files")
    def repair_unmonitored_with_files(self, instance: str) -> dict:
        """
        Decide which owned (has-file) but UNMONITORED movies to monitor — driven by
        watchability, NOT a blanket re-monitor. Lets a clean "unmonitor everything,
        let the scripting monitor what we care about" workflow actually work.

        Policy (config ``owned_monitor_policy``, default "watchability"):
          watchability — monitor a movie iff it is keep/universe-tagged, OR has been
                         watched, OR scores >= ``owned_monitor_score_threshold``
                         (default 35). Everything else stays unmonitored. A movie whose
                         Trakt credits aren't cached yet (affinity unknown) is DEFERRED
                         — left unmonitored — so a household favourite isn't skipped
                         before the enrichment daemon fills its data; the next run
                         re-scores it.
          all          — legacy: monitor every owned movie unconditionally.
          off          — never auto-monitor; leave the user's monitoring untouched.
        """
        instance = self._resolve_instance(instance)
        stats = {
            "checked": 0, "monitored": 0, "monitored_keep": 0, "monitored_watched": 0,
            "monitored_score": 0, "left_unmonitored": 0, "deferred": 0, "failed": 0,
        }
        if self.radarr_api is None:
            return stats

        candidates = self.find_unmonitored_with_files(instance)
        if not candidates:
            return stats
        title_by_id = {c["movie_id"]: c["title"] for c in candidates}
        year_by_id  = {c["movie_id"]: c.get("year") for c in candidates}
        rating_by_id: dict[int, str] = {}   # filled in the watchability branch below

        policy = str(self.config.get("owned_monitor_policy", "watchability")
                     if self.config else "watchability").lower().strip()
        try:
            threshold = int(self.config.get("owned_monitor_score_threshold", 35)
                            if self.config else 35)
        except (TypeError, ValueError):
            threshold = 35

        # ── off: leave everything exactly as the user set it ─────────────────────
        if policy == "off":
            stats["checked"] = len(candidates)
            stats["left_unmonitored"] = len(candidates)
            self.logger.log_info(
                f"[Anomaly] owned_monitor_policy=off — leaving {len(candidates)} "
                f"unmonitored owned movie(s) untouched for '{instance}'."
            )
            return stats

        monitor_reason: dict[int, str] = {}

        # ── all: legacy unconditional re-monitor ─────────────────────────────────
        if policy == "all":
            monitor_ids = [c["movie_id"] for c in candidates]
            stats["checked"] = len(monitor_ids)
        else:
            # ── watchability: score-gated selection (the default) ────────────────
            from scripts.managers.services.trakt.movies.scorer import score_movie
            ctx = self._build_scoring_context(instance)
            monitor_ids: list[int] = []

            def _rate(mv: dict, key: str):
                return ((mv.get("ratings", {}) or {}).get(key) or {}).get("value")

            for c in candidates:
                stats["checked"] += 1
                mid     = c["movie_id"]
                tmdb_id = c.get("tmdb_id")
                movie   = ctx["movie_by_id"].get(int(mid)) if mid else None
                if not movie and tmdb_id:
                    movie = ctx["movie_by_tmdb"].get(int(tmdb_id))
                rating_by_id[mid] = _movie_rating(movie)

                keep_policy = self._resolve_keep_policy(movie, ctx["tag_label_map"]) if movie else None
                watched     = bool(tmdb_id and int(tmdb_id) in ctx["watched_tmdb_ids"])

                # Explicit keep/universe tag and "you've watched it" are hard
                # monitor signals — independent of score / enrichment state.
                if keep_policy:
                    monitor_ids.append(mid); monitor_reason[mid] = f"keep={keep_policy}"
                    stats["monitored_keep"] += 1
                    continue
                if watched:
                    monitor_ids.append(mid); monitor_reason[mid] = "watched"
                    stats["monitored_watched"] += 1
                    continue

                # Score branch — affinity (the biggest lever) needs Trakt credits.
                credits: dict = {}
                if ctx["people_mgr"] and tmdb_id:
                    try:
                        credits = ctx["people_mgr"].get_people(int(tmdb_id)) or {}
                    except Exception:
                        pass

                score = 0
                if movie:
                    try:
                        ol = movie.get("originalLanguage")
                        score = score_movie(
                            movie=movie, completion_pct=0.0, completion_threshold=0.9,
                            collection_members=ctx["collection_members"],
                            watched_tmdb_ids=ctx["watched_tmdb_ids"],
                            genre_affinity=ctx["genre_affinity"], credits=credits,
                            imdb_rating=_rate(movie, "imdb"),
                            tmdb_rating=_rate(movie, "tmdb"),
                            trakt_rating=_rate(movie, "trakt"),
                            metacritic_score=_rate(movie, "metacritic"),
                            rotten_tomatoes_score=_rate(movie, "rottenTomatoes"),
                            popularity=movie.get("popularity"),
                            certification=movie.get("certification"),
                            in_cinemas_date=movie.get("inCinemas"),
                            original_language=ol.get("name") if isinstance(ol, dict) else ol,
                            keep_policy=keep_policy,
                        )
                    except Exception as e:
                        self.logger.log_debug(f"[Anomaly] score failed for '{title_by_id.get(mid, mid)}': {e}")

                if score >= threshold:
                    monitor_ids.append(mid); monitor_reason[mid] = f"score={score}"
                    stats["monitored_score"] += 1
                elif not credits:
                    # Affinity unknown — defer (leave unmonitored); re-scored next run
                    # once the enrichment daemon has cached this movie's credits.
                    stats["deferred"] += 1
                else:
                    stats["left_unmonitored"] += 1

        # ── Apply ────────────────────────────────────────────────────────────────
        if self.dry_run:
            _rows: list[list] = []
            for mid in monitor_ids:
                reason = monitor_reason.get(mid, "policy=all")
                _yr = year_by_id.get(mid)
                _rows.append([
                    str(title_by_id.get(mid, mid))[:28], str(_yr) if _yr else "-",
                    rating_by_id.get(mid, "-"), str(mid), str(reason),
                ])
            _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
            if _rs is not None:
                _rs.add_rows("radarr", "Would re-monitor owned", instance,
                             ["Title", "Year", "Rating", "Id", "Reason"], _rows, order=34)
            else:
                self.logger.log_grid(
                    ["Title", "Year", "Rating", "Id", "Reason"], _rows,
                    title=(
                        f"Radarr: would re-monitor owned [dry_run]"
                        f"  (policy={policy}, score>={threshold})"
                    ),
                    cap=28,
                )
            stats["monitored"] = len(monitor_ids)
        elif monitor_ids:
            ok = False
            try:
                resp = self.radarr_api._make_request(
                    instance, "movie/editor", method="PUT",
                    payload={"movieIds": monitor_ids, "monitored": True},
                )
                ok = bool(resp)
            except Exception as e:
                self.logger.log_warning(
                    f"  Bulk re-monitor failed for {len(monitor_ids)} movie(s): {e}"
                )
            if ok:
                stats["monitored"] = len(monitor_ids)
                self.logger.log_info(f"  Set monitored=True for {len(monitor_ids)} movie(s)")
            else:
                stats["failed"] = len(monitor_ids)

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["monitored",         stats["monitored"]],
                ["monitored_keep",    stats["monitored_keep"]],
                ["monitored_watched", stats["monitored_watched"]],
                ["monitored_score",   stats["monitored_score"]],
                ["left_unmonitored",  stats["left_unmonitored"]],
                ["deferred",          stats["deferred"]],
                ["failed",            stats["failed"]],
            ],
            title=f"[Anomaly] {prefix}Owned-movie monitor pass - '{instance}' (policy={policy}, threshold={threshold})",
            caption="Which owned has-file but unmonitored movies got re-monitored this pass and why.",
            descriptions=[
                "owned movies set monitored=True this pass",
                "monitored because keep/universe tagged",
                "monitored because already watched",
                "monitored because score met the threshold",
                "left unmonitored (score below threshold)",
                "deferred: Trakt credits not cached yet",
                "movies whose bulk re-monitor call errored",
            ],
        )
        return stats

    # ── Demote: two-stage prune of stale, low-watchability owned movies ──────────

    _DELETE_CLOCK_KEY = "radarr/{inst}/monitor_demote_clock"
    _DELETED_SET_KEY  = "radarr/{inst}/demote_deleted"

    def _score_owned(self, movie: dict, ctx: dict, score_movie) -> tuple[int, bool]:
        """Score an owned/known movie. Returns (score, credits_present). credits_present
        is False when Trakt credits aren't cached yet — callers DEFER on that so a movie
        is never demoted/deleted on understated (un-enriched) affinity."""
        tmdb_id = movie.get("tmdbId")
        credits: dict = {}
        if ctx["people_mgr"] and tmdb_id is not None:
            try:
                credits = ctx["people_mgr"].get_people(int(tmdb_id)) or {}
            except Exception:
                pass
        if not credits:
            return 0, False
        keep_policy = self._resolve_keep_policy(movie, ctx["tag_label_map"])
        ol = movie.get("originalLanguage")

        def _rate(key):
            return ((movie.get("ratings", {}) or {}).get(key) or {}).get("value")
        try:
            score = score_movie(
                movie=movie, completion_pct=0.0, completion_threshold=0.9,
                collection_members=ctx["collection_members"],
                watched_tmdb_ids=ctx["watched_tmdb_ids"],
                genre_affinity=ctx["genre_affinity"], credits=credits,
                imdb_rating=_rate("imdb"), tmdb_rating=_rate("tmdb"),
                trakt_rating=_rate("trakt"), metacritic_score=_rate("metacritic"),
                rotten_tomatoes_score=_rate("rottenTomatoes"),
                popularity=movie.get("popularity"), certification=movie.get("certification"),
                in_cinemas_date=movie.get("inCinemas"),
                original_language=ol.get("name") if isinstance(ol, dict) else ol,
                keep_policy=keep_policy,
            )
        except Exception:
            return -1, True   # sentinel: scoring error → caller treats as "do nothing"
        return int(score), True

    @LoggerManager().log_function_entry
    @timeit("demote_stale_monitored")
    def demote_stale_monitored(self, instance: str) -> dict:
        """
        Two-stage, watchability-driven pruning of owned movies whose score stays below
        the demote floor (owned_demote_score_threshold, default 20):

          stage 1  unmonitor — below floor for owned_demote_dwell_days (default 30):
                               set monitored=False (file is kept).
          stage 2  DELETE    — below floor for owned_delete_dwell_days (default 90):
                               delete the movie file. The deletion is tracked so the
                               movie is RESTORED (re-monitored + re-searched) later if
                               its score recovers — see restore_recovered_deletions.

        Anti-bias / anti-churn safeguards:
          * Hysteresis — promote at owned_monitor_score_threshold (35); act only below
            the demote floor (20). The 20-35 band is sticky → no flapping.
          * One per-movie clock (global_cache) tracks "continuously below floor since";
            it RESETS the instant the score recovers to >= floor.
          * Hard guards — keep/universe-tagged OR ever-watched movies are never touched.
          * Data-completeness guard — a movie whose Trakt credits aren't cached is
            DEFERRED (no action, clock preserved). Deletion only ever happens on a
            fully-enriched, sustained low score — never on missing affinity data.

        dry_run logs "would ..." and mutates nothing; the clock still advances so
        elapsed time is real (mirrors the deletion grace-period pattern).
        """
        instance = self._resolve_instance(instance)
        stats = {"checked": 0, "below_floor": 0, "aging": 0, "unmonitored": 0,
                 "deleted": 0, "guarded": 0, "deferred": 0, "recovered": 0, "failed": 0,
                 "skipped_universe": 0}

        if not bool(self.config.get("owned_demote_enabled", True) if self.config else True):
            return stats
        if self.radarr_api is None or self.global_cache is None:
            return stats

        def _int(key: str, default: int) -> int:
            try:
                return int(self.config.get(key, default) if self.config else default)
            except (TypeError, ValueError):
                return default
        floor          = _int("owned_demote_score_threshold", 20)
        unmonitor_days = _int("owned_demote_dwell_days", 30)
        delete_days    = _int("owned_delete_dwell_days", 90)
        # HARD SAFETY GATE folded into delete_enabled: with no operator-set
        # free_space_limit the stale prune may still unmonitor + age its clocks, but
        # its DELETE stage is disabled (was: legacy always-active time-based delete).
        delete_enabled = (
            bool(self.config.get("owned_delete_enabled", True) if self.config else True)
            and deletions_enabled(self.config)
        )

        # ── Behavior-change gates (both default-OFF → byte-identical) ─────────────
        # (1) Franchise exemption: spare a below-floor movie from DELETION when its
        #     collection is substantially watched (raw watched fraction of siblings).
        # (2) Cohort budget: under pressure, delete only enough of the worst/biggest
        #     stale movies to reclaim free space back to U, not the whole backlog.
        _cfg = self.config or {}
        _franchise_exempt_enabled = bool(_cfg.get("owned_delete_franchise_exempt_enabled", False))
        try:
            _franchise_threshold = float(_cfg.get("owned_delete_franchise_watched_fraction", 0.5))
        except (TypeError, ValueError):
            _franchise_threshold = 0.5
        _budget_enabled = bool(_cfg.get("owned_delete_budget_enabled", False))

        # ── Space-pressure gating + expedite ──────────────────────────────────────
        # The stale prune (unmonitor + delete) only ACTS when free space is in the
        # pressure band (free < U); with plenty of space the clocks still advance so
        # the dwell is already counted when pressure later arrives — nothing is pruned
        # while the disk is comfortable. And the closer free gets to the floor T, the
        # shorter the delete dwell (expedite). With no free_space_limit configured it
        # falls back to the legacy time-based prune (always active).
        min_delete_days = _int("owned_delete_min_dwell_days", 7)
        eff_delete_days = delete_days
        free_gb = float("inf")
        try:
            free_gb = float(self.radarr_api.disk_free_gb(instance))
        except Exception:
            pass
        # fallback_gb=0.0 and NO total_gb here is deliberate (do NOT thread disk_total_gb):
        # this is a SENTINEL, not a space gate. T>0 only when free_space_limit is set; the
        # operator opting into a floor is what switches the owned-movie stale prune from the
        # legacy always-active time-based dwell to pressure-gated+expedited. Threading
        # total_gb would silently flip every unconfigured install to "only prune below
        # 25%-of-total", quietly weakening the owned-deletion path the operator never tuned.
        T, U = space_targets(self.config, fallback_gb=0.0)
        eff_delete_days, pressure_active = expedite_dwell(
            free_gb, T, U, delete_days, min_delete_days
        )
        # The cross-service coordinator (when enabled) owns deletion; defer to it.
        delete_active = pressure_active and not coordinator_owns_deletion(self.config)

        from scripts.managers.services.trakt.movies.scorer import score_movie
        ctx = self._build_scoring_context(instance)
        owned = [m for m in ctx["all_movies"] if m.get("hasFile") and m.get("tmdbId") is not None]
        if not owned:
            return stats

        # Borrowed franchise/universe credit (per-tmdb, recency-decayed by refresh_scores): an
        # UNTAGGED hot-saga member resists this stale-prune DELETE just as it resists the space-
        # pressure delete/downgrade — deletion must not be more aggressive than the step-down it
        # already survives. This path scores RAW Radarr dicts, which don't carry the column, so
        # source it from the movie_files parquet. Empty (no guard) when the credit pass hasn't run
        # or the column is cold -> byte-identical.
        credit_by_tmdb: dict[int, float] = {}
        try:
            _mfm = self.registry.get("manager", "RadarrCacheMovieFilesManager") if self.registry else None
            _mdf = _mfm.load(instance) if _mfm is not None else None
            if _mdf is not None and not _mdf.empty \
                    and "universe_credit" in _mdf.columns and "tmdb_id" in _mdf.columns:
                import pandas as pd
                _t = pd.to_numeric(_mdf["tmdb_id"], errors="coerce")
                _c = pd.to_numeric(_mdf["universe_credit"], errors="coerce")
                for _ti, _ci in zip(_t, _c):
                    if pd.notna(_ti) and pd.notna(_ci):
                        credit_by_tmdb[int(_ti)] = float(_ci)
        except Exception:
            credit_by_tmdb = {}

        clock_key = self._DELETE_CLOCK_KEY.format(inst=instance)
        clock = self.global_cache.get(clock_key)
        clock = clock if isinstance(clock, dict) else {}
        new_clock: dict[str, str] = {}
        now = datetime.now(timezone.utc)

        unmonitor_ids: list[int] = []
        delete_pairs:  list[tuple[int, int]] = []   # (movie_id, movie_file_id)
        deleted_tmdbs: list[int] = []
        title_by_id:  dict[int, str] = {}
        year_by_id:   dict[int, str] = {}
        rating_by_id: dict[int, str] = {}
        # Dwell-passed delete candidates, resolved into delete_pairs AFTER the loop so the
        # optional U-budget can keep only enough of them (worst/biggest first).
        delete_candidates: list[dict] = []

        for movie in owned:
            stats["checked"] += 1
            mid     = movie.get("id")
            tmdb_id = int(movie["tmdbId"])
            if mid is None:
                continue
            title_by_id[mid]  = movie.get("title", str(mid))
            year_by_id[mid]   = _movie_year(movie)
            rating_by_id[mid] = _movie_rating(movie)
            k = str(tmdb_id)

            # Hard guards — never touch, never clock.
            keep_policy = self._resolve_keep_policy(movie, ctx["tag_label_map"])
            if keep_policy or tmdb_id in ctx["watched_tmdb_ids"]:
                stats["guarded"] += 1
                continue

            score, has_credits = self._score_owned(movie, ctx, score_movie)
            # ── Score gate (pure: defer / error / recovered / below_floor) ──────
            gate = prune_score_gate(score, has_credits, floor)
            if gate == "defer":
                # Affinity unknown → defer; preserve any existing clock, take no action.
                if k in clock:
                    new_clock[k] = clock[k]
                stats["deferred"] += 1
                continue
            if gate == "error":          # scoring error sentinel → do nothing this run
                if k in clock:
                    new_clock[k] = clock[k]
                continue
            if gate == "recovered":      # recovered → drop from clock (reset dwell)
                stats["recovered"] += 1
                continue

            stats["below_floor"] += 1
            since_iso, age_days = clock_age(clock.get(k) or now.isoformat(), now)

            movie_file = movie.get("movieFile") or {}
            fid = movie_file.get("id")
            # Franchise exemption (default-off): a below-floor movie in a substantially-
            # watched collection is spared DELETION (still eligible to unmonitor/age) by
            # making it ineligible for the delete branch.
            _coll_id = (movie.get("collection") or {}).get("tmdbId")
            _exempt = franchise_delete_exempt(
                collection_tmdb_id=_coll_id,
                sibling_tmdb_ids=ctx["collection_members"].get(int(_coll_id), set()) if _coll_id else set(),
                watched_tmdb_ids=ctx["watched_tmdb_ids"], movie_tmdb_id=tmdb_id,
                threshold=_franchise_threshold, enabled=_franchise_exempt_enabled,
            )
            # Hot franchise/universe credit spares the DELETE branch (still eligible to
            # unmonitor/age, exactly like the franchise exemption) — the recency decay lets a
            # stale saga member become deletable again once its credit drops below the floor.
            _credit_protected = credit_by_tmdb.get(tmdb_id, 0.0) >= UNIVERSE_PROTECT_MIN
            if _credit_protected and bool(fid) and not _exempt:
                stats["skipped_universe"] += 1
            _has_fid_delete = bool(fid) and not _exempt and not _credit_protected
            # ── Dwell decision (pure: delete / unmonitor / age) ────────────────
            action = prune_below_floor_action(
                age_days=age_days, delete_enabled=delete_enabled, delete_active=delete_active,
                has_fid=_has_fid_delete, eff_delete_days=eff_delete_days, pressure_active=pressure_active,
                unmonitor_days=unmonitor_days, monitored=movie.get("monitored"),
            )
            if action == "delete":
                # Collected, not finalized — the post-loop budget decides which actually
                # delete (default-off → all of them, byte-identical).
                try:
                    _size_gb = float(movie_file.get("size") or 0) / (1024 ** 3)
                except (TypeError, ValueError):
                    _size_gb = 0.0
                delete_candidates.append(
                    {"mid": mid, "fid": int(fid), "tmdb": tmdb_id, "score": score,
                     "size_gb": _size_gb, "k": k, "since_iso": since_iso}
                )
                stats["aging"] += 1
            else:
                # Only unmonitor/delete under space pressure; otherwise just keep the
                # clock advancing so the dwell is counted for when pressure arrives.
                if action == "unmonitor":
                    unmonitor_ids.append(mid)
                new_clock[k] = since_iso        # keep clocking toward the delete mark
                stats["aging"] += 1

        # ── Resolve delete candidates through the optional U-budget ───────────────
        # Default-off (or free space unknown) → ALL candidates delete, byte-identical to
        # the original in-loop append. Enabled → keep only enough (worst/biggest first) to
        # reclaim back to U; the rest keep their clock and age toward next pass.
        _need_gb = (U - free_gb) if free_gb != float("inf") else None
        _to_delete = budget_delete_cohort(delete_candidates, need_gb=_need_gb, enabled=_budget_enabled)
        _delete_ids = {id(c) for c in _to_delete}
        for c in delete_candidates:
            if id(c) in _delete_ids:
                delete_pairs.append((c["mid"], c["fid"]))
                deleted_tmdbs.append(c["tmdb"])
                if self.dry_run:
                    new_clock[c["k"]] = c["since_iso"]   # preview keeps surfacing it
                # live: file goes away → clock pruned; restore-set takes over
            else:
                new_clock[c["k"]] = c["since_iso"]       # budget-deferred → keep ageing

        try:
            self.global_cache.set(clock_key, new_clock)
        except Exception:
            pass

        # ── stage 1: unmonitor ────────────────────────────────────────────────────
        if unmonitor_ids:
            if self.dry_run:
                _u_rows: list[list] = []
                for mid in unmonitor_ids:
                    _u_rows.append([
                        str(title_by_id.get(mid, mid))[:28], year_by_id.get(mid, "-"),
                        rating_by_id.get(mid, "-"), str(mid),
                    ])
                _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
                if _rs is not None:
                    _rs.add_rows("radarr", "Stale-prune: would unmonitor", instance,
                                 ["Title", "Year", "Rating", "Id"], _u_rows, order=31)
                else:
                    self.logger.log_grid(
                        ["Title", "Year", "Rating", "Id"], _u_rows,
                        title=f"Radarr stale-prune: would unmonitor (>={unmonitor_days}d below {floor})",
                        cap=28,
                    )
                stats["unmonitored"] = len(unmonitor_ids)
            else:
                try:
                    resp = self.radarr_api._make_request(
                        instance, "movie/editor", method="PUT",
                        payload={"movieIds": unmonitor_ids, "monitored": False},
                    )
                    if resp:
                        stats["unmonitored"] = len(unmonitor_ids)
                        self.logger.log_info(f"  Unmonitored {len(unmonitor_ids)} stale movie(s)")
                    else:
                        stats["failed"] += len(unmonitor_ids)
                except Exception as e:
                    self.logger.log_warning(f"  Bulk unmonitor failed: {e}")
                    stats["failed"] += len(unmonitor_ids)

        # ── stage 2: delete file (destructive — guarded, restorable) ──────────────
        if delete_pairs:
            if self.dry_run:
                _d_rows: list[list] = []
                for mid, fid in delete_pairs:
                    _d_rows.append([
                        str(title_by_id.get(mid, mid))[:28], year_by_id.get(mid, "-"),
                        rating_by_id.get(mid, "-"), str(mid), str(fid),
                    ])
                _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
                if _rs is not None:
                    _rs.add_rows("radarr", "Stale-prune: would delete file", instance,
                                 ["Title", "Year", "Rating", "Id", "FileId"], _d_rows, order=32)
                else:
                    self.logger.log_grid(
                        ["Title", "Year", "Rating", "Id", "FileId"], _d_rows,
                        title=f"Radarr stale-prune: would DELETE file (>={delete_days}d below {floor})",
                        cap=28,
                    )
                stats["deleted"] = len(delete_pairs)
            else:
                # Unmonitor first so Radarr won't instantly re-grab the file we delete.
                try:
                    self.radarr_api._make_request(
                        instance, "movie/editor", method="PUT",
                        payload={"movieIds": [m for m, _ in delete_pairs], "monitored": False},
                    )
                except Exception as e:
                    self.logger.log_warning(f"  Pre-delete unmonitor failed: {e}")
                ok = 0
                _m_rows: list[list] = []          # per-movie deletes for the end-of-run summary
                for mid, fid in delete_pairs:
                    try:
                        self.radarr_api._make_request(instance, f"moviefile/{fid}", method="DELETE")
                        ok += 1
                        _m_rows.append([str(title_by_id.get(mid, mid))[:28], year_by_id.get(mid, "-"),
                                        rating_by_id.get(mid, "-"), str(mid), "deleted"])
                    except Exception as e:
                        self.logger.log_warning(
                            f"  Delete failed for '{title_by_id.get(mid, mid)}' (fileId={fid}): {e}"
                        )
                        stats["failed"] += 1
                stats["deleted"] = ok
                _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
                if _rs is not None and _m_rows:
                    _rs.add_rows("radarr", "Deletions & movements", instance,
                                 ["Title", "Year", "Rating", "Id", "Action"], _m_rows, order=40)
                # Record real deletions so they can be restored if the score recovers.
                if deleted_tmdbs:
                    dkey = self._DELETED_SET_KEY.format(inst=instance)
                    dset = self.global_cache.get(dkey)
                    dset = dset if isinstance(dset, dict) else {}
                    for t in deleted_tmdbs:
                        dset[str(t)] = now.isoformat()
                    try:
                        self.global_cache.set(dkey, dset)
                    except Exception:
                        pass

        prefix   = "[dry_run] " if self.dry_run else ""
        expedite = (f" [expedited from {delete_days}d, {free_gb:.0f}GB free]"
                    if eff_delete_days < delete_days else "")
        idle = "" if pressure_active else f" [space OK ({free_gb:.0f}GB >= {U:.0f}GB) - clocks only, no prune]"
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["checked",     stats["checked"]],
                ["below_floor", stats["below_floor"]],
                ["aging",       stats["aging"]],
                ["unmonitored", stats["unmonitored"]],
                ["deleted",     stats["deleted"]],
                ["guarded",     stats["guarded"]],
                ["deferred",    stats["deferred"]],
                ["failed",      stats["failed"]],
            ],
            title=(
                f"[Anomaly] {prefix}Stale-owned prune - '{instance}' "
                f"(floor<{floor}, unmonitor@{unmonitor_days}d, "
                f"delete@{eff_delete_days}d{expedite}{idle}{'' if delete_enabled else ' [disabled]'})"
            ),
            caption="Two-stage prune of stale low-watchability owned movies: unmonitor then delete.",
            descriptions=[
                "owned has-file movies examined this pass",
                "movies scoring below the demote floor",
                "below-floor movies whose dwell clock advanced",
                "stale movies set unmonitored this pass",
                "stale movie files deleted this pass",
                "skipped: keep-tagged or already watched",
                "deferred: Trakt credits not cached yet",
                "movies whose unmonitor/delete call errored",
            ],
        )
        return stats

    # ── Restore: re-acquire deleted movies whose watchability recovered ──────────

    @LoggerManager().log_function_entry
    @timeit("restore_recovered_deletions")
    def restore_recovered_deletions(self, instance: str) -> dict:
        """
        Re-acquire movies previously deleted by the prune pass whose score has
        recovered above owned_restore_score_threshold (default 20): re-monitor +
        trigger a search so Radarr re-downloads. Tracked in global_cache
        ``radarr/<instance>/demote_deleted``; entries drop when restored, when the
        movie regains a file by other means, or when it leaves Radarr.
        """
        instance = self._resolve_instance(instance)
        stats = {"tracked": 0, "restored": 0, "still_low": 0, "cooling": 0,
                 "dropped": 0, "deferred": 0, "failed": 0}
        if self.radarr_api is None or self.global_cache is None:
            return stats
        dkey = self._DELETED_SET_KEY.format(inst=instance)
        dset = self.global_cache.get(dkey)
        dset = dset if isinstance(dset, dict) else {}
        if not dset:
            return stats

        now = datetime.now(timezone.utc)
        try:
            restore_floor = int(self.config.get("owned_restore_score_threshold", 20) if self.config else 20)
        except (TypeError, ValueError):
            restore_floor = 20
        try:
            restore_min_age = int(self.config.get("owned_restore_min_age_days", 0) if self.config else 0)
        except (TypeError, ValueError):
            restore_min_age = 0

        from scripts.managers.services.trakt.movies.scorer import score_movie
        ctx = self._build_scoring_context(instance)

        keep: dict[str, str] = {}
        restore: list[tuple[str, int, str]] = []   # (tmdb_key, movie_id, title)
        meta_by_mid: dict[int, tuple[str, str]] = {}   # movie_id -> (year, rating)
        for k, iso in dset.items():
            stats["tracked"] += 1
            try:
                tmdb_id = int(k)
            except (TypeError, ValueError):
                continue
            movie = ctx["movie_by_tmdb"].get(tmdb_id)
            if not movie:
                stats["dropped"] += 1
                continue                       # gone from Radarr
            if movie.get("hasFile"):
                stats["dropped"] += 1
                continue                       # re-acquired by other means
            score, has_credits = self._score_owned(movie, ctx, score_movie)
            if not has_credits:
                keep[k] = iso
                stats["deferred"] += 1
                continue
            if score > restore_floor:
                if restore_cooldown_active(iso, now, restore_min_age):
                    # Score recovered but the re-grab cooldown hasn't elapsed since
                    # deletion — hold off so a title hovering at the floor can't be
                    # deleted one run and restored the next, repeatedly.
                    keep[k] = iso
                    stats["cooling"] += 1
                    continue
                mid = movie.get("id")
                if mid is not None:
                    restore.append((k, mid, movie.get("title", str(mid))))
                    meta_by_mid[mid] = (_movie_year(movie), _movie_rating(movie))
                else:
                    keep[k] = iso
            else:
                keep[k] = iso
                stats["still_low"] += 1

        if restore:
            mids = [mid for _, mid, _ in restore]
            if self.dry_run:
                _r_rows: list[list] = []
                for _, mid, title in restore:
                    _yr, _rt = meta_by_mid.get(mid, ("-", "-"))
                    _r_rows.append([str(title)[:28], _yr, _rt, str(mid)])
                _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
                if _rs is not None:
                    _rs.add_rows("radarr", "Would restore deleted", instance,
                                 ["Title", "Year", "Rating", "Id"], _r_rows, order=35)
                else:
                    self.logger.log_grid(
                        ["Title", "Year", "Rating", "Id"], _r_rows,
                        title=f"Radarr: would restore deleted (score>{restore_floor}) [dry_run]",
                        cap=28,
                    )
                stats["restored"] = len(restore)
                for k, _, _ in restore:        # still deleted in reality → keep tracking
                    keep[k] = dset[k]
            else:
                try:
                    self.radarr_api._make_request(
                        instance, "movie/editor", method="PUT",
                        payload={"movieIds": mids, "monitored": True},
                    )
                    self.radarr_api._make_request(
                        instance, "command", method="POST",
                        payload={"name": "MoviesSearch", "movieIds": mids},
                    )
                    stats["restored"] = len(restore)
                    self.logger.log_info(f"  Restored {len(restore)} movie(s): re-monitored + searched")
                except Exception as e:
                    self.logger.log_warning(f"  Restore failed: {e}")
                    stats["failed"] = len(restore)
                    for k, _, _ in restore:    # retry next run
                        keep[k] = dset[k]

        try:
            self.global_cache.set(dkey, keep)
        except Exception:
            pass

        prefix = "[dry_run] " if self.dry_run else ""
        cooldown = f", cooldown>={restore_min_age}d" if restore_min_age > 0 else ""
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["tracked",   stats["tracked"]],
                ["restored",  stats["restored"]],
                ["still_low", stats["still_low"]],
                ["cooling",   stats["cooling"]],
                ["deferred",  stats["deferred"]],
                ["dropped",   stats["dropped"]],
                ["failed",    stats["failed"]],
            ],
            title=f"[Anomaly] {prefix}Deletion-restore - '{instance}' (score>{restore_floor}{cooldown})",
            caption="Re-acquires previously pruned movies whose watchability score recovered.",
            descriptions=[
                "previously deleted movies still being tracked",
                "movies re-monitored and re-searched this pass",
                "movies still scoring at or below the floor",
                "recovered but held: re-grab cooldown not yet elapsed",
                "deferred: Trakt credits not cached yet",
                "entries dropped: gone or re-acquired otherwise",
                "movies whose restore call errored",
            ],
        )
        return stats

    # ── Triage: score monitored-missing and act ─────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("triage_monitored_missing")
    def triage_monitored_missing(self, instance: str) -> dict:
        """
        Score every monitored-but-missing movie through the watchability matrix and
        route it (the routing + release-availability decisions live in
        lifecycle/monitor_policy):

          score >= WATCH_THRESHOLD (60)                    → MoviesSearch at current quality
          UNMONITOR_BELOW (20) <= score < WATCH, wrong QP  → adjust to HD-720p then search
          score < UNMONITOR_BELOW (20)                     → unmonitor (unless keep-tagged
                                                             or credits not yet fetched)

        Inputs pulled from global_cache so no extra API calls needed.
        """
        WATCH_THRESHOLD    = 60   # score ≥ this → actively search (0-100 scale)
        UNMONITOR_BELOW    = 20   # score < this → unmonitor

        instance = self._resolve_instance(instance)
        stats = {
            "checked": 0, "searched": 0, "adjusted_and_searched": 0,
            "unmonitored": 0, "skipped_keep_tagged": 0, "failed": 0,
        }

        if self.radarr_api is None or self.global_cache is None:
            self.logger.log_warning(
                "[Anomaly] triage_monitored_missing requires radarr_api + global_cache"
            )
            return stats

        # ── Fetch inputs from global_cache ──────────────────────────────────────
        missing = self.find_monitored_missing_files(instance)
        if not missing:
            return stats

        # Shared scoring inputs (same lookups used by repair_unmonitored_with_files)
        ctx = self._build_scoring_context(instance)
        all_movies         = ctx["all_movies"]
        movie_by_tmdb      = ctx["movie_by_tmdb"]
        movie_by_id        = ctx["movie_by_id"]
        genre_affinity     = ctx["genre_affinity"]
        watched_tmdb_ids   = ctx["watched_tmdb_ids"]
        collection_members = ctx["collection_members"]
        tag_label_map      = ctx["tag_label_map"]
        people_mgr         = ctx["people_mgr"]

        # HD-720p profile id (for marginal movies) + id->name map for the grid's Profile column.
        hd720p_id: int | None = None
        _prof_name_by_id: dict = {}
        try:
            raw_profiles = (
                self.global_cache.get(f"radarr.quality.{instance}")
                or self.radarr_api._make_request(instance, "qualityprofile", fallback=[]) or []
            )
            for p in raw_profiles:
                if p.get("id") is not None:
                    _prof_name_by_id[int(p["id"])] = p.get("name") or str(p["id"])
                if hd720p_id is None and (p.get("name") or "").lower().strip() == "hd-720p":
                    hd720p_id = p["id"]
        except Exception:
            pass
        _hd720p_name = _prof_name_by_id.get(hd720p_id, "HD-720p") if hd720p_id is not None else "HD-720p"

        # ── Score and act ────────────────────────────────────────────────────────
        # Live writes are deferred into id-lists and flushed as bulk /movie/editor
        # calls after the loop (one PUT per action vs a per-movie GET+PUT each).
        search_ids:    list[int] = []
        search_titles: dict[int, str] = {}
        unmonitor_ids: list[int] = []
        adjust_ids:    list[int] = []
        now_utc = datetime.now(timezone.utc)
        _rows: list[list] = []   # [Title, Action, Score] grid, dry_run + live

        for c in missing:
            stats["checked"] += 1
            mid      = c["movie_id"]
            title    = c["title"]
            tmdb_id  = c.get("tmdb_id")
            movie    = movie_by_id.get(int(mid)) if mid else None
            if not movie:
                movie = movie_by_tmdb.get(int(tmdb_id)) if tmdb_id else None

            # Explicit keep/universe override — resolved once, used by both the
            # scorer (soft signal) and the unmonitor guard below (hard override).
            keep_policy  = self._resolve_keep_policy(movie, tag_label_map) if movie else None
            has_keep_tag = keep_policy is not None

            # ── Release availability guard ──────────────────────────────────────────────
            # Skip search entirely if no home-media release has passed (isAvailable,
            # a physical/digital date in the past, or 'released' status). Radarr's
            # isAvailable respects minimumAvailability — trusted first (decision in
            # lifecycle/monitor_policy.release_available).
            if movie and not release_available(movie, now_utc):
                self.logger.log_debug(
                    f"  ⏩ '{title}' — not yet available "
                    f"(status={movie.get('status')}, isAvailable=False) — skipping search"
                )
                stats["not_available"] = stats.get("not_available", 0) + 1
                continue

            # Score — default 5 (neutral) if we can't resolve the movie. Bind
            # credits BEFORE the scoring block so an unresolvable movie (movie=None,
            # e.g. tmdb/id not in the scoring maps) leaves credits={} rather than
            # unbound — `credits_fetched = bool(credits)` below would otherwise
            # NameError and crash the whole triage pass. The no-credits/low-score
            # row then routes to 'defer' via triage_action, a safe outcome.
            score = 5
            credits: dict = {}
            if movie:
                if people_mgr and tmdb_id:
                    try:
                        credits = people_mgr.get_people(int(tmdb_id)) or {}
                    except Exception:
                        pass
                try:
                    from scripts.managers.services.trakt.movies.scorer import score_movie
                    score = score_movie(
                        movie=movie,
                        completion_pct=0.0,   # not watched yet
                        completion_threshold=0.9,
                        collection_members=collection_members,
                        watched_tmdb_ids=watched_tmdb_ids,
                        genre_affinity=genre_affinity,
                        credits=credits,
                        imdb_rating=((movie.get("ratings") or {}).get("imdb") or {}).get("value"),
                        tmdb_rating=((movie.get("ratings") or {}).get("tmdb") or {}).get("value"),
                        trakt_rating=((movie.get("ratings") or {}).get("trakt") or {}).get("value"),
                        metacritic_score=((movie.get("ratings") or {}).get("metacritic") or {}).get("value"),
                        rotten_tomatoes_score=((movie.get("ratings") or {}).get("rottenTomatoes") or {}).get("value"),
                        popularity=movie.get("popularity"),
                        certification=movie.get("certification"),
                        in_cinemas_date=movie.get("inCinemas"),
                        original_language=(movie.get("originalLanguage") or {}).get("name") if isinstance(movie.get("originalLanguage"), dict) else movie.get("originalLanguage"),
                        is_franchise_entry=False,   # franchise detection not yet implemented
                        universe_name=None,   # not in raw Radarr dict — Parquet-only
                        keep_policy=keep_policy,
                    )
                except Exception as e:
                    self.logger.log_debug(f"  ↺ Score failed for '{title}': {e}")

            cur_profile_id = (movie or {}).get("qualityProfileId")
            _cur_prof = (
                _prof_name_by_id.get(int(cur_profile_id), "-") if cur_profile_id is not None else "-"
            )

            self.logger.log_debug(
                f"  📊 '{title}' (tmdb={tmdb_id}) — score={score}"
            )

            # ── Route by score (decision in lifecycle/monitor_policy.triage_action) ──
            # keep_skip: an explicit keep/universe tag is a user override — never
            #   unmonitor it, however low it scores (a missing movie often has no
            #   ratings in the raw dict and scores artificially low). defer: below the
            #   floor but credits aren't fetched yet, so the score is unreliable —
            #   wait for enrichment rather than unmonitor a household favourite.
            credits_fetched = bool(credits)  # empty dict = not fetched
            # Household-watched hard override: a movie the household HAS watched but
            # lost its file is always re-acquired, never unmonitored (mirrors the
            # stale-prune watched-set guard).
            household_watched = tmdb_id is not None and int(tmdb_id) in watched_tmdb_ids
            action = triage_action(
                score=score, has_keep_tag=has_keep_tag, credits_fetched=credits_fetched,
                cur_profile_id=cur_profile_id, hd720p_id=hd720p_id,
                watch_threshold=WATCH_THRESHOLD, unmonitor_below=UNMONITOR_BELOW,
                household_watched=household_watched,
            )

            # Shared leading cells for whichever action row we append below.
            _meta = [str(title)[:28], _movie_year(movie), _movie_rating(movie)]

            if action == "keep_skip":
                _rows.append(_meta + ["keep-skip", str(score), _cur_prof])
                stats["skipped_keep_tagged"] += 1
                continue

            if action == "defer":
                self.logger.log_debug(
                    f"  ⏳ '{title}' — score={score} but no credits yet, deferring unmonitor decision"
                )
                stats["deferred"] = stats.get("deferred", 0) + 1
                continue

            if action == "unmonitor":
                # Unlikely to be watched — unmonitor (batched after the loop).
                _rows.append(_meta + ["unmonitor", str(score), _cur_prof])
                if self.dry_run:
                    stats["unmonitored"] += 1
                    continue
                unmonitor_ids.append(mid)

            elif action == "adjust_and_search":
                # Marginal watchability — lower quality bar first, then search
                _rows.append(_meta + ["adjust+search", str(score), f"{_cur_prof}->{_hd720p_name}"])
                if self.dry_run:
                    stats["adjusted_and_searched"] += 1
                    continue
                adjust_ids.append(mid)
                search_ids.append(mid)
                search_titles[mid] = title

            else:  # "search"
                # Good watchability — search at current quality
                _rows.append(_meta + ["search", str(score), _cur_prof])
                search_ids.append(mid)
                search_titles[mid] = title

        _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
        if _rs is not None:
            _rs.add_rows("radarr", "Monitored-missing triage", instance,
                         ["Title", "Year", "Rating", "Action", "Score", "Profile"], _rows, order=30)
        else:
            self.logger.log_grid(
                ["Title", "Year", "Rating", "Action", "Score", "Profile"], _rows,
                title=(
                    f"Radarr triage: monitored-missing{' [dry_run]' if self.dry_run else ''}"
                    f"  (search>={WATCH_THRESHOLD}, adjust {UNMONITOR_BELOW}-{WATCH_THRESHOLD - 1}, "
                    f"unmonitor<{UNMONITOR_BELOW})"
                ),
                cap=28,
            )

        # ── Batch profile adjust (marginal movies → HD-720p) ─────────────────────
        # One PUT /movie/editor applies the same qualityProfileId to all; runs
        # BEFORE the batch search so Radarr searches at the lowered quality bar.
        # Credit adjusted_and_searched only on success (mirroring C1/C2); on
        # failure count it as failed AND drop those ids from the search so we
        # never grab at the un-lowered profile we failed to set.
        if adjust_ids and not self.dry_run:
            resp = None
            try:
                resp = self.radarr_api._make_request(
                    instance, "movie/editor", method="PUT",
                    payload={"movieIds": adjust_ids, "qualityProfileId": hd720p_id},
                )
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Bulk HD-720p adjust failed: {e}")
            if resp:
                stats["adjusted_and_searched"] += len(adjust_ids)
                self.logger.log_info(f"  📉 Adjusted {len(adjust_ids)} movie(s) to HD-720p")
            else:
                stats["failed"] += len(adjust_ids)
                _drop = set(adjust_ids)
                search_ids = [i for i in search_ids if i not in _drop]
                self.logger.log_warning(
                    f"  ⚠️ HD-720p adjust failed for {len(adjust_ids)} movie(s) — skipping their search"
                )

        # ── Batch unmonitor (low-score missing movies) ───────────────────────────
        if unmonitor_ids and not self.dry_run:
            try:
                resp = self.radarr_api._make_request(
                    instance, "movie/editor", method="PUT",
                    payload={"movieIds": unmonitor_ids, "monitored": False},
                )
                if resp:
                    stats["unmonitored"] += len(unmonitor_ids)
                    self.logger.log_info(f"  🔕 Unmonitored {len(unmonitor_ids)} low-score movie(s)")
                else:
                    stats["failed"] += len(unmonitor_ids)
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Bulk unmonitor failed: {e}")
                stats["failed"] += len(unmonitor_ids)

        # ── Batch search ─────────────────────────────────────────────────────────
        # Cancel in-flight downloads before re-searching so Radarr grabs at
        # the newly-scored quality profile, not a stale one.
        if search_ids and not self.dry_run:
            try:
                from scripts.managers.factories.mixins.queue_cancel import QueueCancelMixin
                _qc2            = QueueCancelMixin()
                _qc2.radarr_api = self.radarr_api
                _qc2.logger     = self.logger
                _qc2.dry_run    = False
                _cancelled = 0
                for _cmid in search_ids:
                    _cancelled += _qc2._cancel_radarr_queue_for_movie(
                        instance, _cmid,
                        movie_title=search_titles.get(_cmid, str(_cmid))
                    )
                if _cancelled:
                    self.logger.log_info(
                        f"  🗑️ Cancelled {_cancelled} in-flight queue item(s) before re-search"
                    )
                    stats["queue_cancelled"] = stats.get("queue_cancelled", 0) + _cancelled
            except Exception as _qe:
                self.logger.log_warning(f"  ⚠️ Queue cancel pass failed: {_qe}")


        if search_ids and not self.dry_run:
            try:
                self.radarr_api._make_request(
                    instance, "command", method="POST",
                    payload={"name": "MoviesSearch", "movieIds": search_ids},
                )
                self.logger.log_info(
                    f"  📡 Batch search triggered for {len(search_ids)} movie(s)"
                )
                stats["searched"] += len(search_ids)
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Batch search failed: {e}")
                stats["failed"] += len(search_ids)
        elif search_ids and self.dry_run:
            stats["searched"] += len(search_ids)

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["checked",               stats["checked"]],
                ["searched",              stats["searched"]],
                ["adjusted_and_searched", stats["adjusted_and_searched"]],
                ["unmonitored",           stats["unmonitored"]],
                ["skipped_keep_tagged",   stats["skipped_keep_tagged"]],
                ["deferred",              stats.get("deferred", 0)],
                ["not_available",         stats.get("not_available", 0)],
                ["failed",                stats["failed"]],
            ],
            title=f"[Anomaly] {prefix}Monitored-missing triage - '{instance}'",
            caption="Scores monitored-but-missing movies and routes each to search, adjust, or unmonitor.",
            descriptions=[
                "monitored-missing movies examined this pass",
                "movies a search was triggered for as-is",
                "movies dropped to HD-720p then searched",
                "low-score movies set unmonitored",
                "skipped: keep/universe tagged, never unmonitored",
                "deferred: Trakt credits not cached yet",
                "skipped: no home-media release available yet",
                "movies whose search/edit/unmonitor call errored",
            ],
        )
        return stats



    @LoggerManager().log_function_entry
    @timeit("run_anomaly_scan")
    def run(self, instance: str) -> dict:
        """Run all anomaly checks and repairs, return combined results."""
        instance        = self._resolve_instance(instance)
        res_mis         = self.find_resolution_mismatches(instance)
        triage_stats    = self.triage_monitored_missing(instance)
        remonitor_stats = self.repair_unmonitored_with_files(instance)   # promote (add)
        demote_stats    = self.demote_stale_monitored(instance)          # prune (unmonitor → delete)
        restore_stats   = self.restore_recovered_deletions(instance)     # restore recovered deletions
        return {
            "resolution_mismatches":   res_mis,
            "monitored_missing_triage": triage_stats,
            "remonitored":             remonitor_stats,
            "demoted":                 demote_stats,
            "restored":                restore_stats,
        }